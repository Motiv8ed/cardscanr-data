#!/usr/bin/env python3
"""Sanitize transient signed URLs from publishable staging records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cardscanr_worldwide.signed_url_sanitization import sanitize_signed_urls


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--provider", default="pokemon-cn-official")
    args = parser.parse_args()
    print(json.dumps(sanitize_signed_urls(args.database, args.provider), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

