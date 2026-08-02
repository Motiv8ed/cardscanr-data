#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.publication import load_publication_config
from cardscanr_search_index.worldwide_publication import (
    ACTIVE_MANIFEST_KEY,
    activate_version,
    load_remote_manifest,
    publish_version,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish and optionally activate the worldwide catalogue index.")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "cloudflare_env.local.json")
    parser.add_argument("--previous-manifest-url")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--full-public-download", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--confirm-active-key", default="")
    args = parser.parse_args()

    if args.activate and args.confirm_active_key != ACTIVE_MANIFEST_KEY:
        parser.error(f"activation requires --confirm-active-key {ACTIVE_MANIFEST_KEY}")
    previous = load_remote_manifest(args.previous_manifest_url) if args.previous_manifest_url else None
    config = load_publication_config(args.config)
    published, client = publish_version(
        database_path=args.database,
        config=config,
        previous_manifest=previous,
        full_public_download=args.full_public_download,
    )
    result = activate_version(published=published, client=client, bucket=config.r2_bucket) if args.activate else published
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0 if result.classification == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
