#!/usr/bin/env python3
"""Fresh eBay AU 5-card reliability run harness (authorised offline script).

Live eBay search submissions are capped at 5. Bootstrap (Xvfb/Chrome about:blank)
does not count. Live attempts are consumed only via SEARCH_SUBMISSION_STARTED.
Stops fail-closed on challenge/SORRY/preflight denial.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.live_navigation_attempt import (
    has_search_submission_started,
    new_attempt_id,
)
from cardscanr_market_engine.local_browser_runtime import ensure_xvfb, probe_pre_live_runtime
from cardscanr_market_engine.marketplace_ops_state import get_active_cooldown
from cardscanr_market_engine.owned_daily_outcomes import (
    CHECKED_NO_NEW_EXACT_EVIDENCE,
    UNCHANGED_FROM_EBAY,
    UPDATED_FROM_EBAY,
)
from cardscanr_market_engine.demand_aware_scheduler import (
    DemandIndex,
    evaluate_demand_aware_target,
    events_from_job_rows,
    select_fair_lane_mix,
)
from cardscanr_market_engine.owned_daily_pacing import OwnedDailyPacingConfig
from cardscanr_market_engine.owned_daily_scheduler import OwnedDailySchedulerConfig, OwnedPrintingRefreshScheduler
from cardscanr_market_engine.providers.factory import create_market_comps_provider
from cardscanr_market_engine.providers.linux_x11_ebay_nav import ensure_chrome_with_cdp
from cardscanr_market_engine.providers.query_builder import build_provider_search_queries
from cardscanr_market_engine.reliability_harness_classification import (
    classify_reliability_card_verdict,
    is_structured_challenge,
)
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.models import ProviderRequest
from tools.desktop_ebay_e2e_pricing import run_forced_job

OUT = ROOT / "reports" / "artifacts" / "ebay_5_card_reliability_fresh"
OUT.mkdir(parents=True, exist_ok=True)
CARDS_DIR = OUT / "cards"
CARDS_DIR.mkdir(parents=True, exist_ok=True)

AUTHORISED_MAX = 5


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _configure() -> None:
    os.environ["EBAY_BROWSER_NAV_MODE"] = "linux_x11"
    os.environ["EBAY_BROWSER_ENABLED"] = "true"
    os.environ["EBAY_BROWSER_HEADLESS"] = "false"
    os.environ["EBAY_BROWSER_MAX_QUERY_ATTEMPTS"] = "1"
    os.environ["EBAY_BROWSER_REUSE_CONTEXT"] = "true"
    os.environ.setdefault("EBAY_BROWSER_CDP_PORT", "9444")
    os.environ.setdefault("EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS", "20")
    os.environ.setdefault("EBAY_BROWSER_COOLDOWN_SECONDS", "20")
    os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"


def _cdp_targets(port: int = 9444) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    version: dict[str, Any] = {}
    targets: list[dict[str, Any]] = []
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as resp:
        version = json.loads(resp.read().decode("utf-8", errors="replace"))
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as resp:
        raw = json.loads(resp.read().decode("utf-8", errors="replace"))
        targets = raw if isinstance(raw, list) else []
    ebay = [str(t.get("url") or "") for t in targets if "ebay." in str(t.get("url") or "").lower()]
    return version, targets, ebay


def _gate_ok() -> tuple[bool, dict[str, Any]]:
    gate = evaluate_ebay_browser_work_gate(market="AU", for_probe=False)
    payload = {
        "gate": gate.to_dict(),
        "activeAuChallenges": gate.active_challenge_count,
        "cooldown": None if get_active_cooldown("AU") is None else get_active_cooldown("AU").to_dict(),
        "ownedDaily": Path("reports/runtime/owned_daily_full_enable.flag").read_text(encoding="utf-8").strip(),
    }
    return bool(gate.allowed) and gate.active_challenge_count == 0 and payload["ownedDaily"] == "false", payload


def _pre_live_ok() -> tuple[bool, dict[str, Any]]:
    payload = probe_pre_live_runtime(cdp_port=9444)
    return bool(payload.get("ready")), payload


def _select_due_cards(client: SupabaseMarketEngineClient, *, limit: int = 5) -> list[dict[str, Any]]:
    """Demand-aware owned-daily ordering (AU). Fresh cards are not selected."""
    sched = OwnedPrintingRefreshScheduler(client=client, config=OwnedDailySchedulerConfig.from_env())
    now = datetime.now(timezone.utc)
    if hasattr(client, "list_recent_user_demand_jobs"):
        try:
            sched._demand_index = DemandIndex(
                events_from_job_rows(client.list_recent_user_demand_jobs(hours=168) or [])
            )
        except Exception:
            sched._demand_index = DemandIndex([])
    payload = client.list_owned_market_pricing_targets(include_zero_owners=False)
    targets = payload.get("targets") or payload.get("items") or []
    if isinstance(payload, list):
        targets = payload
    explain_rows = []
    enriched_by_id: dict[str, dict[str, Any]] = {}
    for t in targets:
        if not isinstance(t, dict):
            continue
        if str(t.get("market_country") or t.get("market") or "AU").upper() != "AU":
            continue
        enriched = client.enrich_owned_target_from_cache(t)
        decision = sched.evaluate_target(enriched, now=now)
        da = decision.details.get("_demand_row")
        if da is None:
            da = evaluate_demand_aware_target(
                enriched, now=now, demand_index=sched._demand_index
            )
        kid = str(enriched.get("market_price_key_id") or enriched.get("price_key_id") or "").strip()
        if not kid:
            continue
        if not da.due or not decision.should_enqueue:
            continue
        ident = kid
        if ident in enriched_by_id:
            continue
        enriched_by_id[ident] = {
            "priceKeyId": kid,
            "fingerprint": str(enriched.get("fingerprint") or ""),
            "resolvedKeyFingerprint": enriched.get("resolved_key_fingerprint"),
            "card": str(enriched.get("card_name") or enriched.get("normalized_card_name") or ""),
            "set": str(enriched.get("set_name") or ""),
            "collector": str(enriched.get("collector_number") or ""),
            "language": str(enriched.get("language") or enriched.get("language_code") or "en"),
            "market": "AU",
            "currency": str(enriched.get("currency") or "AUD"),
            "schedulerReason": decision.reason,
            "priorityBand": decision.details.get("owned_priority_band"),
            "score": decision.score,
            "ownerCount": enriched.get("owner_count"),
            "schedulerPrice": decision.details.get("current_market_price"),
            "cacheEnrichmentApplied": bool(enriched.get("cache_enrichment_applied")),
            "rawTarget": enriched,
            "demandAware": da.to_public_dict(),
        }
        explain_rows.append(da)
    mix = select_fair_lane_mix(explain_rows, budget=max(1, int(limit)))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in mix:
        kid = row.price_key_id
        if kid in seen:
            continue
        seen.add(kid)
        packed = enriched_by_id.get(kid)
        if packed:
            out.append(packed)
        if len(out) >= limit:
            break
    return out


def _cache_snap(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    return {
        "price": row.get("current_market_price"),
        "recommendedPrice": row.get("recommended_price"),
        "source": row.get("display_price_source") or row.get("provider"),
        "provider": row.get("provider"),
        "freshness": row.get("last_updated_at") or row.get("updated_at"),
        "refreshStatus": row.get("refresh_status"),
        "sampleCount": row.get("sample_count") or row.get("comp_count"),
        "confidence": row.get("confidence"),
    }


def _diag(result: dict[str, Any]) -> dict[str, Any]:
    pd = result.get("providerDiagnostics") or {}
    if isinstance(pd, dict) and isinstance(pd.get("diagnostics"), dict):
        return pd.get("diagnostics") or {}
    return pd if isinstance(pd, dict) else {}


def _build_job_identity(client: SupabaseMarketEngineClient, sel: dict[str, Any]) -> dict[str, Any]:
    """Construct exact production query BEFORE any live navigation."""
    kid = str(sel["priceKeyId"])
    key = client.get_price_key(price_key_id=kid)
    market = resolve_marketplace_config(
        market_country=str(getattr(key, "market_country", None) or "au"),
        currency=str(getattr(key, "currency", None) or sel.get("currency") or "AUD"),
        marketplace="ebay",
    )
    request = ProviderRequest(
        price_key=key,
        market_country=market.market_country,
        currency=market.currency,
        marketplace=market.marketplace,
        provider_marketplace_id=market.provider_marketplace_id,
        provider_domain=market.provider_domain,
        search_locale=market.search_locale,
        display_name=market.display_name,
        market_config=market,
    )
    queries = build_provider_search_queries(request, max_attempts=1)
    if not queries:
        raise RuntimeError("query_construction_failed:empty")
    primary = queries[0]
    return {
        "priceKeyId": kid,
        "fingerprint": str(getattr(key, "fingerprint", None) or sel.get("fingerprint") or ""),
        "card": str(getattr(key, "card_name", None) or sel.get("card") or ""),
        "set": str(getattr(key, "set_name", None) or sel.get("set") or ""),
        "collector": str(getattr(key, "collector_number", None) or sel.get("collector") or ""),
        "language": str(getattr(key, "language", None) or sel.get("language") or "en"),
        "market": "AU",
        "currency": str(getattr(key, "currency", None) or sel.get("currency") or "AUD"),
        "schedulerReason": sel.get("schedulerReason"),
        "priorityBand": sel.get("priorityBand"),
        "query": primary.query_text,
        "querySource": primary.query_source,
        "queryIndex": primary.query_index,
    }


def main() -> int:
    started = _utc()
    _configure()
    load_supabase_env()
    accounting = {
        "reliabilityRunAuthorisedMax": AUTHORISED_MAX,
        "liveNavigationStartedCount": 0,
        "terminalAttemptCount": 0,
        "completedHealthyPathCount": 0,
        "safeNoPriceCount": 0,
        "failedAttemptCount": 0,
        "challengeStopCount": 0,
        "retryCount": 0,
        "attemptIds": [],
    }
    run: dict[str, Any] = {
        "taskId": "CARDSCANR-EBAY-AU-FRESH-5-CARD-RELIABILITY-RUN",
        "startedAtUtc": started,
        "attemptAccounting": accounting,
        "cards": [],
        "schedulerSelection": [],
        "stopReason": None,
        "verdict": None,
        "liveAttemptDefinition": "SEARCH_SUBMISSION_STARTED durable event only",
    }

    # Phase 0
    ok, pre = _gate_ok()
    (OUT / "bootstrap").mkdir(parents=True, exist_ok=True)
    (OUT / "bootstrap" / "phase0_recheck.json").write_text(json.dumps(pre, indent=2) + "\n", encoding="utf-8")
    if not ok:
        run["verdict"] = "5_CARD_RELIABILITY_STOPPED_SAFE"
        run["stopReason"] = "phase0_gate_denied"
        run["phase0"] = pre
        run["finishedAtUtc"] = _utc()
        (OUT / "RUN_RESULT.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(run, indent=2))
        return 2

    xv = ensure_xvfb()
    (OUT / "bootstrap" / "ensure_xvfb_recheck.json").write_text(json.dumps(xv, indent=2) + "\n", encoding="utf-8")
    try:
        ensure_chrome_with_cdp(cdp_port=9444, start_url="about:blank")
    except Exception as exc:
        run["verdict"] = "5_CARD_RELIABILITY_STOPPED_SAFE"
        run["stopReason"] = f"chrome_bootstrap_failed:{type(exc).__name__}:{exc}"
        run["finishedAtUtc"] = _utc()
        (OUT / "RUN_RESULT.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(run, indent=2))
        return 2

    rt_ok, rt = _pre_live_ok()
    (OUT / "bootstrap" / "runtime_gate.json").write_text(json.dumps(rt, indent=2) + "\n", encoding="utf-8")
    if rt.get("ebayTargets"):
        run["verdict"] = "5_CARD_RELIABILITY_STOPPED_SAFE"
        run["stopReason"] = "UNEXPECTED_EBAY_TARGET_DURING_BOOTSTRAP"
        run["runtime"] = rt
        run["finishedAtUtc"] = _utc()
        (OUT / "RUN_RESULT.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(run, indent=2))
        return 2
    if not rt_ok:
        run["verdict"] = "5_CARD_RELIABILITY_STOPPED_SAFE"
        run["stopReason"] = "local_runtime_or_x11_nav_not_ready"
        run["runtime"] = rt
        run["finishedAtUtc"] = _utc()
        (OUT / "RUN_RESULT.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(run, indent=2))
        return 2

    cfg = MarketEngineConfig.from_env()
    client = SupabaseMarketEngineClient(
        supabase_url=cfg.supabase_url,
        service_role_key=supabase_secret_key_from_env(),
    )
    selections = _select_due_cards(client, limit=AUTHORISED_MAX)
    # Strip rawTarget from persisted selection for size; keep identity fields.
    persist_sel = [{k: v for k, v in row.items() if k != "rawTarget"} for row in selections]
    run["schedulerSelection"] = persist_sel
    (OUT / "scheduler_selection.json").write_text(json.dumps(persist_sel, indent=2) + "\n", encoding="utf-8")
    if not selections:
        run["verdict"] = "5_CARD_RELIABILITY_STOPPED_SAFE"
        run["stopReason"] = "no_due_scheduler_targets"
        run["finishedAtUtc"] = _utc()
        (OUT / "RUN_RESULT.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(run, indent=2))
        return 1

    pacing = OwnedDailyPacingConfig.from_env()
    inter_delay = int(pacing.min_inter_job_delay_seconds)
    shared_runner = MarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider("ebay_browser"),
        config=cfg,
    )
    shared_runner._ebay_probe_mode = False  # noqa: SLF001

    card_reports: list[dict[str, Any]] = []
    stop_run = False
    stop_reason = None

    for idx in range(AUTHORISED_MAX):
        position = idx + 1
        report: dict[str, Any] = {
            "reliabilityPosition": position,
            "selection": persist_sel[idx] if idx < len(persist_sel) else {},
            "cardVerdict": None,
        }
        if stop_run or idx >= len(selections):
            report["cardVerdict"] = "NOT_ATTEMPTED_AFTER_STOP"
            card_reports.append(report)
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            continue

        sel = selections[idx]
        if accounting["liveNavigationStartedCount"] >= AUTHORISED_MAX:
            report["cardVerdict"] = "NOT_ATTEMPTED_AFTER_STOP"
            stop_run = True
            stop_reason = "authorised_max_reached"
            card_reports.append(report)
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            break

        # Preflight order (no attempt increment):
        # 1 marketplace gate 2 owned_daily off 3 no competing worker (gate)
        # 4-8 runtime/x11/self-check 9-12 identity/query/attemptId/scheduler
        g_ok, g_payload = _gate_ok()
        r_ok, r_payload = _pre_live_ok()
        report["preflight"] = {"marketplace": g_payload, "runtime": r_payload}
        if not g_ok or not r_ok:
            report["cardVerdict"] = "STOP_PREFLIGHT"
            stop_run = True
            stop_reason = f"preflight_denied_before_card_{position}"
            card_reports.append(report)
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            break

        kid = str(sel["priceKeyId"])
        before = _cache_snap(client.get_cache_row(price_key_id=kid) or {})
        report["before"] = before

        try:
            identity = _build_job_identity(client, sel)
        except Exception as exc:
            report["cardVerdict"] = "STOP_PREFLIGHT"
            report["identityError"] = f"{type(exc).__name__}:{exc}"
            stop_run = True
            stop_reason = f"query_identity_failed_before_card_{position}"
            card_reports.append(report)
            (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            break

        attempt_id = new_attempt_id()
        accounting["attemptIds"].append(attempt_id)
        report["identity"] = identity
        report["attemptId"] = attempt_id
        print(
            f"[reliability] CARD {position}/{AUTHORISED_MAX} {identity.get('card')} "
            f"key={kid} attempt={attempt_id} query={identity.get('query')!r}",
            flush=True,
        )

        os.environ["CARDSCANR_LIVE_ATTEMPT_ID"] = attempt_id
        os.environ["CARDSCANR_PRICE_KEY_ID"] = kid
        t0 = time.monotonic()
        payload: dict[str, Any] | None = None
        exc_text = None
        try:
            payload = run_forced_job(
                client,
                price_key_id=kid,
                reason=f"reliability_fresh_5:{sel.get('schedulerReason') or 'owned_daily'}",
                runner=shared_runner,
            )
        except Exception as exc:
            exc_text = f"{type(exc).__name__}:{exc}"
            payload = {
                "result": {"status": "failed", "error": exc_text},
                "before": before,
                "after": _cache_snap(client.get_cache_row(price_key_id=kid) or {}),
                "durationSec": round(time.monotonic() - t0, 1),
            }
        finally:
            os.environ.pop("CARDSCANR_LIVE_ATTEMPT_ID", None)
            os.environ.pop("CARDSCANR_PRICE_KEY_ID", None)

        result = payload.get("result") if isinstance(payload, dict) else {}
        result = result if isinstance(result, dict) else {}
        diag = _diag(result)

        # Authoritative consumption: durable SEARCH_SUBMISSION_STARTED only.
        search_started = has_search_submission_started(attempt_id)
        if search_started:
            accounting["liveNavigationStartedCount"] += 1
            if accounting["liveNavigationStartedCount"] > AUTHORISED_MAX:
                report["cardVerdict"] = "FAIL_LOCAL"
                stop_run = True
                stop_reason = "maximum_authorised_attempts_exceeded"
                card_reports.append(report)
                break

        challenge = is_structured_challenge(result, diag)
        accounting["terminalAttemptCount"] += 1 if search_started else 0
        after = payload.get("after") if isinstance(payload, dict) else _cache_snap(client.get_cache_row(price_key_id=kid) or {})
        report["after"] = after
        report["job"] = {
            "jobId": payload.get("jobId") if isinstance(payload, dict) else None,
            "durationSec": payload.get("durationSec") if isinstance(payload, dict) else None,
            "status": result.get("status"),
            "ownedDailyOutcome": result.get("ownedDailyOutcome") or result.get("outcomeClass"),
            "recommendedPrice": result.get("recommendedPrice"),
            "error": result.get("error") or exc_text,
            "postSoldCapturePhase": result.get("postSoldCapturePhase"),
            "x11SoldStateVerified": result.get("x11SoldStateVerified") or diag.get("x11SoldStateVerified"),
            "parsePhase": result.get("parsePhase"),
            "providerDiagnostics": result.get("providerDiagnostics"),
            "liveAttemptNumber": accounting["liveNavigationStartedCount"] if search_started else None,
            "searchSubmitted": search_started,
            "attemptId": attempt_id,
            "searchSubmissionEventPresent": search_started,
        }
        report["navigation"] = {
            "liveAttemptNumber": accounting["liveNavigationStartedCount"] if search_started else None,
            "searchSubmitted": search_started,
            "desktopNav": diag.get("desktopNav") or diag.get("linuxNav"),
            "finalUrl": (diag.get("desktopNav") or diag.get("linuxNav") or {}).get("url")
            or diag.get("finalUrl")
            or result.get("sourceUrl"),
        }
        report["challenge"] = {
            "activeChallenge": challenge,
            "providerOutcome": diag.get("providerOutcome"),
            "operationalStatus": diag.get("operationalStatus"),
            "structuredOnly": True,
        }

        verdict = classify_reliability_card_verdict(
            result,
            diag=diag,
            search_submission_started=search_started,
            preflight_failed=False,
        )
        if verdict == "PASS_PRICE_UPDATED":
            try:
                bp = before.get("price")
                ap = after.get("price") if isinstance(after, dict) else None
                if bp is not None and ap is not None and float(bp) == float(ap):
                    if str(result.get("ownedDailyOutcome") or "") == UNCHANGED_FROM_EBAY:
                        verdict = "PASS_PRICE_UNCHANGED"
                    elif str(result.get("ownedDailyOutcome") or "") != UPDATED_FROM_EBAY:
                        verdict = "PASS_PRICE_UNCHANGED"
            except (TypeError, ValueError):
                pass
        report["cardVerdict"] = verdict

        if challenge:
            accounting["challengeStopCount"] += 1
            stop_run = True
            stop_reason = f"STOP_CHALLENGE_card_{position}"
        elif verdict in {"PASS_PRICE_UPDATED", "PASS_PRICE_UNCHANGED", "SAFE_NO_NEW_EXACT_EVIDENCE"}:
            accounting["completedHealthyPathCount"] += 1
            if verdict == "SAFE_NO_NEW_EXACT_EVIDENCE":
                accounting["safeNoPriceCount"] += 1
        elif search_started:
            accounting["failedAttemptCount"] += 1
            stop_run = True
            stop_reason = f"{verdict}_card_{position}"
        else:
            stop_run = True
            stop_reason = f"{verdict}_card_{position}"

        _, g_after = _gate_ok()
        report["controlPlaneAfter"] = g_after
        card_reports.append(report)
        (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"CARD": position, "verdict": verdict, "accounting": accounting}, indent=2), flush=True)

        if stop_run:
            break
        if position < AUTHORISED_MAX:
            print(f"[reliability] pacing sleep {inter_delay}s before next card", flush=True)
            time.sleep(inter_delay)

    while len(card_reports) < AUTHORISED_MAX:
        position = len(card_reports) + 1
        report = {
            "reliabilityPosition": position,
            "cardVerdict": "NOT_ATTEMPTED_AFTER_STOP",
            "selection": persist_sel[position - 1] if position - 1 < len(persist_sel) else {},
        }
        card_reports.append(report)
        (CARDS_DIR / f"card_{position}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    run["cards"] = card_reports
    run["stopReason"] = stop_reason
    run["attemptAccounting"] = accounting
    healthy = accounting["completedHealthyPathCount"]
    if healthy == AUTHORISED_MAX and accounting["challengeStopCount"] == 0 and accounting["failedAttemptCount"] == 0:
        run["verdict"] = "5_CARD_RELIABILITY_PASS"
    elif accounting["liveNavigationStartedCount"] > AUTHORISED_MAX:
        run["verdict"] = "5_CARD_RELIABILITY_FAIL"
    else:
        run["verdict"] = "5_CARD_RELIABILITY_STOPPED_SAFE"

    _, g_final = _gate_ok()
    rt_final = probe_pre_live_runtime(cdp_port=9444)
    run["controlPlaneFinal"] = g_final
    run["runtimeFinal"] = rt_final
    run["finishedAtUtc"] = _utc()
    run["ownedDaily"] = {
        "flag": Path("reports/runtime/owned_daily_full_enable.flag").read_text(encoding="utf-8").strip(),
        "scheduler": "NOT_RUNNING",
        "worker": "NOT_RUNNING",
    }
    (OUT / "RUN_RESULT.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"VERDICT": run["verdict"], "accounting": accounting, "stopReason": stop_reason}, indent=2))
    return 0 if run["verdict"] != "5_CARD_RELIABILITY_FAIL" else 3


if __name__ == "__main__":
    raise SystemExit(main())
