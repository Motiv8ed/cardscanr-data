#!/usr/bin/env python3
"""Write JSON and Markdown reports for a normalized worldwide staging DB."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_worldwide.reporting import build_report, markdown


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    args = parser.parse_args()
    report = build_report(args.database.resolve())
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"json": str(args.json.resolve()), "markdown": str(args.markdown.resolve()),
                      "counts": report["counts"], "integrity": report["integrity"]}, indent=2))
    return 0 if report["integrity"]["sqlite_integrity_check"] == "ok" and not report["integrity"]["foreign_key_failure_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
