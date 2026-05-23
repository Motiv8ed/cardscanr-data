#!/usr/bin/env python3
"""Read-only Provider -> App catalogue gap report."""

from __future__ import annotations

import argparse
import json
import sys

from promote_provider_catalog_to_app_catalog import analyse_provider_to_app, selected_languages


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report provider catalogue records not represented in app catalogue.")
    parser.add_argument("--languages", default="en,jp", help="Comma-separated app-supported languages to evaluate.")
    parser.add_argument("--include-zh", action="store_true", help="Treat ZH as enabled for this report.")
    parser.add_argument("--summary-only", action="store_true", help="Omit per-provider records from the JSON output.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    languages = selected_languages(args.languages, include_zh=args.include_zh)
    if "zh" in languages and not args.include_zh:
        languages = [language for language in languages if language != "zh"]
    report = analyse_provider_to_app(languages, include_zh=args.include_zh)
    if args.summary_only:
        report = {key: value for key, value in report.items() if key != "providerRecords"}
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
