#!/usr/bin/env python3
"""Verify and register an immutable worldwide publication bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.publication_registry import register_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--status", choices=("canary", "verified", "active"), default="canary")
    parser.add_argument("--previous-version")
    args = parser.parse_args()
    print(json.dumps(register_bundle(
        args.database, args.bundle, status=args.status, previous_version=args.previous_version,
    ), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
