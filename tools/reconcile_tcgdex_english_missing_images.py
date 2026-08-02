#!/usr/bin/env python3
"""Verify and persist exact TCGdex attempts for the English missing-image queue."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.image_attempt_reconciliation import (
    matched_urls,
    observe_urls,
    reconcile_tcgdex_english_missing_images,
    write_report,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--require-status", type=int)
    parser.add_argument("--report-json", type=Path)
    parser.add_argument("--report-md", type=Path)
    args = parser.parse_args()
    observations = observe_urls(matched_urls(args.database), workers=args.workers, timeout=args.timeout)
    result = reconcile_tcgdex_english_missing_images(
        args.database, observations, require_status=args.require_status
    )
    if bool(args.report_json) != bool(args.report_md):
        parser.error("--report-json and --report-md must be supplied together")
    if args.report_json and args.report_md:
        write_report(result, args.report_json, args.report_md)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
