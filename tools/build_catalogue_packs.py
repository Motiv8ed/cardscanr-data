#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.catalogue_packs import build_all_packs


def main() -> int:
    parser = argparse.ArgumentParser(description="Build versioned catalogue packs from a monolithic SQLite index.")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--public-base-url", default="")
    parser.add_argument("--catalogue-release-id", default="")
    parser.add_argument(
        "--languages",
        default="",
        help=(
            "Comma-separated pack languages to build (e.g. en,ja). "
            "jp is accepted as an alias for ja. When omitted, all LANGUAGE_PACKS are built."
        ),
    )
    parser.add_argument(
        "--skip-sealed",
        action="store_true",
        help="Omit sealed-products pack (still builds core + selected language packs).",
    )
    args = parser.parse_args()

    if not args.database.exists():
        parser.error(f"database not found: {args.database}")

    language_filter = None
    if args.languages.strip():
        language_filter = [part.strip() for part in args.languages.split(",") if part.strip()]

    manifest = build_all_packs(
        source_db=args.database,
        output_dir=args.output_dir,
        public_base_url=args.public_base_url or None,
        catalogue_release_id=args.catalogue_release_id or None,
        languages=language_filter,
        include_sealed=not args.skip_sealed,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest.get("classification") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
