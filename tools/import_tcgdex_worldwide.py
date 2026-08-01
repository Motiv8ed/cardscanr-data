#!/usr/bin/env python3
"""CLI for importing a TCGdex exporter JSONL into CardScanR staging SQLite."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_worldwide.tcgdex import import_jsonl


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--jsonl", required=True, type=Path)
    parser.add_argument("--source-version", required=True)
    args = parser.parse_args()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    counters = import_jsonl(args.database.resolve(), args.jsonl.resolve(), args.source_version)
    print(json.dumps(counters, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
