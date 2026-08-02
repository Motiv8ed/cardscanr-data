#!/usr/bin/env python3
"""Create evidence-backed provisional regional printing rosters."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.regional_skeletons import DEFAULT_LANGUAGES, import_regional_skeletons


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--languages", nargs="+", default=list(DEFAULT_LANGUAGES))
    args = parser.parse_args()
    print(json.dumps(import_regional_skeletons(args.database, args.languages), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

