#!/usr/bin/env python3
"""Normalize Portuguese scope and classify exhausted regional roster gaps."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cardscanr_worldwide.regional_blockers import classify_derived_regional_blockers, write_report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()
    result = classify_derived_regional_blockers(args.database)
    write_report(result, args.report_dir)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

