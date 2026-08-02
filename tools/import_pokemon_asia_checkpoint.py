#!/usr/bin/env python3
"""Import a stable completed Pokémon Asia collector checkpoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_worldwide.pokemon_asia_import import LOCALE_SCOPE, import_checkpoint


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--locale", required=True, choices=sorted(LOCALE_SCOPE))
    args = parser.parse_args()
    print(json.dumps(import_checkpoint(args.database.resolve(), args.checkpoint.resolve(), args.locale),
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
