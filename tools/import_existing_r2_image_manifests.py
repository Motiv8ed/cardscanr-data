#!/usr/bin/env python3
"""Import preserved CardScanR R2 image manifests into worldwide staging."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.existing_r2_image_import import import_existing_r2_manifests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--manifest", required=True, action="append", type=Path)
    parser.add_argument("--app-catalogue-root", type=Path)
    args = parser.parse_args()
    print(json.dumps(import_existing_r2_manifests(
        args.database, args.manifest, app_catalogue_root=args.app_catalogue_root,
    ), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
