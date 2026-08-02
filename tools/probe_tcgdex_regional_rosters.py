#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_worldwide.tcgdex_regional_probe import Probe, report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--delay-seconds", type=float, default=0.2)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    probe = Probe(args.runtime_root, args.delay_seconds)
    try:
        counters = probe.run(args.database)
    finally:
        probe.close()
    payload = report(args.runtime_root / "checkpoint.sqlite")
    payload["counters"] = counters
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"counters": counters, "status": payload["status"]}, indent=2))
    return 1 if payload["status"].get("fetch_failed") or payload["status"].get("invalid_payload") else 0


if __name__ == "__main__":
    raise SystemExit(main())
