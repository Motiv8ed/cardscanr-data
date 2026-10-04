#!/usr/bin/env python3
"""Serialized US/GB/CA live canary through the single global browser.

Does not start a second Chrome worker loop. Enqueues one due job for the
target market, then runs market_price_worker --once. AU flag is left unchanged.
JP/EU are never allowlisted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.continuous_worker_policy import classify_continuous_gate
from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from cardscanr_market_engine.local_browser_runtime import ensure_xvfb
from cardscanr_market_engine.owned_daily_enablement import owned_daily_full_enable
from cardscanr_market_engine.region_pricing_registry import region_definition
from cardscanr_market_engine.region_pricing_status import multi_region_status


ART = ROOT / "reports" / "artifacts" / "us_gb_ca_serial_canary"
CURRENCY = {"US": "USD", "GB": "GBP", "CA": "CAD"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def bind_browser_env(market: str) -> dict[str, str]:
    definition = region_definition(market)
    values = {
        "OWNED_DAILY_ALLOWED_MARKETS": market,
        "OWNED_DAILY_MAX_ENQUEUE": "1",
        "OWNED_DAILY_FULL_MAX_ENQUEUE": "1",
        "OWNED_DAILY_SYNC_KEYS": "false",
        "OWNED_DAILY_DRY_RUN": "false",
        "MARKET_WORKER_ALLOWED_MARKETS": market,
        "MARKET_WORKER_CONCURRENCY": "1",
        "MARKET_WORKER_MAX_JOBS_PER_RUN": "1",
        "CONFIRM_LIVE_EBAY_WORKER": "true",
        "EBAY_BROWSER_ENABLED": "true",
        "MARKET_LOOKUP_PROVIDER": "ebay_browser",
        "EBAY_BROWSER_NAV_MODE": "linux_x11",
        "EBAY_BROWSER_MAX_QUERY_ATTEMPTS": "1",
        "GLOBAL_BROWSER_PRICING_CONCURRENCY": "1",
        "CARDSCANR_MARKET": market,
        "CARDSCANR_CURRENCY": CURRENCY[market],
        "CARDSCANR_MARKETPLACE_HOME": definition.homepage,
        "MAX_LIVE_SUBMISSIONS_PER_HOUR": "20",
        "MAX_LIVE_SUBMISSIONS_PER_DAY": "200",
    }
    os.environ.pop("PRE_SUBMIT_ONLY", None)
    os.environ.pop("CARDSCANR_PRE_SUBMIT_ONLY", None)
    for key, value in values.items():
        os.environ[key] = value
    return values


def precheck(market: str) -> dict:
    definition = region_definition(market)
    gate = evaluate_ebay_browser_work_gate(market=market, for_probe=False)
    xvfb = ensure_xvfb()
    return {
        "market": market,
        "currency": definition.currency,
        "host": definition.marketplace_host,
        "homepage": definition.homepage,
        "locale": definition.locale,
        "searchMode": definition.search_mode,
        "soldLabels": list(definition.sold_labels),
        "browserCapable": definition.browser_capable,
        "dispatchable": True,
        "PRE_SUBMIT_ONLY": os.getenv("PRE_SUBMIT_ONLY"),
        "ownedDailyFlag": owned_daily_full_enable(),
        "gateAllowed": gate.allowed,
        "gateReasons": list(gate.reason_codes),
        "availability": gate.availability_state,
        "activeChallenges": gate.active_challenge_count,
        "jpBlocked": classify_continuous_gate(market="JP")["workerState"] == "BLOCKED",
        "euBlocked": classify_continuous_gate(market="EU")["workerState"] == "BLOCKED",
        "xvfbReady": bool(xvfb.get("ok")),
        "xvfbReason": xvfb.get("reason"),
        "status": multi_region_status(),
        "ok": bool(
            definition.browser_capable
            and definition.currency == CURRENCY[market]
            and gate.active_challenge_count == 0
            and gate.state_integrity_ok
            and bool(xvfb.get("ok"))
            and os.getenv("PRE_SUBMIT_ONLY") not in {"1", "true"}
        ),
    }


def run_cycle(market: str, *, dry_run: bool) -> dict:
    bind_browser_env(market)
    if dry_run:
        os.environ["OWNED_DAILY_DRY_RUN"] = "true"
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    ART.mkdir(parents=True, exist_ok=True)
    sched = subprocess.run(
        [sys.executable, "-u", str(ROOT / "workers" / "owned_daily_price_scheduler.py"), "--once", "--no-sync-keys"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    worker = None
    if not dry_run:
        worker = subprocess.run(
            [
                sys.executable,
                "-u",
                str(ROOT / "workers" / "market_price_worker.py"),
                "--once",
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=1500,
        )
    return {
        "market": market,
        "dryRun": dry_run,
        "schedulerExit": sched.returncode,
        "schedulerStdout": (sched.stdout or "")[-4000:],
        "schedulerStderr": (sched.stderr or "")[-2000:],
        "workerExit": None if worker is None else worker.returncode,
        "workerStdout": None if worker is None else (worker.stdout or "")[-6000:],
        "workerStderr": None if worker is None else (worker.stderr or "")[-2000:],
        "at": utc_now(),
    }


def main() -> int:
    market = (sys.argv[1] if len(sys.argv) > 1 else "US").strip().upper()
    mode = (sys.argv[2] if len(sys.argv) > 2 else "precheck").strip().lower()
    ART.mkdir(parents=True, exist_ok=True)
    check = precheck(market)
    (ART / f"{market}_precheck.json").write_text(json.dumps(check, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"precheck": {k: v for k, v in check.items() if k != "status"}}, indent=2, default=str))
    if mode == "precheck":
        return 0 if check["ok"] else 2
    if not check["ok"]:
        return 2
    result = run_cycle(market, dry_run=(mode == "dry"))
    (ART / f"{market}_{mode}_cycle.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in result if "Stdout" not in k and "Stderr" not in k}, indent=2))
    if result.get("schedulerStdout"):
        print("--- scheduler ---")
        print(result["schedulerStdout"][-1500:])
    if result.get("workerStdout"):
        print("--- worker ---")
        print(result["workerStdout"][-1500:])
    return 0 if (result["schedulerExit"] == 0 and (mode == "dry" or result["workerExit"] == 0)) else 3


if __name__ == "__main__":
    raise SystemExit(main())
