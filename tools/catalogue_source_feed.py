#!/usr/bin/env python3
"""Owner-editable, bounded CardScanR catalogue source inbox and collector."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_worldwide.source_feed import add_seed, add_source, load_feed, run_batch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", type=Path, default=ROOT / "config" / "catalogue_source_feed.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="Validate source feed without network access")
    add = sub.add_parser("add-url", help="Feed a known index, card detail or direct image URL")
    add.add_argument("--source", required=True)
    add.add_argument("--kind", required=True, choices=["index", "card", "image"])
    add.add_argument("--url", required=True)
    add.add_argument("--printing-key", help="Required for direct image seed; never auto-matches printings")
    provider = sub.add_parser("add-source", help="Register a new disabled source without editing code")
    provider.add_argument("--id", required=True)
    provider.add_argument("--name", required=True)
    provider.add_argument("--region", required=True, help="TW, HK, CN, KR, etc")
    provider.add_argument("--language", required=True, help="zh-Hant, zh-Hans, ko, ja, etc")
    provider.add_argument("--page-host", required=True)
    provider.add_argument("--page-path", required=True)
    provider.add_argument("--image-host", required=True)
    provider.add_argument("--image-path", required=True)
    collect = sub.add_parser("collect", help="Run one resumable batch to private archive")
    collect.add_argument("--source", required=True)
    collect.add_argument("--archive", type=Path, default=Path(os.environ.get("CARDSCANR_ARCHIVE", "D:/CardScanR_Archive")))
    collect.add_argument("--max-items", type=int, default=25)
    collect.add_argument("--pace-seconds", type=float, default=1.0)
    collect.add_argument("--dry-run", action="store_true", help="Show valid queue seed counts; never fetch")
    args = parser.parse_args()
    if args.command == "add-url":
        changed = add_seed(args.feed, args.source, args.kind, args.url, args.printing_key)
        print("added" if changed else "already present")
    elif args.command == "add-source":
        add_source(args.feed, source_id=args.id, name=args.name, region=args.region,
                   language=args.language, page_host=args.page_host, page_path=args.page_path,
                   image_host=args.image_host, image_path=args.image_path)
        print(f"Registered {args.id} as disabled; review selectors/rights before enabling")
    elif args.command == "validate":
        _, sources = load_feed(args.feed)
        print(json.dumps({"valid": True, "sources": [
            {"id": sid, "enabled": src["enabled"], "seeds": len(src["seeds"])} for sid, src in sources.items()]}, indent=2))
    else:
        _, sources = load_feed(args.feed)
        src = sources[args.source]
        if args.dry_run:
            print(json.dumps({"source": args.source, "enabled": src["enabled"], "seeds": len(src["seeds"]),
                              "archive": str(args.archive), "network_used": False}, indent=2))
        else:
            print(json.dumps(run_batch(args.feed, args.source, args.archive,
                  max_items=args.max_items, pace=args.pace_seconds), indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, ValueError, PermissionError, OSError, RuntimeError) as error:
        print(f"catalogue feed: {error}", file=sys.stderr)
        raise SystemExit(2) from error