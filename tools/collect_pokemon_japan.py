#!/usr/bin/env python3
"""Collect official Japanese Pokémon TCG cards."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cardscanr_worldwide.collectors.pokemon_japan import Collector


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--mode", choices=("inventory", "full"), default="inventory")
    parser.add_argument("--delay-seconds", type=float, default=0.35)
    args = parser.parse_args()
    collector = Collector(args.runtime_root, args.delay_seconds)
    try:
        print(json.dumps(collector.run(args.mode), indent=2, sort_keys=True))
    finally:
        collector.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

