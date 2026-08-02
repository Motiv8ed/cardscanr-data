#!/usr/bin/env python3
"""Collect public Pokémon Asia card inventories with durable HTTP caching."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_worldwide.collectors.pokemon_asia import Collector, SUPPORTED_LOCALES


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--locale", required=True, choices=sorted(SUPPORTED_LOCALES))
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--mode", choices=("inventory", "full"), default="inventory")
    parser.add_argument("--delay-seconds", type=float, default=0.35)
    args = parser.parse_args()
    collector = Collector(args.locale, args.runtime_root, args.delay_seconds)
    try:
        print(json.dumps(collector.run(args.mode), indent=2, sort_keys=True, ensure_ascii=False))
    finally:
        collector.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
