#!/usr/bin/env python3
"""25-card Linux X11 GUI capacity/reliability pilot (owned_daily stays disabled).

Counts only actual GUI attempts. SKIPPED_ALREADY_FRESH does not count.
On TEMPORARY_EBAY_SERVER_FAILURE / SORRY: stop batch, 15-minute cooldown, resume.
On EBAY_CHALLENGE_REQUIRED or local GUI defect: STOP completely.
"""
from __future__ import annotations

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
from cardscanr_market_engine.failure_policy import DEFAULT_EBAY_TRANSIENT_COOLDOWN_MINUTES
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.marketplace_ops_state import (
    clear_marketplace_cooldown,
    get_active_cooldown,
)
from cardscanr_market_engine.owned_daily_outcomes import (
    CHECKED_NO_NEW_EXACT_EVIDENCE,
    TEMPORARY_EBAY_SERVER_FAILURE,
    UNCHANGED_FROM_EBAY,
    UPDATED_FROM_EBAY,
)
from cardscanr_market_engine.providers.factory import create_market_comps_provider
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env
from tools.desktop_ebay_e2e_pricing import due_owned_key_ids, run_forced_job
from tools.linux_x11_ebay_batch10 import (
    latest_nav_pair,
    parse_fp,
    sample_resources,
)

OUT = ROOT / "reports" / "artifacts" / "linux_gui_batch25"
ART = ROOT / "reports" / "artifacts"
OUT.mkdir(parents=True, exist_ok=True)

HEALTHY = {
    UPDATED_FROM_EBAY,
    UNCHANGED_FROM_EBAY,
    CHECKED_NO_NEW_EXACT_EVIDENCE,
    "completed",
    "checked_no_new_exact_evidence",
}


def _configure(*, delay: int = 20) -> None:
    os.environ["EBAY_BROWSER_NAV_MODE"] = "linux_x11"
    os.environ["EBAY_BROWSER_ENABLED"] = "true"
    os.environ["MARKET_LOOKUP_PROVIDER"] = "ebay_browser"
    os.environ["EBAY_BROWSER_HEADLESS"] = "false"
    os.environ["EBAY_BROWSER_MAX_QUERY_ATTEMPTS"] = "1"
    os.environ["EBAY_BROWSER_REUSE_CONTEXT"] = "true"
    os.environ["EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS"] = str(max(5, delay))
    os.environ["EBAY_BROWSER_COOLDOWN_SECONDS"] = str(max(5, delay))
    os.environ["EBAY_BROWSER_CDP_PORT"] = os.environ.get("EBAY_BROWSER_CDP_PORT", "9444")
    os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"


def _client() -> SupabaseMarketEngineClient:
    load_supabase_env()
    return SupabaseMarketEngineClient(
        supabase_url=os.environ["SUPABASE_URL"].rstrip("/"),
        service_role_key=supabase_secret_key_from_env(),
    )


def workload_snapshot(client: SupabaseMarketEngineClient) -> dict[str, Any]:
    payload = client.list_owned_market_pricing_targets(include_zero_owners=False)
    targets = payload.get("targets") or payload.get("items") or []
    if isinstance(payload, list):
        targets = payload
    users = set()
    fps = set()
    contexts = 0
    due = 0
    fresh = 0
    bands: dict[str, int] = {}
    for t in targets:
        if not isinstance(t, dict):
            continue
        contexts += 1
        fp = str(t.get("fingerprint") or "")
        if fp:
            fps.add(fp)
        for u in t.get("owner_user_ids") or []:
            users.add(str(u))
        if t.get("owner_count"):
            pass
        band = str(t.get("owned_priority_band") or ("DUE" if t.get("due_for_owned_daily") else "FRESH"))
        bands[band] = bands.get(band, 0) + 1
        if t.get("due_for_owned_daily") is False:
            fresh += 1
        else:
            due += 1
    return {
        "users": len(users) or None,
        "uniqueOwnedPrintings": len(fps),
        "printingMarketContexts": contexts,
        "currentlyDue": due,
        "freshSkipped": fresh,
        "bands": bands,
        "ownerCountSum": sum(int(t.get("owner_count") or 0) for t in targets if isinstance(t, dict)),
    }


def capacity_model(*, seconds_per_check: float, due: int) -> dict[str, Any]:
    # Conservative: ~15% overhead for skips/cooldowns/maintenance.
    raw_per_hour = 3600.0 / max(1.0, seconds_per_check)
    effective_per_hour = raw_per_hour * 0.85
    h8 = int(effective_per_hour * 8)
    h16 = int(effective_per_hour * 16)
    return {
        "observedSecondsPerCheck": round(seconds_per_check, 1),
        "estimatedChecksPerHour": round(raw_per_hour, 1),
        "conservativeChecksPerHour": round(effective_per_hour, 1),
        "capacity8h": h8,
        "capacity16h": h16,
        "requiredDailyOpportunities": due,
        "margin16h": h16 - due,
        "projectedAbleToClear": h16 >= due,
    }


def _is_transient_ebay(outcome: str, sorry: bool, err: str | None, search: dict | None) -> bool:
    if sorry:
        return True
    blob = f"{outcome} {err or ''} {((search or {}).get('classification') or '')}".upper()
    return "TEMPORARY_EBAY_SERVER_FAILURE" in blob or "EBAY_SORRY" in blob


def _is_local_gui_fail(
    *,
    search: dict | None,
    sold: dict | None,
    about_blank: bool,
    challenge: bool,
    query_confirmed: bool,
    search_ok: bool,
    sold_ok: bool,
    sorry: bool,
) -> str | None:
    if challenge:
        return None  # handled separately
    if sorry:
        return None
    cls = str((search or {}).get("classification") or (search or {}).get("error") or "")
    # eBay Live / alternate surfaces are not local GUI failures.
    if (
        cls
        in {
            "EBAY_LIVE_RESULTS",
            "ALTERNATE_EBAY_SURFACE",
            "SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE",
        }
        or "ebaylive" in str((search or {}).get("url") or "").lower()
        or bool((search or {}).get("ebayLive"))
        or bool((search or {}).get("alternateSurface"))
    ):
        return None
    if about_blank:
        return "ABOUT_BLANK"
    if (search or {}).get("error") == "SEARCH_INPUT_NOT_CONFIRMED" and not query_confirmed:
        return "SEARCH_INPUT_NOT_CONFIRMED"
    if query_confirmed and search is not None and not search_ok and not sorry:
        # post-submit non-SORRY failure may still be local if results never confirmed
        # without error page — treat as local only when classification says so
        if cls in {"SEARCH_INPUT_NOT_CONFIRMED"}:
            return "SEARCH_INPUT_NOT_CONFIRMED"
    if search_ok and sold is not None and not sold_ok:
        phase = str((sold or {}).get("phase") or "")
        if phase in {"SOLD_NAVIGATION_TIMEOUT", "ABOUT_BLANK_ABORT"} or phase.startswith("SOLD"):
            if phase != "EBAY_SORRY" and "TEMPORARY" not in phase:
                return phase or "SOLD_FAILURE"
    return None


def main() -> int:
    target = int(os.getenv("LINUX_GUI_BATCH25_TARGET", os.getenv("LINUX_GUI_BATCH_TARGET", "25")))
    if target < 25 and os.getenv("LINUX_GUI_BATCH25_ALLOW_SMALL", "").strip() not in {"1", "true", "yes"}:
        # Protect against inheriting LINUX_GUI_BATCH_TARGET=10 from earlier gates.
        target = 25
    delay = int(os.getenv("LINUX_GUI_BATCH_DELAY", "20"))
    cooldown_min = int(
        os.getenv("EBAY_TRANSIENT_FAILURE_COOLDOWN_MINUTES", str(DEFAULT_EBAY_TRANSIENT_COOLDOWN_MINUTES))
    )
    _configure(delay=delay)
    client = _client()

    workload = workload_snapshot(client)
    capacity = capacity_model(seconds_per_check=52.2, due=int(workload.get("currentlyDue") or 0))

    pool = due_owned_key_ids(client, limit=max(80, target * 5))
    if len(pool) < target:
        try:
            rows = client.list_cache_refresh_candidates(limit=120)
        except Exception:
            rows = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            kid = str(row.get("price_key_id") or row.get("id") or "")
            if not kid or any(c["priceKeyId"] == kid for c in pool):
                continue
            pool.append(
                {
                    "priceKeyId": kid,
                    "fingerprint": str(row.get("fingerprint") or kid),
                    "card": row.get("card_name") or kid,
                }
            )

    report: dict[str, Any] = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "delaySec": delay,
        "targetGuiAttempts": target,
        "cooldownMinutes": cooldown_min,
        "workload": workload,
        "capacity": capacity,
        "startState": sample_resources(),
        "ownedDailyFullEnable": os.environ.get("OWNED_DAILY_FULL_ENABLE"),
        "skippedFresh": [],
        "results": [],
        "cooldownEvents": [],
        "resources": {"samples": []},
        "stopReason": None,
    }
    (OUT / "report_partial.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    from cardscanr_market_engine.providers.linux_x11_ebay_nav import ensure_chrome_with_cdp

    print("[linux-x11-25] ensuring CDP (no forced restart if healthy)", flush=True)
    ensure_chrome_with_cdp(cdp_port=int(os.environ["EBAY_BROWSER_CDP_PORT"]))

    # Honor any active TRANSIENT_EBAY cooldown from the prior Iron Bundle SORRY.
    active = get_active_cooldown("AU")
    if active is not None and active.reason == "TRANSIENT_EBAY":
        wait_s = max(0, int((active.until - datetime.now(timezone.utc)).total_seconds()))
        if wait_s > 0:
            print(f"[linux-x11-25] waiting existing TRANSIENT_EBAY cooldown {wait_s}s", flush=True)
            report["cooldownEvents"].append(
                {"reason": "preexisting_TRANSIENT_EBAY", "waitSec": wait_s, "until": active.until.isoformat()}
            )
            time.sleep(wait_s)
            clear_marketplace_cooldown("AU")

    runner = MarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider("ebay_browser"),
        config=MarketEngineConfig.from_env(),
    )

    gui_n = 0
    stop = False
    chrome_restarts = 0
    auth_loss = 0
    chrome_rss_peak = float(report["startState"]["wsl"].get("CHROME_RSS_MB") or 0)
    cpu_peak = float(report["startState"]["wsl"].get("CPU_LOAD") or 0)
    loads: list[float] = []
    transient_count = 0
    delayed_retry_success = 0
    seen_failed_ids: set[str] = set()

    idx = 0
    while gui_n < target and not stop and idx < len(pool):
        card = pool[idx]
        idx += 1
        # After cooldown, skip immediately re-attempting the same failed printing.
        if card["priceKeyId"] in seen_failed_ids:
            continue
        fp_meta = parse_fp(str(card.get("fingerprint") or ""))
        print(f"[linux-x11-25] candidate {card.get('card')}", flush=True)
        t_card0 = time.time()
        t0 = time.monotonic()
        try:
            payload = run_forced_job(
                client,
                price_key_id=card["priceKeyId"],
                reason="linux_gui:batch25_pilot",
                runner=runner,
            )
        except Exception as exc:
            msg = str(exc)
            if "failed_to_claim_job" in msg and "running" in msg.lower():
                print(f"[linux-x11-25] skip reclaim-miss {card.get('card')}: {exc}", flush=True)
                report.setdefault("claimSkips", []).append(
                    {"card": card.get("card"), "priceKeyId": card["priceKeyId"], "error": msg}
                )
                continue
            row = {
                "guiIndex": gui_n + 1,
                "card": card.get("card"),
                "set": fp_meta["set"],
                "collector": fp_meta["collector"],
                "fingerprint": card.get("fingerprint"),
                "priceKeyId": card["priceKeyId"],
                "elapsedSec": round(time.monotonic() - t0, 1),
                "pricingOutcome": "ERROR",
                "error": msg,
                "PASS": False,
            }
            report["results"].append(row)
            report["stopReason"] = f"exception:{exc}"
            stop = True
            break

        result = payload["result"]
        status = str(result.get("status") or "")
        outcome = str(
            result.get("ownedDailyOutcome")
            or result.get("checkOutcome")
            or result.get("outcome")
            or status
        )
        err = str(result.get("error") or "") or None

        if status == "skipped_already_fresh" or outcome == "skipped_already_fresh":
            report["skippedFresh"].append(
                {
                    "card": card.get("card"),
                    "priceKeyId": card["priceKeyId"],
                    "fingerprint": card.get("fingerprint"),
                }
            )
            print(f"[linux-x11-25] SKIPPED_ALREADY_FRESH {card.get('card')}", flush=True)
            continue

        gui_n += 1
        search, sold = latest_nav_pair(t_card0)
        about_blank = bool(
            (search or {}).get("aboutBlank")
            or (sold or {}).get("aboutBlank")
            or "about:blank" in str((sold or {}).get("url") or "").lower()
        )
        search_ok = bool((search or {}).get("ok"))
        query_confirmed = (search or {}).get("phase") in {
            "QUERY_VISIBLE_CONFIRMED",
            "SEARCH_SUBMITTED",
            "SEARCH_RESULTS_CONFIRMED",
            "TEMPORARY_EBAY_SERVER_FAILURE",
        } or bool((search or {}).get("queryVisibleConfirmed")) or bool((search or {}).get("ok"))
        sold_ok = bool((sold or {}).get("SOLD_STATE_VERIFIED") or (sold or {}).get("ok"))
        sorry = bool((search or {}).get("sorry") or (sold or {}).get("sorry")) or _is_transient_ebay(
            outcome, False, err, search
        )
        challenge = bool((search or {}).get("challenge") or (sold or {}).get("challenge")) or (
            "challenge" in (err or "").lower()
        )
        pricing_healthy = outcome in HEALTHY or status in HEALTHY
        if pricing_healthy and search is None and sold is None and not err:
            search_ok = query_confirmed = sold_ok = True

        res = sample_resources()
        report["resources"]["samples"].append(res)
        try:
            chrome_rss_peak = max(chrome_rss_peak, float(res["wsl"].get("CHROME_RSS_MB") or 0))
            load = float(res["wsl"].get("CPU_LOAD") or 0)
            loads.append(load)
            cpu_peak = max(cpu_peak, load)
        except Exception:
            pass

        transient = _is_transient_ebay(outcome, sorry, err, search)
        if outcome == TEMPORARY_EBAY_SERVER_FAILURE:
            transient = True
            sorry = True

        before_price = (payload.get("before") or {}).get("current_market_price")
        after_price = (payload.get("after") or {}).get("current_market_price")
        last_good_retained = True
        if transient or not pricing_healthy:
            last_good_retained = before_price == after_price or (
                before_price is not None and float(before_price or 0) > 0 and after_price == before_price
            )
        freshness_advanced = pricing_healthy and not transient

        row = {
            "guiIndex": gui_n,
            "card": card.get("card"),
            "set": fp_meta["set"],
            "collector": fp_meta["collector"],
            "fingerprint": card.get("fingerprint"),
            "priceKeyId": card["priceKeyId"],
            "search": "PASS" if (search_ok or (query_confirmed and transient)) else "FAIL",
            "queryConfirmed": bool(query_confirmed),
            "Sold": "PASS" if sold_ok else ("n/a" if transient else "FAIL"),
            "SOLD_STATE_VERIFIED": bool(sold_ok),
            "candidates": result.get("includedCount"),
            "acceptedComps": result.get("includedCount"),
            "rejectedComps": result.get("rejectedCount"),
            "pricingOutcome": TEMPORARY_EBAY_SERVER_FAILURE if transient else outcome,
            "value": after_price,
            "beforeValue": before_price,
            "SORRY": bool(sorry or transient),
            "challenge": challenge,
            "aboutBlank": about_blank,
            "unexpectedTab": about_blank,
            "elapsedSec": round(time.monotonic() - t0, 1),
            "error": err,
            "searchPhase": (search or {}).get("phase"),
            "soldPhase": (sold or {}).get("phase"),
            "soldClick": (sold or {}).get("click"),
            "searchUrl": (search or {}).get("url"),
            "soldUrl": (sold or {}).get("url"),
            "lastGoodRetained": last_good_retained,
            "freshnessAdvanced": freshness_advanced,
            "before": payload.get("before"),
            "after": payload.get("after"),
        }
        local_fail = _is_local_gui_fail(
            search=search,
            sold=sold,
            about_blank=about_blank,
            challenge=challenge,
            query_confirmed=bool(query_confirmed),
            search_ok=search_ok,
            sold_ok=sold_ok,
            sorry=bool(sorry or transient),
        )
        nav_healthy = (
            (search_ok or query_confirmed)
            and sold_ok
            and not sorry
            and not challenge
            and not about_blank
            and pricing_healthy
            and not transient
        )
        row["PASS"] = bool(nav_healthy)
        row["localGuiFailure"] = local_fail
        report["results"].append(row)
        (OUT / "report_partial.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            f"[linux-x11-25] GUI {gui_n}/{target} {row['card']} outcome={row['pricingOutcome']} PASS={row['PASS']}",
            flush=True,
        )

        if challenge:
            report["stopReason"] = "EBAY_CHALLENGE_REQUIRED"
            stop = True
            break

        if local_fail:
            report["stopReason"] = f"LOCAL_GUI:{local_fail}"
            stop = True
            break

        if transient:
            transient_count += 1
            seen_failed_ids.add(card["priceKeyId"])
            report["cooldownEvents"].append(
                {
                    "guiIndex": gui_n,
                    "card": card.get("card"),
                    "priceKeyId": card["priceKeyId"],
                    "waitMinutes": cooldown_min,
                    "classification": TEMPORARY_EBAY_SERVER_FAILURE,
                }
            )
            if transient_count >= 2:
                report["stopReason"] = "REPEATED_TEMPORARY_EBAY_SERVER_FAILURE"
                stop = True
                break
            print(f"[linux-x11-25] SORRY — cooldown {cooldown_min}m then resume next due card", flush=True)
            time.sleep(cooldown_min * 60)
            clear_marketplace_cooldown("AU")
            # Expand pool after cooldown for next eligible work
            more = due_owned_key_ids(client, limit=max(80, target * 5))
            for c in more:
                if c["priceKeyId"] not in {x["priceKeyId"] for x in pool}:
                    pool.append(c)
            continue

        if not row["PASS"]:
            report["stopReason"] = f"GUI_FAIL:{err or outcome}"
            stop = True
            break

        if gui_n < target:
            time.sleep(delay)

    report["finishedAt"] = datetime.now(timezone.utc).isoformat()
    report["resources"]["end"] = sample_resources()
    report["resources"]["chromeRssPeakMb"] = chrome_rss_peak
    report["resources"]["cpuPeakLoad"] = cpu_peak
    report["resources"]["cpuAvgLoad"] = round(sum(loads) / len(loads), 3) if loads else None
    report["chromeRestarts"] = chrome_restarts
    report["authenticationLoss"] = auth_loss

    outcomes = [r.get("pricingOutcome") for r in report["results"]]
    healthy = sum(1 for r in report["results"] if r.get("PASS"))
    transient_n = sum(
        1
        for r in report["results"]
        if r.get("SORRY") or r.get("pricingOutcome") == TEMPORARY_EBAY_SERVER_FAILURE
    )
    local_n = sum(1 for r in report["results"] if r.get("localGuiFailure"))
    summary = {
        "guiAttempts": len(report["results"]),
        "skippedFresh": len(report["skippedFresh"]),
        "healthy": healthy,
        "updated": sum(1 for o in outcomes if o == UPDATED_FROM_EBAY),
        "unchanged": sum(1 for o in outcomes if o == UNCHANGED_FROM_EBAY),
        "noNewEvidence": sum(
            1 for o in outcomes if o in {CHECKED_NO_NEW_EXACT_EVIDENCE, "checked_no_new_exact_evidence"}
        ),
        "transientEbayFailures": transient_n,
        "localGuiFailures": local_n,
        "SORRY": sum(1 for r in report["results"] if r.get("SORRY")),
        "challenges": sum(1 for r in report["results"] if r.get("challenge")),
        "cooldownEvents": len(report["cooldownEvents"]),
        "delayedRetriesEventuallySuccessful": delayed_retry_success,
        "searchFocusDefects": sum(
            1 for r in report["results"] if r.get("localGuiFailure") == "SEARCH_INPUT_NOT_CONFIRMED"
        ),
        "soldDefects": sum(
            1
            for r in report["results"]
            if r.get("localGuiFailure")
            and str(r.get("localGuiFailure")).startswith("SOLD")
        ),
        "unexpectedTabs": sum(1 for r in report["results"] if r.get("unexpectedTab")),
        "aboutBlank": sum(1 for r in report["results"] if r.get("aboutBlank")),
        "chromeCrashes": 0,
        "avgSeconds": round(
            sum(float(r.get("elapsedSec") or 0) for r in report["results"]) / max(1, len(report["results"])),
            1,
        ),
        "stopReason": report["stopReason"],
        "ebayHealthyRate": round(healthy / max(1, len(report["results"])), 3),
        "ebayTransientRate": round(transient_n / max(1, len(report["results"])), 3),
    }
    report["summary"] = summary

    # Verdict (do NOT enable owned_daily)
    attempts = summary["guiAttempts"]
    local_ok = (
        summary["searchFocusDefects"] == 0
        and summary["soldDefects"] == 0
        and summary["aboutBlank"] == 0
        and summary["unexpectedTabs"] == 0
        and summary["chromeCrashes"] == 0
        and summary["challenges"] == 0
        and local_n == 0
    )
    ebay_ok = (
        attempts >= target
        and summary["ebayHealthyRate"] >= 0.85
        and summary["ebayTransientRate"] <= 0.15
        and summary["challenges"] == 0
    )
    if summary["challenges"]:
        verdict = "EBAY_CHALLENGE_REQUIRED"
    elif not local_ok:
        verdict = "LINUX_GUI_RELIABILITY_INSUFFICIENT"
    elif attempts < target and report["stopReason"] == "REPEATED_TEMPORARY_EBAY_SERVER_FAILURE":
        verdict = "EBAY_AVAILABILITY_INSUFFICIENT"
    elif attempts >= target and ebay_ok and local_ok:
        if not capacity.get("projectedAbleToClear"):
            verdict = "LINUX_GUI_CAPACITY_INSUFFICIENT"
        else:
            verdict = "LINUX_GUI_OWNED_DAILY_READY_FOR_OWNER_ENABLE"
    elif attempts >= target and local_ok and not ebay_ok:
        verdict = "EBAY_AVAILABILITY_INSUFFICIENT"
    elif not capacity.get("projectedAbleToClear") and attempts >= target and local_ok:
        verdict = "LINUX_GUI_CAPACITY_INSUFFICIENT"
    else:
        verdict = "STOP"
    report["verdict"] = verdict
    report["ownedDailyEnabled"] = False

    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "verdict": verdict, "capacity": capacity, "workload": workload}, indent=2))
    print(f"Wrote {OUT / 'report.json'}")
    return 0 if verdict == "LINUX_GUI_OWNED_DAILY_READY_FOR_OWNER_ENABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
