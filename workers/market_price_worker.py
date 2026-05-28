#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.providers import create_market_comps_provider
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


def utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the CardScanR market price worker.")
    parser.add_argument("--once", action="store_true", help="Process one poll cycle and exit.")
    parser.add_argument("--max-cycles", type=int, default=0, help="Optional cycle limit for loop mode.")
    parser.add_argument("--max-jobs", type=int, default=0, help="Override MARKET_WORKER_MAX_JOBS_PER_RUN.")
    parser.add_argument("--poll-seconds", type=int, default=0, help="Override MARKET_WORKER_POLL_SECONDS.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = MarketEngineConfig.from_env(require_supabase=True)
    if config.worker_concurrency != 1:
        print("[market-engine] MARKET_WORKER_CONCURRENCY>1 is not used for local browser provider; continuing sequentially.")

    client = SupabaseMarketEngineClient(
        supabase_url=config.supabase_url,
        service_role_key=config.supabase_service_role_key,
    )
    runner = MarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider(config.provider_name),
        config=config,
    )
    poll_seconds = args.poll_seconds if args.poll_seconds > 0 else config.poll_seconds
    max_jobs = args.max_jobs if args.max_jobs > 0 else config.max_jobs_per_run
    cycle = 0

    while True:
        cycle += 1
        started_at = utc_iso()
        results = runner.run_once(max_jobs=max_jobs)
        summary = {
            "startedAtUtc": started_at,
            "finishedAtUtc": utc_iso(),
            "cycle": cycle,
            "workerId": config.worker_id,
            "provider": config.provider_name,
            "jobCount": len(results),
            "results": results,
        }
        write_json(config.latest_report_path, summary)
        append_jsonl(config.runs_report_path, summary)
        print(f"[market-engine] cycle={cycle} jobCount={len(results)} report={config.latest_report_path}")

        if args.once:
            return 0
        if args.max_cycles > 0 and cycle >= args.max_cycles:
            return 0
        time.sleep(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
