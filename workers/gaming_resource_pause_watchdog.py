#!/usr/bin/env python3
"""Host watchdog loop for Fortnite-aware pricing pause (no game interaction)."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.gaming_resource_pause import (
    GamingResourcePauseController,
    poll_interval_seconds,
    status_payload,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="CardScanR gaming resource pause watchdog")
    parser.add_argument("--once", action="store_true", help="Single tick then exit")
    parser.add_argument("--seconds", type=int, default=0, help="Run for N seconds (0=forever)")
    args = parser.parse_args()
    ctrl = GamingResourcePauseController()
    started = time.time()
    while True:
        state = ctrl.tick()
        print(json.dumps(status_payload(state), indent=2), flush=True)
        if args.once:
            return 0
        if args.seconds and (time.time() - started) >= args.seconds:
            return 0
        time.sleep(poll_interval_seconds())


if __name__ == "__main__":
    raise SystemExit(main())
