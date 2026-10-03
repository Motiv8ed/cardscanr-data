#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.scheduler import MarketPriceRefreshScheduler, MarketSchedulerConfig
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


def _maybe_run_owned_daily(client: object) -> dict | None:
    """Once-daily owned pass via the existing scheduler process (no second competitor)."""
    enabled = os.getenv("OWNED_DAILY_FULL_ENABLE", "false").strip().lower() in {"1", "true", "yes", "on"}
    print(
        "[market-scheduler] owned-daily "
        f"FULL_ENABLE={str(enabled).lower()} "
        f"MAX_ENQUEUE={os.getenv('OWNED_DAILY_MAX_ENQUEUE', '')} "
        f"MODE={os.getenv('EBAY_BROWSER_MODE', os.getenv('EBAY_BROWSER_HEADLESS', ''))}"
    )
    if not enabled:
        return None
    stamp_path = ROOT / "reports" / "runtime" / "owned_daily_last_run_utc.txt"
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    force = os.getenv("OWNED_DAILY_FORCE_RERUN", "false").strip().lower() in {"1", "true", "yes", "on"}
    if stamp_path.exists() and not force:
        last = stamp_path.read_text(encoding="utf-8").strip()
        if last == today:
            return {"skipped": True, "reason": "already_ran_utc_day", "day": today, "fullEnable": True}
    from cardscanr_market_engine.owned_daily_scheduler import (
        OwnedDailySchedulerConfig,
        OwnedPrintingRefreshScheduler,
    )

    owned_cfg = OwnedDailySchedulerConfig.from_env(require_supabase=True)
    report = OwnedPrintingRefreshScheduler(client=client, config=owned_cfg).run_and_write_reports()
    stamp_path.write_text(today, encoding="utf-8")
    return {
        "skipped": False,
        "day": today,
        "summary": report.get("summary"),
        "metrics": report.get("metrics"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the CardScanR market price refresh scheduler.")
    parser.add_argument("--once", action="store_true", help="Run one scheduling cycle and exit.")
    parser.add_argument("--max-cycles", type=int, default=0, help="Optional cycle limit for loop mode.")
    parser.add_argument("--poll-seconds", type=int, default=0, help="Override MARKET_SCHEDULER_POLL_SECONDS.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = MarketSchedulerConfig.from_env(require_supabase=True)
    client = SupabaseMarketEngineClient(
        supabase_url=config.supabase_url,
        service_role_key=config.supabase_service_role_key,
    )
    scheduler = MarketPriceRefreshScheduler(client=client, config=config)

    cycle = 0
    poll_seconds = args.poll_seconds if args.poll_seconds > 0 else config.poll_seconds
    while True:
        cycle += 1
        try:
            owned = _maybe_run_owned_daily(client)
            if owned and not owned.get("skipped"):
                print(
                    "[market-scheduler] owned-daily "
                    f"enqueued={(owned.get('summary') or {}).get('jobsEnqueued')} "
                    f"gap={(owned.get('summary') or {}).get('capacityGap')}"
                )
        except Exception as exc:
            print(f"[market-scheduler] owned-daily pass failed: {exc}")
        report = scheduler.run_and_write_reports()
        summary = report.get("summary", {})
        print(
            "[market-scheduler] "
            f"cycle={cycle} candidates={summary.get('candidatesScanned', 0)} "
            f"enqueued={summary.get('jobsEnqueued', 0)} dryRun={report.get('dryRun', False)} "
            f"report={config.latest_report_path}"
        )
        if args.once:
            return 0
        if args.max_cycles > 0 and cycle >= args.max_cycles:
            return 0
        time.sleep(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
