#!/usr/bin/env python3
"""Register missing language/region sealed-product catalogues as explicit blockers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.regional_product_blockers import register_regional_product_blockers, write_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--scope", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()
    result = register_regional_product_blockers(args.database, args.scope)
    write_report(result, args.report_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

