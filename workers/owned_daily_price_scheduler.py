#!/usr/bin/env python3
"""Run one owned-daily pricing scheduler cycle (or loop)."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.owned_daily_scheduler import (
    OwnedDailySchedulerConfig,
    OwnedPrintingRefreshScheduler,
)
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Owned-daily market price refresh scheduler.")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    parser.add_argument("--max-cycles", type=int, default=0, help="Optional cycle limit.")
    parser.add_argument("--poll-seconds", type=int, default=0, help="Loop sleep override.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without enqueue.")
    parser.add_argument("--max-enqueue", type=int, default=0, help="Override OWNED_DAILY_MAX_ENQUEUE.")
    parser.add_argument("--full-enable", action="store_true", help="Set OWNED_DAILY_FULL_ENABLE=true.")
    parser.add_argument("--no-sync-keys", action="store_true", help="Skip sync_owned_market_price_keys.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.dry_run:
        import os

        os.environ["OWNED_DAILY_DRY_RUN"] = "true"
    if args.max_enqueue > 0:
        import os

        os.environ["OWNED_DAILY_MAX_ENQUEUE"] = str(args.max_enqueue)
    if args.full_enable:
        import os

        os.environ["OWNED_DAILY_FULL_ENABLE"] = "true"
    if args.no_sync_keys:
        import os

        os.environ["OWNED_DAILY_SYNC_KEYS"] = "false"

    config = OwnedDailySchedulerConfig.from_env(require_supabase=True)
    client = SupabaseMarketEngineClient(
        supabase_url=config.supabase_url,
        service_role_key=config.supabase_service_role_key,
    )
    scheduler = OwnedPrintingRefreshScheduler(client=client, config=config)
    cycle = 0
    poll_seconds = args.poll_seconds if args.poll_seconds > 0 else 3600
    while True:
        cycle += 1
        report = scheduler.run_and_write_reports()
        summary = report.get("summary", {})
        metrics = report.get("metrics", {})
        print(
            "[owned-daily-scheduler] "
            f"cycle={cycle} targets={summary.get('targetsScanned', 0)} "
            f"due={summary.get('keysEligible', 0)} "
            f"enqueued={summary.get('jobsEnqueued', 0)} "
            f"gap={summary.get('capacityGap', 0)} "
            f"dryRun={report.get('dryRun', False)} "
            f"full={report.get('fullEnable', False)} "
            f"OWNED_PRICE_ENQUEUED={metrics.get('OWNED_PRICE_ENQUEUED')} "
            f"report={config.latest_report_path}"
        )
        if args.once:
            return 0
        if args.max_cycles > 0 and cycle >= args.max_cycles:
            return 0
        time.sleep(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
