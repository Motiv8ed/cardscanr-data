#!/usr/bin/env python3
"""Import a preserved CardScanR missing-image registry package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cardscanr_worldwide.missing_image_registry import import_registry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--package", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(import_registry(args.database, args.package), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

