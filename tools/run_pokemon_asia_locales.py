#!/usr/bin/env python3
"""Run Pokémon Asia locale collectors sequentially so one locale cannot block others."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_worldwide.collectors.pokemon_asia import Collector, SUPPORTED_LOCALES


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--locales", nargs="+", choices=sorted(SUPPORTED_LOCALES), required=True)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--mode", choices=("inventory", "full"), default="inventory")
    parser.add_argument("--delay-seconds", type=float, default=0.35)
    args = parser.parse_args()
    results = {}
    failed = False
    for locale in args.locales:
        collector = Collector(locale, args.runtime_root, args.delay_seconds)
        try:
            results[locale] = {"status": "completed", "counters": collector.run(args.mode)}
        except Exception as error:
            failed = True
            results[locale] = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
        finally:
            collector.close()
        print(json.dumps({locale: results[locale]}, ensure_ascii=False, sort_keys=True), flush=True)
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
