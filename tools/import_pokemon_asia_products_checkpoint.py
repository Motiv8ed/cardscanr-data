#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.pokemon_asia_products_import import import_checkpoint


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--locale", required=True, choices=["hk", "tw", "th", "id", "sg", "my", "ph"])
    args = parser.parse_args()
    print(json.dumps(import_checkpoint(args.database, args.checkpoint, args.locale), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
