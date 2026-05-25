#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.scheduler import MarketPriceRefreshScheduler, MarketSchedulerConfig
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


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
