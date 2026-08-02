#!/usr/bin/env python3
"""Classify known evidence-exhausted unresolved items."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.external_blocker_finalization import finalize_external_blockers, write_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--include-missing-card-images", action="store_true")
    args = parser.parse_args()
    result = finalize_external_blockers(args.database, args.include_missing_card_images)
    write_report(result, args.report_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

