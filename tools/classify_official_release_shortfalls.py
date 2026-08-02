#!/usr/bin/env python3
"""Classify unexplained official_count shortfalls on set_release rows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_worldwide.official_release_shortfall_classification import (
    classify_official_release_shortfalls,
    write_report,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports" / "worldwide_catalogue")
    args = parser.parse_args()
    result = classify_official_release_shortfalls(args.database.resolve())
    json_path, md_path = write_report(result, args.output_dir.resolve())
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
