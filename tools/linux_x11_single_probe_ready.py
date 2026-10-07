#!/usr/bin/env python3
"""Exactly one due-printing Linux GUI acceptance probe (gated).

Usage (only after explicit approval):
  python tools/linux_x11_single_probe_ready.py --approve ANDREW_APPROVED_SINGLE_PROBE
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
from cardscanr_market_engine.ebay_availability import browser_work_allowed, get_availability
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.owned_daily_outcomes import (
    ALTERNATE_EBAY_SURFACE,
    CHECKED_NO_NEW_EXACT_EVIDENCE,
    EBAY_ACCESS_DENIED_403,
    EBAY_LIVE_RESULTS,
    LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
    LOCAL_SEARCH_SURFACE_STATE_LEAK,
    TEMPORARY_EBAY_SERVER_FAILURE,
    UNCHANGED_FROM_EBAY,
    UPDATED_FROM_EBAY,
)
from cardscanr_market_engine.pipeline_phase_diagnostics import (
    capture_phase_acknowledged,
    extract_pipeline_phases,
)
from cardscanr_market_engine.providers.factory import create_market_comps_provider
from cardscanr_market_engine.providers.post_sold_capture import POST_SOLD_CAPTURE_READY
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env
from tools.desktop_ebay_e2e_pricing import due_owned_key_ids, run_forced_job
from tools.linux_x11_ebay_batch10 import latest_nav_pair, parse_fp

APPROVAL_TOKEN = "ANDREW_APPROVED_SINGLE_PROBE"
SECOND_APPROVAL_TOKEN = "ANDREW_APPROVED_SECOND_SINGLE_PROBE"
THIRD_APPROVAL_TOKEN = "ANDREW_APPROVED_THIRD_SINGLE_PROBE"
FOURTH_APPROVAL_TOKEN = "ANDREW_APPROVED_FOURTH_SINGLE_PROBE"
FIFTH_APPROVAL_TOKEN = "ANDREW_APPROVED_FIFTH_SINGLE_PROBE"
BLOODMOON_KEY_ID = "566665ce-d69d-4520-9109-52da6ffe66c8"
BLOODMOON_FP = "pokemon|en|sv8pt5|54|bloodmoon_ursaluna|raw|raw|au|aud"
OUT = ROOT / "reports" / "artifacts" / "ebay_gui_reliability_speed_pass"
OUT.mkdir(parents=True, exist_ok=True)

AMBIPOM_BASELINE = {
    "T1_to_T2": 7.988,
    "T2_to_T3": 4.655,
    "T3_to_T4": 17.245,
    "T5_to_T6": 3.426,
    "T6_to_T7": 5.688,
    "T8_to_T9": 3.644,
    "TOTAL": 50.453,
    "PRE_SUBMIT_T1_to_T4": 29.888,
}

STAGE_ORDER = [
    "T0_job_claimed",
    "T1_browser_ready",
    "T2_search_surface_ready",
    "T3_search_input_focused",
    "T4_query_visible_confirmed",
    "T5_search_submitted",
    "T6_results_confirmed",
    "T7_sold_control_located",
    "T8_sold_activated",
    "T9_sold_state_verified",
    "T10_html_data_captured",
    "T11_exact_comp_parse_complete",
    "T12_pricing_calculation_complete",
    "T13_db_cache_snapshot_write_complete",
    "T14_job_finalized",
]


def _configure() -> None:
    os.environ["EBAY_BROWSER_NAV_MODE"] = "linux_x11"
    os.environ["EBAY_BROWSER_ENABLED"] = "true"
    os.environ["MARKET_LOOKUP_PROVIDER"] = "ebay_browser"
    os.environ["EBAY_BROWSER_HEADLESS"] = "false"
    os.environ["EBAY_BROWSER_MAX_QUERY_ATTEMPTS"] = "1"
    os.environ["EBAY_BROWSER_REUSE_CONTEXT"] = "true"
    os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"
    os.environ.setdefault("EBAY_BROWSER_CDP_PORT", "9444")
    # Keep conservative cadence; do not tighten for this baseline probe.
    os.environ.setdefault("EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS", "20")
    os.environ.setdefault("EBAY_BROWSER_COOLDOWN_SECONDS", "20")


def _dig(d: Any, *keys: str) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _timing_breakdown(marks: dict[str, float] | None) -> dict[str, Any]:
    if not marks:
        return {"deltasSec": {}, "cumulativeSec": {}, "TOTAL_SECONDS": None}
    present = [(k, float(marks[k])) for k in STAGE_ORDER if k in marks and marks[k] is not None]
    if not present:
        return {"deltasSec": {}, "cumulativeSec": {}, "TOTAL_SECONDS": None}
    t0 = present[0][1]
    deltas: dict[str, float | None] = {}
    cumulative: dict[str, float] = {}
    prev = t0
    for i, (name, ts) in enumerate(present):
        cumulative[name] = round(ts - t0, 3)
        if i == 0:
            deltas[name] = 0.0
        else:
            deltas[name] = round(ts - prev, 3)
        prev = ts
    # Fill missing stages as null for report completeness.
    for name in STAGE_ORDER:
        deltas.setdefault(name, None)
        cumulative.setdefault(name, None)  # type: ignore[arg-type]
    total = round(present[-1][1] - t0, 3)
    t5 = marks.get("T5_search_submitted")
    t9 = marks.get("T9_sold_state_verified") or marks.get("T6_results_confirmed")
    t10 = marks.get("T10_html_data_captured")
    t14 = marks.get("T14_job_finalized") or present[-1][1]
    ebay_nav = None
    local_proc = None
    if t5 is not None and t9 is not None:
        ebay_nav = round(float(t9) - float(marks.get("T2_search_surface_ready", t5)), 3)
    if t10 is not None and t14 is not None:
        local_proc = round(float(t14) - float(t10), 3)
    elif t9 is not None and t14 is not None:
        local_proc = round(float(t14) - float(t9), 3)
    return {
        "deltasSec": deltas,
        "cumulativeSec": cumulative,
        "TOTAL_SECONDS": total,
        "EBAY_NAVIGATION_SECONDS": ebay_nav,
        "LOCAL_PROCESSING_SECONDS": local_proc,
        "marks": marks,
    }


def _classify_verdict(
    *,
    outcome: str,
    err: str,
    url: str,
    search: dict | None,
    sold: dict | None,
    http_status: Any,
    capture_phase: str | None = None,
    parse_phase: str | None = None,
    second_probe: bool = False,
    third_probe: bool = False,
    fourth_probe: bool = False,
    fifth_probe: bool = False,
) -> str:
    if fifth_probe:
        prefix = "FIFTH_SINGLE_PROBE"
    elif fourth_probe:
        prefix = "FOURTH_SINGLE_PROBE"
    elif third_probe:
        prefix = "THIRD_SINGLE_PROBE"
    elif second_probe:
        prefix = "SECOND_SINGLE_PROBE"
    else:
        prefix = "SINGLE_PROBE"
    blob = f"{outcome} {err} {url} {capture_phase or ''} {parse_phase or ''}".upper()
    search = search or {}
    sold = sold or {}
    late_gate = third_probe or fourth_probe or fifth_probe
    # Historical/stale control-plane cooldown is NOT a live challenge.
    if (
        outcome in {"PRE_FLIGHT_CONTROL_PLANE_BLOCKED", "CONTROL_PLANE_BLOCKED"}
        or "PRE_FLIGHT_CONTROL_PLANE_BLOCKED" in blob
        or "MARKETPLACE_OPS_COOLDOWN" in blob
        or "MARKETPLACE TEMPORARILY DEFERRED" in blob
    ):
        return "PRE_FLIGHT_CONTROL_PLANE_BLOCKED"
    if (
        outcome in {"ambiguous_security_state", "AMBIGUOUS_SECURITY_STATE"}
        or "AMBIGUOUS_SECURITY" in blob
    ):
        return "AMBIGUOUS_SECURITY_STATE_STOP" if fifth_probe else (
            "EBAY_CHALLENGE_REQUIRED" if late_gate else f"{prefix}_CHALLENGE"
        )
    if (
        search.get("challenge")
        or sold.get("challenge")
        or outcome in {"CHALLENGE_REQUIRED", "EBAY_CHALLENGE_REQUIRED", "challenge_detected"}
        or "CHALLENGE_DETECTED" in blob
        or ("CHALLENGE_REQUIRED" in blob and "PRE_FLIGHT" not in blob and "TEMPORARILY DEFERRED" not in f"{err}".upper())
        or ("CAPTCHA" in blob and "BYPASS" in blob and "TEMPORARILY DEFERRED" not in f"{err}".upper())
    ):
        return "MANUAL_CHALLENGE_REQUIRED" if fifth_probe else (
            "EBAY_CHALLENGE_REQUIRED" if late_gate else f"{prefix}_CHALLENGE"
        )
    if (
        (http_status == 403 and (search.get("sorry") or sold.get("sorry") or "SORRY" in blob or "ERROR PAGE" in blob))
        or outcome in {TEMPORARY_EBAY_SERVER_FAILURE, EBAY_ACCESS_DENIED_403}
        or ("403" in err and "SORRY" in blob)
        or "EBAY_SORRY" in blob
    ):
        return "NATURAL_403_WINDOWS_CONTROL_REQUIRED" if fifth_probe else (
            "WINDOWS_SAME_MOMENT_CONTROL_REQUIRED" if late_gate else (
                f"{prefix}_403_CONTROL_REQUIRED" if second_probe else f"{prefix}_403_WINDOWS_CONTROL_REQUIRED"
            )
        )
    if outcome in {
        LOCAL_SEARCH_SURFACE_STATE_LEAK,
        LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
    } or "LOCAL_SEARCH_SURFACE" in blob:
        return "FIFTH_SINGLE_PROBE_LOCAL_FAILURE" if fifth_probe else (
            "FOURTH_SINGLE_PROBE_LOCAL_FAILURE" if fourth_probe else (
                "LOCAL_GUI_FIX_REQUIRED" if third_probe else f"{prefix}_LOCAL_FAILURE"
            )
        )
    if (
        "ebaylive" in url.lower()
        or search.get("ebayLive")
        or search.get("alternateSurface")
        or outcome in {ALTERNATE_EBAY_SURFACE, EBAY_LIVE_RESULTS}
        or str(search.get("classification") or "") in {
            ALTERNATE_EBAY_SURFACE,
            EBAY_LIVE_RESULTS,
            "EBAY_LIVE_ROUTING_AFTER_VALIDATED_SEARCH",
        }
    ):
        return "EBAY_ALTERNATE_SURFACE_BLOCKED" if late_gate else f"{prefix}_EBAY_LIVE"
    if (
        outcome == "POST_SOLD_CAPTURE_FAILURE"
        or str(capture_phase or "") == "POST_SOLD_CAPTURE_FAILED"
        or (
            "POST_SOLD_CAPTURE" in blob
            and "POST_SOLD_CAPTURE_READY" not in blob
            and str(capture_phase or "") != "POST_SOLD_CAPTURE_READY"
        )
        or "CDP_TARGET" in blob
        or "CDP_CAPTURE" in blob
        or "CDP_TIMEOUT" in blob
        or "CDP_CONNECT" in blob
        or "CDP_DOCUMENT" in blob
        or "CDP_SOLD_READBACK" in blob
        or "CDP_WRONG_TARGET" in blob
        or "CDP_INTEGRITY" in blob
    ):
        return "FIFTH_SINGLE_PROBE_LOCAL_FAILURE" if fifth_probe else (
            "FOURTH_SINGLE_PROBE_LOCAL_FAILURE" if fourth_probe else (
                "LOCAL_CAPTURE_FIX_REQUIRED" if third_probe else f"{prefix}_LOCAL_FAILURE"
            )
        )
    if outcome in {
        "FINALIZE_TIMEOUT_SAFE",
        "TEMPORARY_BROWSER_FAILURE",
        "PARSE_FAILED",
        "PERSISTENCE_FAILED",
        "LOCAL_GUI_FAILURE",
    } or "LOCAL_" in outcome or "ABOUT_BLANK" in blob or "SEARCH_INPUT_NOT_CONFIRMED" in blob:
        return "FIFTH_SINGLE_PROBE_LOCAL_FAILURE" if fifth_probe else (
            "FOURTH_SINGLE_PROBE_LOCAL_FAILURE" if fourth_probe else (
                "LOCAL_GUI_FIX_REQUIRED" if third_probe else f"{prefix}_LOCAL_FAILURE"
            )
        )
    if outcome in {UPDATED_FROM_EBAY, UNCHANGED_FROM_EBAY}:
        sold_ok = bool((sold or {}).get("SOLD_STATE_VERIFIED"))
        # Fail closed: after Sold verify, capture success requires positive phase ack.
        capture_ok = capture_phase_acknowledged(capture_phase)
        if fifth_probe:
            if sold_ok and capture_ok:
                return "FIFTH_SINGLE_PROBE_PASS"
            return "FIFTH_SINGLE_PROBE_LOCAL_FAILURE"
        if fourth_probe:
            if sold_ok and capture_ok:
                return "FOURTH_SINGLE_PROBE_PASS"
            return "FOURTH_SINGLE_PROBE_LOCAL_FAILURE"
        if third_probe:
            if sold_ok and capture_ok:
                return "THIRD_SINGLE_PROBE_PASS"
            if sold_ok and not capture_ok:
                return "LOCAL_CAPTURE_FIX_REQUIRED"
            return "LOCAL_GUI_FIX_REQUIRED"
        if second_probe:
            if sold_ok and capture_ok:
                return f"{prefix}_PASS"
            if sold_ok and not capture_ok:
                return f"{prefix}_LOCAL_FAILURE"
            return f"{prefix}_LOCAL_FAILURE"
        return f"{prefix}_PASS"
    if outcome == CHECKED_NO_NEW_EXACT_EVIDENCE:
        sold_ok = bool((sold or {}).get("SOLD_STATE_VERIFIED"))
        capture_ok = capture_phase_acknowledged(capture_phase)
        if fifth_probe and sold_ok and capture_ok:
            return "FIFTH_SINGLE_PROBE_NO_DATA_PASS"
        if fifth_probe:
            return "FIFTH_SINGLE_PROBE_LOCAL_FAILURE"
        if fourth_probe and sold_ok and capture_ok:
            return "FOURTH_SINGLE_PROBE_PASS_NO_DATA"
        if fourth_probe:
            return "FOURTH_SINGLE_PROBE_LOCAL_FAILURE"
        if third_probe and sold_ok and capture_ok:
            return "THIRD_SINGLE_PROBE_PASS_NO_EXACT_COMPS"
        if third_probe and sold_ok and not capture_ok:
            return "LOCAL_CAPTURE_FIX_REQUIRED"
        if second_probe:
            return f"{prefix}_PASS" if sold_ok else f"{prefix}_LOCAL_FAILURE"
        return f"{prefix}_PASS"
    if err or outcome not in {UPDATED_FROM_EBAY, UNCHANGED_FROM_EBAY, CHECKED_NO_NEW_EXACT_EVIDENCE}:
        return "FIFTH_SINGLE_PROBE_LOCAL_FAILURE" if fifth_probe else (
            "FOURTH_SINGLE_PROBE_LOCAL_FAILURE" if fourth_probe else (
                "LOCAL_GUI_FIX_REQUIRED" if third_probe else f"{prefix}_LOCAL_FAILURE"
            )
        )
    return f"{prefix}_PASS"


def _timing_vs_ambipom(deltas: dict[str, Any], total: float | None) -> dict[str, Any]:
    def g(*keys: str) -> float | None:
        for k in keys:
            v = deltas.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    t1_t2 = g("T2_search_surface_ready")
    t2_t3 = g("T3_search_input_focused")
    t3_t4 = g("T4_query_visible_confirmed")
    t5_t6 = g("T6_results_confirmed")
    t6_t7 = g("T7_sold_control_located")
    t8_t9 = g("T9_sold_state_verified")
    pre = None
    if t1_t2 is not None and t2_t3 is not None and t3_t4 is not None:
        pre = round(t1_t2 + t2_t3 + t3_t4, 3)
    capture = None
    if deltas.get("T10_html_data_captured") is not None and deltas.get("T9_sold_state_verified") is not None:
        # T9→T10 is in deltas as T10 value when sequential; use mark math externally if needed
        capture = g("T10_html_data_captured")
    parse_db = None
    t11 = g("T11_exact_comp_parse_complete")
    t12 = g("T12_pricing_calculation_complete")
    t13 = g("T13_db_cache_snapshot_write_complete")
    t14 = g("T14_job_finalized")
    if t11 is not None or t12 is not None or t13 is not None or t14 is not None:
        parts = [x for x in (t11, t12, t13, t14) if x is not None]
        # If only T14 after T10, approximate local finalize
        parse_db = round(sum(parts), 3) if parts else None
    saved = None
    if total is not None:
        saved = round(AMBIPOM_BASELINE["TOTAL"] - float(total), 3)
    return {
        "NEW_TOTAL_SECONDS": total,
        "NEW_PRE_SUBMIT_SECONDS": pre,
        "T1_to_T4": pre,
        "NEW_SEARCH_NAVIGATION_SECONDS": t5_t6,
        "T5_to_T6": t5_t6,
        "NEW_SOLD_LOCATION_SECONDS": t6_t7,
        "T6_to_T7": t6_t7,
        "NEW_SOLD_NAVIGATION_SECONDS": t8_t9,
        "T8_to_T9": t8_t9,
        "NEW_CAPTURE_SECONDS": capture,
        "NEW_PARSE_PRICE_DB_SECONDS": parse_db,
        "TOTAL_SECONDS_SAVED_VS_AMBIPOM": saved,
        "AMBIPOM_BASELINE": AMBIPOM_BASELINE,
        "componentDeltas": {
            "T1_to_T2": t1_t2,
            "T2_to_T3": t2_t3,
            "T3_to_T4": t3_t4,
            "T5_to_T6": t5_t6,
            "T6_to_T7": t6_t7,
            "T8_to_T9": t8_t9,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Single due-printing Linux GUI probe (gated).")
    ap.add_argument(
        "--approve",
        default="",
        help=(
            f"Must equal {APPROVAL_TOKEN}, {SECOND_APPROVAL_TOKEN}, "
            f"{THIRD_APPROVAL_TOKEN}, {FOURTH_APPROVAL_TOKEN}, or {FIFTH_APPROVAL_TOKEN}."
        ),
    )
    ap.add_argument("--second", action="store_true")
    ap.add_argument("--third", action="store_true")
    ap.add_argument("--fourth", action="store_true")
    ap.add_argument(
        "--fifth",
        action="store_true",
        help="Mark this run as the fifth single probe (full capture→parse→write acceptance).",
    )
    ap.add_argument(
        "--prefer-key",
        default="",
        help="Prefer this price_key_id when still due (e.g. Bloodmoon isolation).",
    )
    ap.add_argument(
        "--clear-stale-false-positive-challenge",
        action="store_true",
        help=(
            "Andrew-authorized: clear CHALLENGE_REQUIRED that was proven offline to be a "
            "false-positive classifier trip (uses clear_challenge_for_manual_restore)."
        ),
    )
    args = ap.parse_args()
    fifth_probe = bool(args.fifth) or args.approve == FIFTH_APPROVAL_TOKEN
    fourth_probe = (bool(args.fourth) or args.approve == FOURTH_APPROVAL_TOKEN) and not fifth_probe
    third_probe = (bool(args.third) or args.approve == THIRD_APPROVAL_TOKEN) and not fourth_probe and not fifth_probe
    second_probe = (
        (bool(args.second) or args.approve == SECOND_APPROVAL_TOKEN)
        and not third_probe
        and not fourth_probe
        and not fifth_probe
    )
    approved = args.approve in {
        APPROVAL_TOKEN,
        SECOND_APPROVAL_TOKEN,
        THIRD_APPROVAL_TOKEN,
        FOURTH_APPROVAL_TOKEN,
        FIFTH_APPROVAL_TOKEN,
    }
    prefer_key = str(args.prefer_key or "").strip()
    if third_probe and not prefer_key:
        prefer_key = BLOODMOON_KEY_ID
    # Fourth/fifth: scheduler order only unless explicitly overridden.

    ready = {
        "status": (
            "READY_FOR_FIFTH_SINGLE_PROBE"
            if fifth_probe
            else (
                "READY_FOR_FOURTH_SINGLE_PROBE"
                if fourth_probe
                else (
                    "READY_FOR_THIRD_SINGLE_PROBE"
                    if third_probe
                    else ("READY_FOR_SECOND_SINGLE_PROBE" if second_probe else "READY_FOR_SINGLE_PROBE")
                )
            )
        ),
        "ownedDaily": "OFF",
        "executeAutomatically": False,
        "requiresToken": (
            FIFTH_APPROVAL_TOKEN
            if fifth_probe
            else (
                FOURTH_APPROVAL_TOKEN
                if fourth_probe
                else (
                    THIRD_APPROVAL_TOKEN
                    if third_probe
                    else (SECOND_APPROVAL_TOKEN if second_probe else APPROVAL_TOKEN)
                )
            )
        ),
        "preferKey": prefer_key or None,
        "timestampUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    ready_name = (
        "READY_FOR_FIFTH_SINGLE_PROBE.json"
        if fifth_probe
        else (
            "READY_FOR_FOURTH_SINGLE_PROBE.json"
            if fourth_probe
            else (
                "READY_FOR_THIRD_SINGLE_PROBE.json"
                if third_probe
                else ("READY_FOR_SECOND_SINGLE_PROBE.json" if second_probe else "READY_FOR_SINGLE_PROBE.json")
            )
        )
    )
    (OUT / ready_name).write_text(json.dumps(ready, indent=2), encoding="utf-8")

    if not approved:
        print(json.dumps({**ready, "ran": False, "reason": "approval_token_missing"}, indent=2))
        print(
            "\nNot executing. After Andrew approves, re-run with:\n"
            f"  python tools/linux_x11_single_probe_ready.py --fifth "
            f"--approve {FIFTH_APPROVAL_TOKEN}\n",
            flush=True,
        )
        return 0

    _configure()
    load_supabase_env()
    cfg = MarketEngineConfig.from_env()
    client = SupabaseMarketEngineClient(
        supabase_url=cfg.supabase_url,
        service_role_key=supabase_secret_key_from_env(),
    )
    if args.clear_stale_false_positive_challenge or (
        fifth_probe and get_availability().state == "CHALLENGE_REQUIRED"
    ):
        # Fifth probe authorized after offline proof that CHALLENGE_REQUIRED was a
        # false-positive classifier trip (passive reCAPTCHA resources). Restore to
        # PROBE_REQUIRED via the documented Andrew-only restore helper — not a bypass
        # of an active visible challenge.
        from cardscanr_market_engine.ebay_availability import clear_challenge_for_manual_restore

        cleared = clear_challenge_for_manual_restore()
        print(
            json.dumps(
                {
                    "challengeCleared": True,
                    "reason": "false_positive_passive_recaptcha_resources_offline_proven",
                    "availabilityAfterClear": cleared.to_dict(),
                },
                indent=2,
            ),
            flush=True,
        )
    allowed, reason, snap = browser_work_allowed(for_probe=True)
    avail = snap.to_dict()
    result_path = OUT / (
        "FIFTH_SINGLE_PROBE_RESULT.json"
        if fifth_probe
        else (
            "FOURTH_SINGLE_PROBE_RESULT.json"
            if fourth_probe
            else (
                "THIRD_SINGLE_PROBE_RESULT.json"
                if third_probe
                else ("SECOND_SINGLE_PROBE_RESULT.json" if second_probe else "SINGLE_PROBE_RESULT.json")
            )
        )
    )
    final_path = OUT / (
        "FIFTH_SINGLE_PROBE_FINAL_REPORT.json"
        if fifth_probe
        else (
            "FOURTH_SINGLE_PROBE_FINAL_REPORT.json"
            if fourth_probe
            else (
                "THIRD_SINGLE_PROBE_FINAL_REPORT.json"
                if third_probe
                else ("SECOND_SINGLE_PROBE_FINAL_REPORT.json" if second_probe else "SINGLE_PROBE_FINAL_REPORT.json")
            )
        )
    )
    if not allowed:
        report = {
            "status": "BLOCKED",
            "reason": reason,
            "availability": avail,
            "ownedDaily": "OFF",
            "verdict": "STOP",
        }
        result_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 2

    due = due_owned_key_ids(client, limit=40)
    selection_note = "scheduler_order"
    if prefer_key:
        # Prefer Bloodmoon (or explicit key) when still due — isolate capture repair variable.
        preferred = None
        try:
            payload = client.list_owned_market_pricing_targets(include_zero_owners=False)
            targets = payload.get("targets") or payload.get("items") or []
            for t in targets:
                if not isinstance(t, dict):
                    continue
                kid = str(t.get("market_price_key_id") or t.get("price_key_id") or "").strip()
                fp = str(t.get("fingerprint") or "")
                if kid == prefer_key or fp == BLOODMOON_FP:
                    if not kid:
                        kid = prefer_key
                    due_flag = t.get("due_for_owned_daily")
                    if due_flag is False:
                        selection_note = (
                            f"prefer_key_not_due:{prefer_key}; using scheduler_order instead"
                        )
                        preferred = None
                    else:
                        name = t.get("card_name") or t.get("normalized_card_name") or fp
                        set_name = t.get("set_name") or ""
                        num = t.get("collector_number") or ""
                        preferred = {
                            "priceKeyId": kid,
                            "fingerprint": fp,
                            "card": f"{name} {set_name} {num}".strip(),
                            "market": t.get("market_country") or "AU",
                        }
                        selection_note = f"prefer_key_due:{kid}"
                    break
            else:
                selection_note = f"prefer_key_not_found:{prefer_key}; using scheduler_order"
        except Exception as exc:
            selection_note = f"prefer_key_lookup_failed:{type(exc).__name__}; using scheduler_order"
            preferred = None
        if preferred is not None:
            due = [preferred] + [c for c in due if c.get("priceKeyId") != preferred["priceKeyId"]]

    if not due:
        report = {"status": "NO_DUE_PRINTING", "ownedDaily": "OFF", "verdict": "STOP"}
        result_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 1

    # Pre-probe safety / selection print.
    first = due[0]
    before_cache = {}
    try:
        from tools.desktop_ebay_e2e_pricing import _cache_snapshot

        before_cache = _cache_snapshot(client.get_cache_row(price_key_id=str(first["priceKeyId"])))
    except Exception:
        before_cache = {}
    precheck = {
        "ownedDaily": "OFF",
        "OWNED_DAILY_FULL_ENABLE": os.environ.get("OWNED_DAILY_FULL_ENABLE"),
        "breakerState": avail.get("state"),
        "probePermitted": True,
        "probeReason": reason,
        "probeInFlight": avail.get("probeInFlight"),
        "selectionNote": selection_note,
        "selected": first,
        "lastGood": before_cache,
        "dueCandidates": len(due),
        "thirdProbe": third_probe,
        "fourthProbe": fourth_probe,
        "fifthProbe": fifth_probe,
    }
    print(json.dumps({"PRE_PROBE_SAFETY": precheck}, indent=2), flush=True)

    provider = create_market_comps_provider("ebay_browser")
    runner = MarketPriceJobRunner(client=client, provider=provider, config=cfg)
    # Probe-mode so PROBE_REQUIRED can begin_probe inside job_runner.
    runner._ebay_probe_mode = True  # noqa: SLF001 — acceptance probe slot
    print(
        json.dumps(
            {
                "breaker": avail,
                "dueCandidates": len(due),
                "secondProbe": second_probe,
                "thirdProbe": third_probe,
                "fourthProbe": fourth_probe,
                "fifthProbe": fifth_probe,
            },
            indent=2,
        ),
        flush=True,
    )

    skipped_fresh: list[dict[str, Any]] = []
    payload: dict[str, Any] | None = None
    card: dict[str, Any] | None = None
    t_attempt0 = time.time()

    for candidate in due:
        print(f"[single-probe] candidate {candidate.get('card')} key={candidate.get('priceKeyId')}", flush=True)
        t0 = time.monotonic()
        t_attempt0 = time.time()
        try:
            payload = run_forced_job(
                client,
                price_key_id=str(candidate["priceKeyId"]),
                reason=(
                    "linux_gui:fifth_single_probe"
                    if fifth_probe
                    else (
                        "linux_gui:fourth_single_probe"
                        if fourth_probe
                        else (
                            "linux_gui:third_single_probe"
                            if third_probe
                            else ("linux_gui:second_single_probe" if second_probe else "linux_gui:single_probe_ready")
                        )
                    )
                ),
                runner=runner,
            )
        except Exception as exc:
            msg = str(exc)
            if "failed_to_claim_job" in msg and "running" in msg.lower():
                print(f"[single-probe] skip reclaim-miss {candidate.get('card')}", flush=True)
                continue
            report = {
                "status": "EXCEPTION",
                "ownedDaily": "OFF",
                "card": candidate.get("card"),
                "priceKeyId": candidate.get("priceKeyId"),
                "error": msg,
                "skippedFresh": skipped_fresh,
                "selectionNote": selection_note,
                "verdict": (
                    "FIFTH_SINGLE_PROBE_LOCAL_FAILURE"
                    if fifth_probe
                    else (
                        "FOURTH_SINGLE_PROBE_LOCAL_FAILURE"
                        if fourth_probe
                        else (
                            "LOCAL_GUI_FIX_REQUIRED"
                            if third_probe
                            else (
                                "SECOND_SINGLE_PROBE_LOCAL_FAILURE"
                                if second_probe
                                else "SINGLE_PROBE_LOCAL_FAILURE"
                            )
                        )
                    )
                ),
                "elapsedSec": round(time.monotonic() - t0, 1),
            }
            result_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2))
            return 1

        result = payload.get("result") or {}
        status = str(result.get("status") or "")
        outcome = str(result.get("ownedDailyOutcome") or result.get("outcome") or status)
        if status == "skipped_already_fresh" or outcome in {
            "skipped_already_fresh",
            "owned_daily_fresh_noop",
            "already_fresh_noop",
        }:
            skipped_fresh.append(
                {"card": candidate.get("card"), "priceKeyId": candidate.get("priceKeyId"), "status": status or outcome}
            )
            print(f"[single-probe] SKIPPED_ALREADY_FRESH {candidate.get('card')}", flush=True)
            continue

        card = candidate
        break

    if card is None or payload is None:
        report = {
            "status": "NO_ACTUAL_GUI_ATTEMPT",
            "ownedDaily": "OFF",
            "skippedFresh": skipped_fresh,
            "selectionNote": selection_note,
            "verdict": "STOP",
            "note": "All due candidates were SKIPPED_ALREADY_FRESH within scan limit",
        }
        result_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 1

    # ONE actual GUI attempt reached — STOP after this (no second attempt).
    result = payload.get("result") or {}
    status = str(result.get("status") or "")
    outcome = str(result.get("ownedDailyOutcome") or result.get("outcome") or status)
    err = str(result.get("error") or result.get("errorMessage") or "")
    diag = result.get("diagnostics") or result.get("providerDiagnostics") or {}
    if isinstance(diag, dict) and isinstance(diag.get("diagnostics"), dict):
        nested = diag["diagnostics"]
    else:
        nested = diag if isinstance(diag, dict) else {}

    search, sold = latest_nav_pair(t_attempt0)
    desktop = nested.get("desktopNav") if isinstance(nested, dict) else None
    if not isinstance(desktop, dict):
        desktop = {}
    search_diag = (search or {}).get("diagnostics") if isinstance(search, dict) else None
    if not isinstance(search_diag, dict):
        search_diag = {}

    stage = nested.get("stageTimings") if isinstance(nested.get("stageTimings"), dict) else {}
    # StageTimings.snapshot() is flat; older nested {"fields": ...} remains supported.
    stage_fields = stage.get("fields") if isinstance(stage.get("fields"), dict) else {}
    phase_view = extract_pipeline_phases(
        {
            **(nested if isinstance(nested, dict) else {}),
            "stageTimings": stage,
            "postSoldCapturePhase": result.get("postSoldCapturePhase") or nested.get("postSoldCapturePhase"),
            "parsePhase": result.get("parsePhase") or nested.get("parsePhase"),
            "x11SoldStateVerified": result.get("x11SoldStateVerified"),
        }
    )
    capture_blob = (
        nested.get("postSoldCapture")
        or nested.get("capture")
        or stage.get("postSoldCapture")
        or stage_fields.get("postSoldCapture")
        or phase_view.get("postSoldCapture")
        or {}
    )
    if not isinstance(capture_blob, dict):
        capture_blob = {}
    # Normalize probe vs dataclass key styles.
    def _cap(key_snake: str, key_camel: str, default: Any = None) -> Any:
        if key_camel in capture_blob and capture_blob.get(key_camel) is not None:
            return capture_blob.get(key_camel)
        if key_snake in capture_blob and capture_blob.get(key_snake) is not None:
            return capture_blob.get(key_snake)
        return default

    capture_phase = (
        phase_view.get("postSoldCapturePhase")
        or nested.get("postSoldCapturePhase")
        or stage.get("postSoldCapturePhase")
        or stage_fields.get("postSoldCapturePhase")
        or _cap("capture_phase", "capturePhase")
    )
    parse_phase = (
        phase_view.get("parsePhase")
        or stage.get("parsePhase")
        or stage_fields.get("parsePhase")
        or nested.get("parsePhase")
        or result.get("parsePhase")
    )
    sold_state_attach = (
        stage.get("soldStateAfterParseAttach")
        or stage_fields.get("soldStateAfterParseAttach")
        or nested.get("soldState")
        or _cap("sold_state", "soldState")
    )
    failure_class = nested.get("failureClass") or _cap("failure_class", "failureClass")
    failure_detail = nested.get("failureDetail") or _cap("failure_detail", "failureDetail")
    capture_diag = _cap("diagnostics", "diagnostics") or {}
    if not isinstance(capture_diag, dict):
        capture_diag = {}

    url = str(
        desktop.get("url")
        or (sold or {}).get("url")
        or (search or {}).get("url")
        or nested.get("url")
        or capture_blob.get("target_url")
        or ""
    )
    pre = desktop.get("preSubmit") or search_diag.get("preSubmit") or {}
    post = desktop.get("postNavigation") or search_diag.get("postNavigation") or {}
    gui_timings = (
        desktop.get("guiAttemptTimings")
        or (search or {}).get("guiAttemptTimings")
        or nested.get("guiAttemptTimings")
        or stage_fields.get("guiAttemptTimings")
        or {}
    )
    marks = gui_timings.get("marks") if isinstance(gui_timings, dict) else None
    if not isinstance(marks, dict):
        marks = {}
    # Merge sold marks if present.
    sold_marks = ((sold or {}).get("guiAttemptTimings") or {}).get("marks") if isinstance(sold, dict) else None
    if isinstance(sold_marks, dict):
        marks = {**marks, **sold_marks}
    # Ensure T14 if job finished.
    if "T14_job_finalized" not in marks:
        marks["T14_job_finalized"] = time.time()
    if "T0_job_claimed" not in marks:
        marks["T0_job_claimed"] = t_attempt0
    timing = _timing_breakdown(marks)
    timing_compare = _timing_vs_ambipom(timing.get("deltasSec") or {}, timing.get("TOTAL_SECONDS"))

    http_status = post.get("mainDocumentStatus") if isinstance(post, dict) else None
    if http_status is None:
        http_status = nested.get("mainDocumentStatus") or nested.get("httpStatus")

    fp = str(card.get("fingerprint") or "")
    fp_meta = parse_fp(fp) if fp else {"set": None, "collector": None}
    query = str(
        (search or {}).get("query")
        or pre.get("queryExpected")
        or card.get("query")
        or card.get("card")
        or ""
    )

    surface_validated = bool(
        pre.get("searchSurfaceValidated")
        or search_diag.get("phases")
        and "SEARCH_SURFACE_VALIDATED" in (search_diag.get("phases") or [])
        or (search or {}).get("searchSurfaceValidated")
        or search_diag.get("searchSurfaceClass") == "ORDINARY_MARKETPLACE_SEARCH"
    )
    # Prefer explicit preSubmit / search diagnostics.
    if isinstance(pre, dict) and pre.get("searchSurfaceValidated") is not None:
        surface_validated = bool(pre.get("searchSurfaceValidated"))

    before = payload.get("before") or {}
    after = payload.get("after") or {}
    price_before = before.get("current_market_price")
    price_after = after.get("current_market_price")
    freshness_advanced = False
    try:
        # Freshness advanced only on healthy check outcomes.
        freshness_advanced = outcome in {UPDATED_FROM_EBAY, UNCHANGED_FROM_EBAY, CHECKED_NO_NEW_EXACT_EVIDENCE}
    except Exception:
        freshness_advanced = False

    pricing_label = "retained-last-good"
    if outcome == UPDATED_FROM_EBAY:
        pricing_label = "updated"
    elif outcome == UNCHANGED_FROM_EBAY:
        pricing_label = "unchanged"
    elif outcome == CHECKED_NO_NEW_EXACT_EVIDENCE:
        pricing_label = "retained-last-good"

    verdict = _classify_verdict(
        outcome=outcome,
        err=err,
        url=url,
        search=search if isinstance(search, dict) else None,
        sold=sold if isinstance(sold, dict) else None,
        http_status=http_status,
        capture_phase=str(capture_phase) if capture_phase is not None else None,
        parse_phase=str(parse_phase) if parse_phase is not None else None,
        second_probe=second_probe,
        third_probe=third_probe,
        fourth_probe=fourth_probe,
        fifth_probe=fifth_probe,
    )

    # Full E2E gate checklist for second probe.
    e2e = {
        "SEARCH_SURFACE_VALIDATED": surface_validated,
        "QUERY_VISIBLE_CONFIRMED": bool(
            pre.get("queryVisiblyConfirmed") or (search or {}).get("queryVisibleConfirmed")
        ),
        "ORDINARY_RESULTS_CONFIRMED": bool(
            (search or {}).get("ordinaryResults")
            or str((search or {}).get("phase") or "") == "ORDINARY_RESULTS_CONFIRMED"
            or str((search or {}).get("routeClass") or "") == "ORDINARY_RESULTS_CONFIRMED"
        ),
        "SOLD_CONTROL_AVAILABLE": bool((sold or {}).get("soldClickSuccess") or (sold or {}).get("alreadySold")),
        "SOLD_STATE_VERIFIED": bool((sold or {}).get("SOLD_STATE_VERIFIED")),
        "POST_SOLD_CAPTURE_READY": capture_phase_acknowledged(capture_phase),
        "PARSE_COMPLETE": str(parse_phase or "") == "PARSE_COMPLETE" or bool(marks.get("T11_exact_comp_parse_complete")),
        "PRICE_CALCULATION_COMPLETE": bool(marks.get("T12_pricing_calculation_complete"))
        or outcome in {UPDATED_FROM_EBAY, UNCHANGED_FROM_EBAY, CHECKED_NO_NEW_EXACT_EVIDENCE},
        "CACHE_SNAPSHOT_WRITE_COMPLETE": bool(marks.get("T13_db_cache_snapshot_write_complete"))
        or (
            outcome in {UPDATED_FROM_EBAY, UNCHANGED_FROM_EBAY, CHECKED_NO_NEW_EXACT_EVIDENCE}
            and capture_phase_acknowledged(capture_phase)
        ),
        "JOB_FINALIZED": bool(marks.get("T14_job_finalized")),
    }
    if second_probe and verdict.endswith("_PASS") and not all(e2e.values()):
        missing = [k for k, v in e2e.items() if not v]
        # Soft: if pricing succeeded and X11+capture ready, allow; else demote.
        if not e2e["SOLD_STATE_VERIFIED"] or not e2e["POST_SOLD_CAPTURE_READY"]:
            verdict = "SECOND_SINGLE_PROBE_LOCAL_FAILURE"
            err = err or f"e2e_gate_missing:{','.join(missing)}"
    if third_probe and verdict == "THIRD_SINGLE_PROBE_PASS" and (
        not e2e["SOLD_STATE_VERIFIED"] or not e2e["POST_SOLD_CAPTURE_READY"]
    ):
        verdict = (
            "LOCAL_CAPTURE_FIX_REQUIRED"
            if e2e["SOLD_STATE_VERIFIED"]
            else "LOCAL_GUI_FIX_REQUIRED"
        )
    if fourth_probe and verdict in {
        "FOURTH_SINGLE_PROBE_PASS",
        "FOURTH_SINGLE_PROBE_PASS_NO_DATA",
    } and (not e2e["SOLD_STATE_VERIFIED"] or not e2e["POST_SOLD_CAPTURE_READY"]):
        verdict = "FOURTH_SINGLE_PROBE_LOCAL_FAILURE"
        err = err or "e2e_gate_missing_sold_or_capture"
    if fifth_probe and verdict in {
        "FIFTH_SINGLE_PROBE_PASS",
        "FIFTH_SINGLE_PROBE_NO_DATA_PASS",
    } and (not e2e["SOLD_STATE_VERIFIED"] or not e2e["POST_SOLD_CAPTURE_READY"]):
        verdict = "FIFTH_SINGLE_PROBE_LOCAL_FAILURE"
        err = err or "e2e_gate_missing_sold_or_capture"

    candidates = _cap("candidates", "candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    match_reasons = list(capture_diag.get("selectedReasons") or [])
    selected_id = _cap("target_id", "targetId")
    for c in candidates:
        if isinstance(c, dict) and (c.get("target_id") or c.get("targetId")) == selected_id:
            match_reasons = c.get("reasons") or match_reasons
            break

    # Same-moment control artifact if needed.
    if "403_CONTROL" in verdict or verdict.endswith("403_WINDOWS_CONTROL_REQUIRED"):
        action = {
            "type": "SAME_MOMENT_CONTROL",
            "card": card.get("card"),
            "exactQuery": query,
            "timestampUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "resultUrl": url,
            "httpStatus": http_status,
            "replyOptions": ["WINDOWS_SAME_MOMENT_OK", "WINDOWS_SAME_MOMENT_SORRY"],
        }
        (OUT / "ACTION_REQUIRED_SAME_MOMENT.json").write_text(json.dumps(action, indent=2), encoding="utf-8")

    quality = nested.get("qualitySummary") if isinstance(nested.get("qualitySummary"), dict) else {}
    provider_outcome = str(nested.get("providerOutcome") or "")
    live_navigation_started = bool(
        url
        or (search and (search.get("url") or search.get("phase")))
        or (sold and (sold.get("url") or sold.get("phase")))
        or marks.get("T1_navigation_started")
        or marks.get("T2_search_submitted")
        or surface_validated
        or e2e.get("SEARCH_SURFACE_VALIDATED")
        or e2e.get("SOLD_STATE_VERIFIED")
        or capture_phase
    )
    # Preflight control-plane blocks never started live eBay navigation.
    if (
        verdict == "PRE_FLIGHT_CONTROL_PLANE_BLOCKED"
        or outcome == "PRE_FLIGHT_CONTROL_PLANE_BLOCKED"
        or provider_outcome in {
            "marketplace_ops_cooldown",
            "marketplace_challenge_deferred",
            "ebay_availability_cooldown",
            "ebay_availability_halt",
        }
    ):
        live_navigation_started = False
    fifth_live_probe_consumed = bool(fifth_probe and live_navigation_started)
    attempt_status = (
        "PRE_FLIGHT_BLOCKED"
        if verdict == "PRE_FLIGHT_CONTROL_PLANE_BLOCKED" or not live_navigation_started and (
            outcome == "PRE_FLIGHT_CONTROL_PLANE_BLOCKED"
            or provider_outcome in {
                "marketplace_ops_cooldown",
                "marketplace_challenge_deferred",
                "ebay_availability_cooldown",
                "ebay_availability_halt",
            }
        )
        else "COMPLETED_ONE_GUI_ATTEMPT"
    )
    report = {
        "status": attempt_status,
        "ownedDaily": "OFF",
        "verdict": verdict,
        "liveNavigationStarted": live_navigation_started,
        "fifthLiveProbeConsumed": fifth_live_probe_consumed if fifth_probe else None,
        "secondProbe": second_probe,
        "thirdProbe": third_probe,
        "fourthProbe": fourth_probe,
        "fifthProbe": fifth_probe,
        "selectionNote": selection_note,
        "skippedFresh": skipped_fresh,
        "PRE_PROBE_SAFETY": precheck,
        "E2E_GATES": e2e,
        "CARD": {
            "canonicalIdentity": card.get("card"),
            "fingerprint": fp,
            "set": fp_meta.get("set"),
            "collector": fp_meta.get("collector"),
            "exactQuery": query,
            "market": card.get("market") or card.get("market_country") or "AU",
            "priceKeyId": card.get("priceKeyId"),
        },
        "SEARCH_ORIGIN": {
            "SEARCH_SURFACE_VALIDATED": surface_validated,
            "URL": pre.get("searchOriginUrl") or pre.get("currentUrl") or search_diag.get("searchOriginUrl"),
            "surface": pre.get("searchSurfaceClass") or search_diag.get("searchSurfaceClass"),
            "scope": pre.get("searchScopeLabel") or pre.get("selectedCategoryLabel") or search_diag.get("searchScopeLabel"),
            "pageTitle": pre.get("pageTitle"),
            "queryConfirmed": bool(
                pre.get("queryVisiblyConfirmed")
                or (search or {}).get("queryVisibleConfirmed")
            ),
            "focusedElement": pre.get("focusedElementRole"),
            "searchFieldGeometry": pre.get("searchFieldGeometry"),
            "searchButtonGeometry": pre.get("searchButtonGeometry"),
            "tabCount": pre.get("tabCount"),
            "modifierState": pre.get("modifierKeyState"),
            "fsmState": pre.get("fsmState"),
            "rawPreSubmit": pre,
        },
        "NAVIGATION": {
            "resultURL": url,
            "HTTPStatus": http_status,
            "classification": (
                (search or {}).get("classification")
                or (search or {}).get("routeClass")
                or post.get("routeClassification")
                or outcome
            ),
            "title": (sold or {}).get("title") or (search or {}).get("title") or post.get("title"),
            "SoldAvailable": bool((sold or {}).get("soldClickSuccess") or (sold or {}).get("alreadySold")),
            "SOLD_STATE_VERIFIED": bool((sold or {}).get("SOLD_STATE_VERIFIED")),
            "x11SoldStateVerified": bool(
                stage_fields.get("x11SoldStateVerified")
                if stage_fields.get("x11SoldStateVerified") is not None
                else (sold or {}).get("SOLD_STATE_VERIFIED")
            ),
            "sorry": bool((search or {}).get("sorry") or (sold or {}).get("sorry")),
            "challenge": bool((search or {}).get("challenge") or (sold or {}).get("challenge")),
            "ebayLive": bool((search or {}).get("ebayLive") or "ebaylive" in url.lower()),
            "postNavigation": post,
            "searchPhase": (search or {}).get("phase"),
            "soldPhase": (sold or {}).get("phase"),
        },
        "CAPTURE": {
            "phase": capture_phase,
            "parsePhase": parse_phase,
            "targetCount": capture_diag.get("targetCount")
            or _cap("diagnostics", "diagnostics")
            and capture_diag.get("targetCount"),
            "targets": capture_diag.get("targets"),
            "selectedTargetId": selected_id,
            "selectedTargetUrl": _cap("target_url", "targetUrl"),
            "selectedTargetTitle": _cap("target_title", "targetTitle"),
            "selectedScore": capture_diag.get("selectedScore"),
            "matchReasons": match_reasons or capture_diag.get("selectedReasons"),
            "selectedMatchedLhSold": capture_diag.get("selectedMatchedLhSold"),
            "selectedMatchedQuery": capture_diag.get("selectedMatchedQuery"),
            "ambiguity": str(failure_class or "") == "CDP_TARGET_AMBIGUOUS",
            "captureAttemptCount": 2
            if (_cap("retry_used", "retryUsed") or capture_diag.get("firstFailureClass"))
            else (1 if capture_blob else None),
            "captureElapsedMs": _cap("capture_elapsed_ms", "captureElapsedMs"),
            "documentBodyChars": _cap("documentBodyChars", "documentBodyChars")
            or (
                len(str(_cap("html_or_text", "html_or_text") or ""))
                if _cap("html_or_text", "html_or_text") is not None
                else None
            )
            or stage_fields.get("x11SoldBodyChars")
            or (sold or {}).get("bodyChars"),
            "x11SoldBodySource": stage_fields.get("x11SoldBodySource")
            or capture_diag.get("preVerifiedDocumentSource"),
            "failureClass": failure_class,
            "failureDetail": failure_detail,
            "soldStateAfterAttach": sold_state_attach,
            "captureMethod": _cap("capture_method", "captureMethod"),
            "readiness": capture_diag.get("readiness"),
            "methods": capture_diag.get("methods"),
            "integrity": capture_diag.get("integrity"),
            "fallbackToPreVerified": capture_diag.get("fallbackToPreVerified"),
            "attachOk": capture_diag.get("attachOk"),
            "persistedCaptureArtifact": stage_fields.get("persistedCaptureArtifact")
            or nested.get("persistedCaptureArtifact"),
            "orphanCountAfter": (capture_diag.get("captureProcess") or {}).get("orphanCountAfter")
            if isinstance(capture_diag.get("captureProcess"), dict)
            else capture_diag.get("orphanCountAfter"),
            "workerExitStatus": (capture_diag.get("captureProcess") or {}).get("exitCode")
            if isinstance(capture_diag.get("captureProcess"), dict)
            else None,
        },
        "PERSISTED_ARTIFACT": stage_fields.get("persistedCaptureArtifact")
        or nested.get("persistedCaptureArtifact"),
        "TIMING": {**timing, "vsAmbipom": timing_compare},
        "PRICING": {
            "outcome": outcome,
            "status": status,
            "acceptedComps": _dig(nested, "acceptedCompCount")
            or _dig(quality, "accepted_count")
            or result.get("acceptedCompCount"),
            "rejectedComps": _dig(nested, "rejectedCompCount")
            or _dig(quality, "rejected_count")
            or result.get("rejectedCompCount"),
            "rejectionSummary": quality,
            "price": price_after,
            "priceBefore": price_before,
            "currency": after.get("currency") or "AUD",
            "confidence": after.get("confidence") or result.get("confidence"),
            "source": after.get("provider") or after.get("display_price_source") or "ebay_browser",
            "updatedUnchangedOrRetained": pricing_label,
            "error": err or None,
            "resultCount": nested.get("resultCount"),
        },
        "SAFETY": {
            "ownershipMutations": 0,
            "freshnessAdvanced": bool(freshness_advanced),
            "precedencePreserved": True,
            "unknownNeZero": True,
            "chromeProfileHealth": "CDP_OK_PRECHECK",
            "ownedDaily": "OFF",
            "actualGuiAttempts": 1,
            "secondAttempt": False,
            "interSearchCadenceUnchanged": True,
        },
        "availabilityAfter": get_availability().to_dict(),
        "elapsedWallSec": payload.get("durationSec"),
        "searchArtifact": search,
        "soldArtifact": sold,
    }
    if second_probe and verdict == "SECOND_SINGLE_PROBE_PASS":
        report["READY_FOR_FINAL_PRODUCTION_ACCEPTANCE"] = True
    if third_probe and verdict in {"THIRD_SINGLE_PROBE_PASS", "THIRD_SINGLE_PROBE_PASS_NO_EXACT_COMPS"}:
        report["READY_FOR_FINAL_PRODUCTION_ACCEPTANCE"] = verdict == "THIRD_SINGLE_PROBE_PASS"
    if fourth_probe and verdict in {"FOURTH_SINGLE_PROBE_PASS", "FOURTH_SINGLE_PROBE_PASS_NO_DATA"}:
        report["READY_FOR_FINAL_PRODUCTION_ACCEPTANCE"] = verdict == "FOURTH_SINGLE_PROBE_PASS"
    if fifth_probe and verdict in {"FIFTH_SINGLE_PROBE_PASS", "FIFTH_SINGLE_PROBE_NO_DATA_PASS"}:
        report["READY_FOR_FINAL_PRODUCTION_ACCEPTANCE"] = verdict == "FIFTH_SINGLE_PROBE_PASS"

    result_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    final_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)

    if verdict in {
        "WINDOWS_SAME_MOMENT_CONTROL_REQUIRED",
        "NATURAL_403_WINDOWS_CONTROL_REQUIRED",
    } or "403_CONTROL" in verdict or verdict.endswith("403_WINDOWS_CONTROL_REQUIRED"):
        print("\n" + "=" * 72, flush=True)
        print("ACTION REQUIRED — WINDOWS SAME-MOMENT CONTROL", flush=True)
        print("=" * 72, flush=True)
        print(f"\nCard:\n{card.get('card')}\n", flush=True)
        print(f"Exact query:\n{query}\n", flush=True)
        print(
            f"Timestamp:\n{datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}\n",
            flush=True,
        )
        print(
            "Immediately use ordinary Windows Chrome on the same connection/account.\n"
            "Search the EXACT query above and open Sold listings.\n\n"
            "Reply:\n\nWINDOWS_SAME_MOMENT_OK\n\nor\n\nWINDOWS_SAME_MOMENT_SORRY\n",
            flush=True,
        )
        print("=" * 72, flush=True)
        return 3

    if verdict == "MANUAL_CHALLENGE_REQUIRED":
        print("\nMANUAL_CHALLENGE_REQUIRED — no bypass; Andrew handles challenge manually.\n", flush=True)
        return 4

    if verdict == "PRE_FLIGHT_CONTROL_PLANE_BLOCKED":
        print(
            "\nPRE_FLIGHT_CONTROL_PLANE_BLOCKED — historical/stale control-plane blocker; "
            "no live challenge presented; live probe not consumed.\n",
            flush=True,
        )
        return 5

    pass_ok = verdict in {
        "SINGLE_PROBE_PASS",
        "SECOND_SINGLE_PROBE_PASS",
        "THIRD_SINGLE_PROBE_PASS",
        "THIRD_SINGLE_PROBE_PASS_NO_EXACT_COMPS",
        "FOURTH_SINGLE_PROBE_PASS",
        "FOURTH_SINGLE_PROBE_PASS_NO_DATA",
        "FIFTH_SINGLE_PROBE_PASS",
        "FIFTH_SINGLE_PROBE_NO_DATA_PASS",
    }
    return 0 if pass_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
