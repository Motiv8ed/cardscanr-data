#!/usr/bin/env python3
"""Hydrate an identical English Asia inventory from a completed official checkpoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.asia_shared_details import hydrate_shared_details


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-checkpoint", required=True, type=Path)
    parser.add_argument("--target-checkpoint", required=True, type=Path)
    parser.add_argument("--source-locale", required=True)
    parser.add_argument("--target-locale", required=True)
    args = parser.parse_args()
    result = hydrate_shared_details(
        args.source_checkpoint, args.target_checkpoint, args.source_locale, args.target_locale
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

