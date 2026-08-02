#!/usr/bin/env python3
"""Acquire and validate card-image candidates from approved or link-only providers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.card_image_validation import acquire, apply_results, checkpoint_counts, register_candidates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--delay-seconds", type=float, default=0.05)
    parser.add_argument("--provider", action="append", dest="providers", required=True)
    parser.add_argument("--acquire-only", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    checkpoint = args.runtime_root / "checkpoint.sqlite"
    output = {"registered": register_candidates(args.database, checkpoint)}
    output["acquired"] = acquire(checkpoint, args.runtime_root / "cache", args.workers, args.limit,
                                  args.delay_seconds, args.providers, args.retry_failed)
    output["checkpoint"] = checkpoint_counts(checkpoint)
    if not args.acquire_only:
        output["applied"] = apply_results(args.database, checkpoint)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
