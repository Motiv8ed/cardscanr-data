#!/usr/bin/env python3
"""Build production EN/JP packed catalogues from the live v1 search SQLite."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.catalogue_packs import build_all_packs
from cardscanr_search_index.publication import load_publication_config
from cardscanr_search_index.v1_to_global_pack_source import (
    convert_v1_search_to_global_pack_source,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--v1-database",
        type=Path,
        default=ROOT / "public/v1/catalog/pokemon/search/catalog_search_v1.sqlite",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "artifacts/local/packs_en_jp_production",
    )
    parser.add_argument("--languages", default="en,ja")
    parser.add_argument("--catalogue-release-id", default="")
    parser.add_argument("--config", type=Path, default=ROOT / "cloudflare_env.local.json")
    args = parser.parse_args()

    languages = [part.strip() for part in args.languages.split(",") if part.strip()]
    work = args.output_dir / "_source"
    work.mkdir(parents=True, exist_ok=True)
    source_db = work / "global_pack_source_en_ja.sqlite"
    convert_report = convert_v1_search_to_global_pack_source(
        v1_db=args.v1_database,
        output_db=source_db,
        languages=tuple(languages),
    )
    (args.output_dir / "v1_to_global_convert_report.json").write_text(
        json.dumps(convert_report, indent=2) + "\n",
        encoding="utf-8",
    )

    # Prefer the custom assets domain for client-facing pack URLs. The local
    # Cloudflare env may list r2PublicDevUrl first for S3 uploads; packs must
    # still resolve via assets.cardscanr.com.
    public_base = "https://assets.cardscanr.com"
    if args.config.exists():
        try:
            cfg = load_publication_config(args.config)
            candidate = getattr(cfg, "r2_catalogue_assets_base_url", None) or None
            if not candidate:
                raw = json.loads(args.config.read_text(encoding="utf-8"))
                candidate = raw.get("r2CatalogueAssetsBaseUrl") or raw.get(
                    "r2PublicBaseUrl"
                )
            if isinstance(candidate, str) and candidate.startswith("https://"):
                public_base = candidate.rstrip("/")
        except Exception:  # noqa: BLE001
            pass

    release_id = args.catalogue_release_id or "production-packs-en-jp-20260926"
    manifest = build_all_packs(
        source_db=source_db,
        output_dir=args.output_dir,
        public_base_url=public_base,
        catalogue_release_id=release_id,
        languages=languages,
        include_sealed=True,
    )
    # Strip local filesystem paths from the public-facing copy used for activation.
    public_manifest = json.loads(json.dumps(manifest))
    for pack in public_manifest.get("packs", []):
        for key in ("sqlitePath", "gzipPath"):
            pack.pop(key, None)
    public_path = args.output_dir / "catalogue.packs.manifest.public.json"
    public_path.write_text(
        json.dumps(public_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"convert": convert_report, "manifest": manifest}, indent=2))
    return 0 if manifest.get("classification") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
