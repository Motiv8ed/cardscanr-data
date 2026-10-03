#!/usr/bin/env python3
"""Single due-card eBay recovery probe when circuit is PROBE_REQUIRED.

Does not start the 25-card pilot. Max recovery sample after healthy probe:
probe + 2 additional due checks (= 3 healthy streak path).
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

from cardscanr_market_engine.ebay_availability import (
    browser_work_allowed,
    get_availability,
    record_sorry,
)
from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.providers.factory import create_market_comps_provider
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env
from tools.desktop_ebay_e2e_pricing import due_owned_key_ids, run_forced_job
from tools.linux_x11_ebay_batch10 import sample_resources

OUT = ROOT / "reports" / "artifacts" / "ebay_availability_recovery"
OUT.mkdir(parents=True, exist_ok=True)


def _client() -> SupabaseMarketEngineClient:
    load_supabase_env()
    return SupabaseMarketEngineClient(
        supabase_url=os.environ["SUPABASE_URL"].rstrip("/"),
        service_role_key=supabase_secret_key_from_env(),
    )


def _configure() -> None:
    os.environ["EBAY_BROWSER_NAV_MODE"] = "linux_x11"
    os.environ["EBAY_BROWSER_ENABLED"] = "true"
    os.environ["MARKET_LOOKUP_PROVIDER"] = "ebay_browser"
    os.environ["EBAY_BROWSER_HEADLESS"] = "false"
    os.environ["EBAY_BROWSER_MAX_QUERY_ATTEMPTS"] = "1"
    os.environ["EBAY_BROWSER_REUSE_CONTEXT"] = "true"
    os.environ["EBAY_BROWSER_CDP_PORT"] = os.environ.get("EBAY_BROWSER_CDP_PORT", "9444")
    os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"


def main() -> int:
    _configure()
    snap = get_availability()
    allowed, reason, snap = browser_work_allowed(for_probe=True)
    report: dict[str, Any] = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "availabilityBefore": snap.to_dict(),
        "probeAllowed": allowed,
        "reason": reason,
        "results": [],
        "ownedDailyEnabled": False,
    }
    if not allowed or snap.state != "PROBE_REQUIRED":
        report["verdict"] = "EBAY_COOLDOWN_ACTIVE" if snap.state == "COOLDOWN" else "BLOCKED"
        report["message"] = "No recovery probe performed; circuit not PROBE_REQUIRED."
        (OUT / "recovery_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0

    client = _client()
    pool = due_owned_key_ids(client, limit=40)
    runner = MarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider("ebay_browser"),
        config=MarketEngineConfig.from_env(),
    )
    runner._ebay_probe_mode = True  # type: ignore[attr-defined]

    # Exactly one probe, then up to two more if healthy.
    max_checks = 3
    gui_n = 0
    for card in pool:
        if gui_n >= max_checks:
            break
        # After probe, subsequent checks are normal (not probe mode).
        if gui_n == 0:
            runner._ebay_probe_mode = True  # type: ignore[attr-defined]
            allow, why, _ = browser_work_allowed(for_probe=True)
            if not allow:
                report["stopReason"] = why
                break
        else:
            runner._ebay_probe_mode = False  # type: ignore[attr-defined]
            allow, why, snap_now = browser_work_allowed(for_probe=False)
            if not allow:
                report["stopReason"] = why
                break

        try:
            payload = run_forced_job(
                client,
                price_key_id=card["priceKeyId"],
                reason="ebay_availability:recovery_probe" if gui_n == 0 else "ebay_availability:recovery_streak",
                runner=runner,
            )
        except Exception as exc:
            report["results"].append({"card": card.get("card"), "error": str(exc), "PASS": False})
            report["verdict"] = "BLOCKED"
            break

        result = payload["result"]
        status = str(result.get("status") or "")
        outcome = str(result.get("ownedDailyOutcome") or status)
        if status == "skipped_already_fresh":
            continue
        gui_n += 1
        sorry = "TEMPORARY_EBAY_SERVER_FAILURE" in outcome or "sorry" in str(result.get("error") or "").lower()
        challenge = "CHALLENGE" in outcome.upper()
        healthy = outcome in {
            "UPDATED_FROM_EBAY",
            "UNCHANGED_FROM_EBAY",
            "CHECKED_NO_NEW_EXACT_EVIDENCE",
            "completed",
            "checked_no_new_exact_evidence",
        }
        row = {
            "guiIndex": gui_n,
            "role": "probe" if gui_n == 1 else "streak",
            "card": card.get("card"),
            "priceKeyId": card["priceKeyId"],
            "outcome": outcome,
            "SORRY": sorry,
            "challenge": challenge,
            "PASS": healthy,
            "before": payload.get("before"),
            "after": payload.get("after"),
        }
        report["results"].append(row)
        print(f"[recovery] {gui_n}/{max_checks} {row['card']} outcome={outcome}", flush=True)
        if challenge:
            report["verdict"] = "EBAY_CHALLENGE_REQUIRED"
            break
        if sorry:
            report["verdict"] = "EBAY_RECOVERY_PROBE_FAILED"
            break
        if not healthy:
            report["verdict"] = "BLOCKED"
            break
        time.sleep(20)

    snap_after = get_availability()
    report["availabilityAfter"] = snap_after.to_dict()
    report["resources"] = sample_resources()
    healthy_n = sum(1 for r in report["results"] if r.get("PASS"))
    if report.get("verdict") is None:
        if healthy_n >= 3 and snap_after.confirmed_healthy:
            report["verdict"] = "EBAY_RECOVERY_HEALTHY_READY_FOR_25_CARD_PILOT"
        elif healthy_n >= 1 and snap_after.state == "HEALTHY":
            report["verdict"] = "EBAY_RECOVERY_HEALTHY_READY_FOR_25_CARD_PILOT" if healthy_n >= 3 else "BLOCKED"
        else:
            report["verdict"] = "BLOCKED"
    report["finishedAt"] = datetime.now(timezone.utc).isoformat()
    (OUT / "recovery_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "results": report["results"], "availabilityAfter": report["availabilityAfter"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
