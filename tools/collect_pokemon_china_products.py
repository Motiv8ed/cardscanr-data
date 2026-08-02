#!/usr/bin/env python3
"""Collect official mainland-China Pokémon TCG product pages."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_worldwide.collectors.pokemon_china_products import Collector


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--delay-seconds", type=float, default=0.35)
    args = parser.parse_args()
    collector = Collector(args.runtime_root, args.delay_seconds)
    try:
        print(json.dumps(collector.run(), ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        collector.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
