#!/usr/bin/env python3
"""CLI for ensure_live_ebay_pricing_runtime.ps1 worker component health (local only)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.live_ebay_component_health import classify_live_ebay_worker_component_health


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify live eBay worker ensure-runtime health")
    parser.add_argument("--process-present", action="store_true", help="A matching worker PID is running")
    parser.add_argument("--pid", type=int, default=None, help="Worker process id when present")
    parser.add_argument("--component", default="worker", choices=("worker",))
    args = parser.parse_args()
    if args.component != "worker":
        raise SystemExit("Only worker classification is implemented")
    health = classify_live_ebay_worker_component_health(
        process_present=bool(args.process_present),
        process_id=args.pid,
    )
    print(json.dumps(health.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())