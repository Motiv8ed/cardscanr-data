#!/usr/bin/env python3
"""CLI for importing a pinned PokémonTCG repository checkout."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_worldwide.pokemontcg import import_repository


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-version", required=True)
    args = parser.parse_args()
    counters = import_repository(args.database.resolve(), args.source_root.resolve(), args.source_version)
    print(json.dumps(counters, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
