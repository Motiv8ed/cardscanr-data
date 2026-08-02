#!/usr/bin/env python3
"""Crosswalk Japanese missing-image records to official candidates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.japanese_missing_image_reconciliation import reconcile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--report-json", type=Path)
    parser.add_argument("--report-md", type=Path)
    args = parser.parse_args()
    report = reconcile(args.database, args.report_json, args.report_md)
    print(json.dumps({key: value for key, value in report.items() if key != "items"}, indent=2, sort_keys=True))
    return 0 if not report.get("ambiguous_exact_identity") else 1


if __name__ == "__main__":
    raise SystemExit(main())

