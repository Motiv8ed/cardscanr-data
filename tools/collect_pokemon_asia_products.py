#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.collectors.pokemon_asia_products import Collector


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--locale", required=True, choices=["hk", "tw", "th", "id", "sg", "my", "ph"])
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--delay-seconds", type=float, default=0.35)
    args = parser.parse_args()
    collector = Collector(args.locale, args.runtime_root, args.delay_seconds)
    try:
        print(json.dumps(collector.run(), indent=2, sort_keys=True))
    finally:
        collector.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
