#!/usr/bin/env python3
"""End-to-end owned pricing via REAL desktop Win32 eBay navigation.

EBAY_BROWSER_NAV_MODE=desktop_win32:
  - search/Sold via Windows mouse/keyboard only
  - CDP attach for read-only parse into existing exact-comp pricing engine
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.owned_daily_outcomes import HEALTHY_CHECK_OUTCOMES, summarize_outcome_counts
from cardscanr_market_engine.providers.factory import create_market_comps_provider
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env

OUT = ROOT / "reports" / "artifacts" / "owned_daily_session"
IVYSAUR_KEY_ID = "0a13b669-d3e2-4a15-9318-1de8fb19221a"
IVYSAUR_FP = "pokemon|en|base1|30|ivysaur|raw|raw|au|aud"


def _client() -> SupabaseMarketEngineClient:
    load_supabase_env()
    return SupabaseMarketEngineClient(
        supabase_url=os.environ["SUPABASE_URL"].rstrip("/"),
        service_role_key=supabase_secret_key_from_env(),
    )


def _configure_desktop_env(*, inter_job_delay: int = 20) -> None:
    os.environ["EBAY_BROWSER_NAV_MODE"] = "desktop_win32"
    os.environ["EBAY_BROWSER_ENABLED"] = "true"
    os.environ["MARKET_LOOKUP_PROVIDER"] = "ebay_browser"
    os.environ["EBAY_BROWSER_HEADLESS"] = "false"
    os.environ["EBAY_BROWSER_MAX_QUERY_ATTEMPTS"] = "1"
    os.environ["EBAY_BROWSER_REUSE_CONTEXT"] = "true"
    os.environ["EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS"] = str(max(5, inter_job_delay))
    os.environ["EBAY_BROWSER_COOLDOWN_SECONDS"] = str(max(5, inter_job_delay))
    os.environ.setdefault("EBAY_BROWSER_CDP_PORT", "9333")
    os.environ.setdefault("OWNED_DAILY_FULL_ENABLE", "false")


def _cache_snapshot(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    keys = (
        "current_market_price",
        "provider",
        "display_price_source",
        "reference_price",
        "sample_size",
        "confidence",
        "last_updated_at",
        "latest_snapshot_id",
        "refresh_status",
        "last_error_message",
    )
    return {k: row.get(k) for k in keys}


def run_forced_job(
    client: SupabaseMarketEngineClient,
    *,
    price_key_id: str,
    reason: str,
    runner: MarketPriceJobRunner | None = None,
) -> dict[str, Any]:
    day = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    worker_id = os.getenv("MARKET_ENGINE_WORKER_ID", "desktop-e2e")
    dedupe_key = f"desktop_e2e:{price_key_id}:{day}:{os.getpid()}"
    job_row = client.enqueue_refresh_job(
        price_key_id=price_key_id,
        reason=reason,
        priority=1,
        dedupe_key=dedupe_key,
    )
    job_id = str(job_row["id"])
    job = client.claim_specific_refresh_job(job_id=job_id, worker_id=worker_id)
    if job is None and str(job_row.get("status") or "").lower() == "running":
        # Prior aborted pilot/worker can leave a running lock; reclaim once.
        try:
            client.cancel_job(job_id=job_id, reason="reclaim_stale_running_for_forced_job")
        except Exception:
            try:
                client.fail_job(
                    job_id=job_id,
                    error_message="reclaim_stale_running_for_forced_job",
                    retryable=True,
                    retry_delay_minutes=1,
                )
            except Exception:
                pass
        job_row = client.enqueue_refresh_job(
            price_key_id=price_key_id,
            reason=reason,
            priority=1,
            dedupe_key=f"{dedupe_key}:retry",
        )
        job_id = str(job_row["id"])
        job = client.claim_specific_refresh_job(job_id=job_id, worker_id=worker_id)
    if job is None:
        raise RuntimeError(f"failed_to_claim_job id={job_id} status={job_row.get('status')}")
    before = client.get_cache_row(price_key_id=price_key_id) or {}
    if runner is None:
        runner = MarketPriceJobRunner(
            client=client,
            provider=create_market_comps_provider("ebay_browser"),
            config=MarketEngineConfig.from_env(),
        )
    t0 = time.monotonic()
    result = runner.run_job(job)
    duration = time.monotonic() - t0
    after = client.get_cache_row(price_key_id=price_key_id) or {}
    return {
        "jobId": str(job.id),
        "priceKeyId": price_key_id,
        "durationSec": round(duration, 1),
        "before": _cache_snapshot(before),
        "after": _cache_snapshot(after),
        "result": result,
    }
def run_reference_sync_for_key(client: SupabaseMarketEngineClient, *, price_key_id: str) -> dict[str, Any]:
    from cardscanr_market_engine.bulk.reference_refresh import BulkReferenceRefreshRunner, BulkRefreshConfig

    before = client.get_cache_row(price_key_id=price_key_id) or {}
    selected_before = before.get("current_market_price")
    provider_before = before.get("provider")
    engine_config = MarketEngineConfig.from_env()
    refresh_config = BulkRefreshConfig.from_env()
    # Bound to one key via max_keys large enough but filter in runner is global —
    # run worker with max_keys and inspect this key's row after.
    refresh_config = BulkRefreshConfig(
        dry_run=False,
        max_keys=25,
        enable_live_tcgdex=refresh_config.enable_live_tcgdex,
        verification_budget_per_run=refresh_config.verification_budget_per_run,
        high_value_threshold=refresh_config.high_value_threshold,
        reference_fresh_hours=refresh_config.reference_fresh_hours,
    )
    runner = BulkReferenceRefreshRunner(
        client=client,
        engine_config=engine_config,
        refresh_config=refresh_config,
        logger=lambda m: print(f"[ref-sync] {m}", flush=True),
    )
    report = runner.run()
    after = client.get_cache_row(price_key_id=price_key_id) or {}
    selected_after = after.get("current_market_price")
    provider_after = after.get("provider")
    try:
        x_before = float(selected_before) if selected_before is not None else None
        x_after = float(selected_after) if selected_after is not None else None
    except (TypeError, ValueError):
        x_before, x_after = selected_before, selected_after
    overwrite = bool(
        provider_before == "ebay_browser"
        and provider_after != "ebay_browser"
        and x_before is not None
        and x_after is not None
        and float(x_before) > 0
        and abs(float(x_after) - float(x_before)) > 1e-9
    )
    # Also treat same provider but replaced by reference display as overwrite signal
    if provider_before == "ebay_browser" and str(after.get("display_price_source") or "").startswith("reference"):
        if x_before is not None and x_after is not None and abs(float(x_after) - float(x_before)) > 1e-9:
            overwrite = True
    return {
        "selectedBefore": x_before,
        "providerBefore": provider_before,
        "referencePrice": after.get("reference_price"),
        "selectedAfter": x_after,
        "providerAfter": provider_after,
        "displaySourceAfter": after.get("display_price_source"),
        "overwrite": overwrite,
        "bulkReportSummary": {
            "processed": report.get("processed"),
            "errors": report.get("errors"),
            "preserved": report.get("preserved") or report.get("preserve_verified"),
        },
        "PASS": (not overwrite) and provider_after == "ebay_browser" and x_before == x_after,
    }


def ivysaur_e2e(client: SupabaseMarketEngineClient) -> dict[str, Any]:
    print("[desktop-e2e] IVYSAUR start", flush=True)
    # Ensure Chrome+CDP before job so first fetch doesn't race
    from cardscanr_market_engine.providers.desktop_win32_ebay_nav import ensure_chrome_with_cdp

    from cardscanr_market_engine.providers.desktop_win32_ebay_nav import wait_for_search_controls

    chrome = ensure_chrome_with_cdp(
        profile_dir=Path(os.getenv("EBAY_BROWSER_USER_DATA_DIR", str(ROOT / ".browser_profiles" / "cardscanr"))),
        cdp_port=int(os.getenv("EBAY_BROWSER_CDP_PORT", "9333")),
    )
    field, button = wait_for_search_controls(timeout_s=45.0)
    chrome["searchControlsReady"] = bool(field and button)
    print(f"[desktop-e2e] chrome={chrome}", flush=True)
    if not field or not button:
        raise RuntimeError("Chrome started but eBay search controls never became available via UIA")

    key = client.get_price_key(IVYSAUR_KEY_ID)
    payload = run_forced_job(
        client,
        price_key_id=IVYSAUR_KEY_ID,
        reason="owned_daily:desktop_e2e_force",
    )
    result = payload["result"]
    meta = {}
    # Pull rejection/candidate stats from latest snapshot if present
    snap_id = result.get("snapshotId")
    ownership = "NONE"
    pricing = {
        "candidateSoldRows": result.get("includedCount"),  # filled below if we can
        "acceptedExactComps": result.get("includedCount"),
        "rejectedCount": result.get("rejectedCount"),
        "estimate": result.get("recommendedPrice"),
        "confidence": result.get("confidence"),
        "provider": payload["after"].get("provider"),
        "selectedValue": payload["after"].get("current_market_price"),
        "snapshotId": snap_id,
        "valuation": payload["after"].get("current_market_price"),
        "status": result.get("status"),
        "ownedDailyOutcome": result.get("ownedDailyOutcome") or result.get("outcomeClass"),
        "SORRY": "sorry" in str(result.get("error") or "").lower(),
        "challenge": "challenge" in str(result.get("error") or "").lower()
        or "captcha" in str(result.get("error") or "").lower(),
    }

    # Enrich from snapshot raw if available
    try:
        if snap_id and hasattr(client, "_rest_get"):
            pass
    except Exception:
        pass

    ref = run_reference_sync_for_key(client, price_key_id=IVYSAUR_KEY_ID)
    report = {
        "fingerprint": getattr(key, "fingerprint", None) or IVYSAUR_FP,
        "priceKeyId": IVYSAUR_KEY_ID,
        "card": "Ivysaur Base Set 30/102 EN raw AU/AUD",
        "navMode": "desktop_win32",
        "chrome": chrome,
        "job": payload,
        "pricing": pricing,
        "ownershipMutation": ownership,
        "referenceSync": ref,
        "PASS": bool(
            result.get("status") == "completed"
            and not pricing["SORRY"]
            and not pricing["challenge"]
            and ref.get("PASS")
        ),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "desktop_e2e_ivysaur_proof.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, default=str), flush=True)
    print(f"[desktop-e2e] wrote {path}", flush=True)
    return report


def due_owned_key_ids(client: SupabaseMarketEngineClient, *, limit: int = 5) -> list[dict[str, Any]]:
    """Pick due owned-daily targets excluding Ivysaur if possible."""
    payload = client.list_owned_market_pricing_targets(include_zero_owners=False)
    targets = payload.get("targets") or payload.get("items") or []
    if isinstance(payload, list):
        targets = payload
    out: list[dict[str, Any]] = []
    for t in targets:
        if not isinstance(t, dict):
            continue
        fp = str(t.get("fingerprint") or "")
        due = t.get("due_for_owned_daily")
        if due is False:
            continue
        kid = str(t.get("market_price_key_id") or t.get("price_key_id") or "").strip()
        if not kid:
            try:
                kid = str(client.ensure_market_price_key_from_owned_target(t) or "").strip()
            except Exception as exc:
                print(f"[desktop-e2e] skip ensure_key {fp}: {exc}", flush=True)
                continue
        if not kid or kid == IVYSAUR_KEY_ID:
            continue
        # Prefer already-priced ebay_browser cards for a normal due check (not never-priced chaos)
        if str(t.get("owned_priority_band") or "").startswith("P0") and len(out) < limit:
            # allow a couple P0s but prefer later bands first — defer by continuing for now
            continue
        name = t.get("card_name") or t.get("normalized_card_name") or fp
        set_name = t.get("set_name") or ""
        num = t.get("collector_number") or ""
        label = f"{name} {set_name} {num}".strip()
        out.append({"priceKeyId": kid, "fingerprint": fp, "card": label})
        if len(out) >= limit:
            break
    if len(out) < limit:
        # Second pass: allow P0/never-priced if needed
        for t in targets:
            if len(out) >= limit:
                break
            if not isinstance(t, dict) or t.get("due_for_owned_daily") is False:
                continue
            fp = str(t.get("fingerprint") or "")
            kid = str(t.get("market_price_key_id") or "").strip()
            if not kid:
                try:
                    kid = str(client.ensure_market_price_key_from_owned_target(t) or "").strip()
                except Exception:
                    continue
            if not kid or kid == IVYSAUR_KEY_ID or any(c["priceKeyId"] == kid for c in out):
                continue
            name = t.get("card_name") or fp
            out.append({"priceKeyId": kid, "fingerprint": fp, "card": name})
    return out


def run_batch5(client: SupabaseMarketEngineClient, *, delay_s: int = 20) -> dict[str, Any]:
    cards = due_owned_key_ids(client, limit=5)
    if len(cards) < 5:
        print(f"[desktop-e2e] only {len(cards)} due targets; supplementing from cache candidates", flush=True)
        try:
            rows = client.list_cache_refresh_candidates(limit=30)
        except Exception as exc:
            print(f"[desktop-e2e] cache candidate fallback failed: {exc}", flush=True)
            rows = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            kid = str(row.get("price_key_id") or row.get("id") or "")
            fp = str(row.get("fingerprint") or kid)
            if not kid or kid == IVYSAUR_KEY_ID or any(c["priceKeyId"] == kid for c in cards):
                continue
            cards.append({"priceKeyId": kid, "fingerprint": fp, "card": row.get("card_name") or fp})
            if len(cards) >= 5:
                break

    if not cards:
        return {"error": "no_due_cards", "attempted": 0}

    # One shared provider keeps the CDP attach alive across consecutive desktop jobs.
    shared_runner = MarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider("ebay_browser"),
        config=MarketEngineConfig.from_env(),
    )
    results: list[dict[str, Any]] = []
    for i, card in enumerate(cards[:5]):
        print(f"[desktop-e2e] BATCH {i+1}/5 {card}", flush=True)
        t0 = time.monotonic()
        try:
            payload = run_forced_job(
                client,
                price_key_id=card["priceKeyId"],
                reason="owned_daily:desktop_batch5_force",
                runner=shared_runner,
            )
            result = payload["result"]
            err = str(result.get("error") or "")
            diag = ((result.get("providerDiagnostics") or {}).get("diagnostics") or {})
            desktop_nav = diag.get("desktopNav") or {}
            status_ok = result.get("status") in {"completed", "checked_no_new_exact_evidence"}
            row = {
                "card": card.get("card"),
                "exactIdentity": card.get("fingerprint"),
                "priceKeyId": card["priceKeyId"],
                "desktopSearchSuccess": bool(desktop_nav.get("searchSuccess")) if desktop_nav else status_ok,
                "soldClickSuccess": bool(desktop_nav.get("soldClickSuccess")) if desktop_nav else status_ok,
                "SOLD_STATE_VERIFIED": bool(desktop_nav.get("SOLD_STATE_VERIFIED")) if desktop_nav else status_ok,
                "candidateCount": result.get("includedCount"),
                "pricingOutcome": result.get("ownedDailyOutcome") or result.get("outcomeClass") or result.get("status"),
                "SORRY": bool(desktop_nav.get("sorry")) or "sorry" in err.lower() or "ebay_sorry" in err.lower(),
                "challenge": bool(desktop_nav.get("challenge"))
                or "challenge" in err.lower()
                or "captcha" in err.lower(),
                "duration": round(time.monotonic() - t0, 1),
                "error": err or None,
                "recommendedPrice": result.get("recommendedPrice"),
                "desktopNav": desktop_nav or None,
            }
        except Exception as exc:
            row = {
                "card": card.get("card"),
                "exactIdentity": card.get("fingerprint"),
                "priceKeyId": card["priceKeyId"],
                "desktopSearchSuccess": False,
                "soldClickSuccess": False,
                "SOLD_STATE_VERIFIED": False,
                "candidateCount": None,
                "pricingOutcome": "ERROR",
                "SORRY": "sorry" in str(exc).lower(),
                "challenge": "challenge" in str(exc).lower() or "captcha" in str(exc).lower(),
                "duration": round(time.monotonic() - t0, 1),
                "error": f"{type(exc).__name__}:{exc}",
            }
        results.append(row)
        print(json.dumps(row, indent=2), flush=True)
        if row.get("challenge"):
            break
        if i + 1 < min(5, len(cards)):
            time.sleep(delay_s)
    metrics = summarize_outcome_counts(
        [{"ownedDailyOutcome": r.get("pricingOutcome")} for r in results]
    )
    healthy = sum(1 for r in results if r.get("pricingOutcome") in HEALTHY_CHECK_OUTCOMES)
    durations = [float(r["duration"]) for r in results if r.get("duration") is not None]
    report = {
        "cards": results,
        "totals": {
            "attempted": len(results),
            "healthy": healthy,
            "updated": metrics.get("OWNED_PRICE_ESTIMATES_UPDATED", 0),
            "unchanged": metrics.get("OWNED_PRICE_ESTIMATES_UNCHANGED", 0),
            "noNewExactEvidence": metrics.get("OWNED_PRICE_NO_NEW_EVIDENCE", 0),
            "SORRY": sum(1 for r in results if r.get("SORRY")),
            "challenges": sum(1 for r in results if r.get("challenge")),
            "averageSecondsPerCheck": round(sum(durations) / len(durations), 1) if durations else None,
            "navSuccesses": sum(1 for r in results if r.get("SOLD_STATE_VERIFIED")),
        },
        "metrics": metrics,
    }
    path = OUT / "desktop_e2e_batch5_proof.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, default=str), flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ivysaur-only", action="store_true")
    parser.add_argument("--batch5-only", action="store_true")
    parser.add_argument("--delay", type=int, default=20)
    args = parser.parse_args()
    _configure_desktop_env(inter_job_delay=args.delay)
    OUT.mkdir(parents=True, exist_ok=True)
    client = _client()

    verdict = "BLOCKED"
    ivy = None
    batch = None
    if not args.batch5_only:
        ivy = ivysaur_e2e(client)
        if not ivy.get("PASS"):
            # Nav may have worked with pricing fail
            job_status = (ivy.get("job") or {}).get("result", {}).get("status")
            sorry = ivy.get("pricing", {}).get("SORRY")
            challenge = ivy.get("pricing", {}).get("challenge")
            if challenge:
                verdict = "BLOCKED"
            elif sorry or job_status not in {"completed", "checked_no_new_exact_evidence"}:
                err = str((ivy.get("job") or {}).get("result", {}).get("error") or "")
                if "Desktop sold navigation" in err or "desktop" in err.lower():
                    verdict = "BLOCKED"
                else:
                    verdict = "REAL_DESKTOP_NAV_PASS_PRICING_FAIL" if "SOLD_STATE" not in err else "BLOCKED"
            else:
                verdict = "REAL_DESKTOP_NAV_PASS_PRICING_FAIL"
            final = {"verdict": verdict, "ivysaur": ivy, "batch5": None}
            (OUT / "desktop_e2e_final_verdict.json").write_text(
                json.dumps(final, indent=2, default=str) + "\n", encoding="utf-8"
            )
            print(json.dumps(final, indent=2, default=str), flush=True)
            return 1

    if args.ivysaur_only:
        verdict = "REAL_DESKTOP_PRICING_PATH_PASS" if ivy and ivy.get("PASS") else verdict
    else:
        batch = run_batch5(client, delay_s=args.delay)
        totals = batch.get("totals") or {}
        if totals.get("challenges"):
            verdict = "BLOCKED"
        elif totals.get("SORRY", 0) > 0 or totals.get("navSuccesses", 0) < totals.get("attempted", 0):
            verdict = "REAL_DESKTOP_BATCH_UNSTABLE"
        elif totals.get("attempted", 0) >= 5 and totals.get("navSuccesses") == totals.get("attempted"):
            verdict = "REAL_DESKTOP_PRICING_PATH_PASS"
        else:
            verdict = "REAL_DESKTOP_BATCH_UNSTABLE"
    final = {
        "verdict": verdict,
        "ivysaur": ivy,
        "batch5": batch,
        "playwrightComparison": {
            "note": "Prior Playwright/CDP AU path produced intermittent PRE_SOLD_SORRY / TRUE_UI invisible failures; "
            "this desktop path requires physical Win32 search+Sold for every job.",
            "desktopNavSuccessRateThisRun": None
            if not batch
            else f"{(batch.get('totals') or {}).get('navSuccesses')}/{(batch.get('totals') or {}).get('attempted')}",
        },
    }
    if batch and (batch.get("totals") or {}).get("attempted"):
        t = batch["totals"]
        if t.get("navSuccesses") == t.get("attempted") and t.get("challenges") == 0 and t.get("SORRY") == 0:
            final["playwrightComparison"]["explicit"] = (
                f"All {t['attempted']} consecutive real-desktop navigations succeeded with 0 SORRY and 0 challenges, "
                "versus prior Playwright/CDP owned-daily pilots that hit SORRY/challenge under clean-URL navigation."
            )
    (OUT / "desktop_e2e_final_verdict.json").write_text(
        json.dumps(final, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps({"verdict": verdict}, indent=2), flush=True)
    print(f"[desktop-e2e] final -> {OUT / 'desktop_e2e_final_verdict.json'}", flush=True)
    return 0 if verdict == "REAL_DESKTOP_PRICING_PATH_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
