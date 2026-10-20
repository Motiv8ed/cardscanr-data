#!/usr/bin/env python3
"""Build production EN/JP packed catalogues from the live v1 search SQLite."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.catalogue_packs import build_all_packs
from cardscanr_search_index.publication import load_publication_config
from cardscanr_search_index.v1_to_global_pack_source import (
    convert_v1_search_to_global_pack_source,
)

DEFAULT_PUBLIC_BASE = "https://assets.cardscanr.com"


def resolve_public_base_url(config_path: Path | None) -> str:
    """Resolve client-facing pack URL base.

    Falls back to ``DEFAULT_PUBLIC_BASE`` only when no config path is supplied,
    the path does not exist, or neither asset-base key is present. An
    explicitly supplied non-HTTPS / non-string value raises (key presence, not
    truthiness).
    """
    public_base = DEFAULT_PUBLIC_BASE
    if config_path is None or not config_path.exists():
        return public_base

    # Prefer the custom assets domain for client-facing pack URLs. The local
    # Cloudflare env may list r2PublicDevUrl first for S3 uploads; packs must
    # still resolve via assets.cardscanr.com when no override is present.
    try:
        # Validate JSON shape via the shared loader, then select asset keys by
        # presence so empty/null/http values cannot silently fall back.
        load_publication_config(config_path)
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config root must be a JSON object")
        if "r2CatalogueAssetsBaseUrl" in raw:
            candidate = raw["r2CatalogueAssetsBaseUrl"]
        elif "r2PublicBaseUrl" in raw:
            candidate = raw["r2PublicBaseUrl"]
        else:
            return public_base
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid publication config {config_path}: {exc}") from exc
    if not isinstance(candidate, str) or not candidate.startswith("https://"):
        raise ValueError(
            f"invalid publication config {config_path}: "
            f"asset base URL must be https, got {candidate!r}"
        )
    return candidate.rstrip("/")


def require_nonzero_production_language_counts(convert_report: Mapping[str, Any]) -> None:
    """Reject incomplete EN/JP conversion before writing a public manifest."""
    counts = convert_report.get("languageCounts") or {}
    if not isinstance(counts, Mapping):
        raise ValueError("convert report missing languageCounts mapping")
    selected = convert_report.get("languages") or []
    if not selected:
        raise ValueError("convert report selected zero languages")
    missing = [
        str(lang)
        for lang in selected
        if int(counts.get(lang) or 0) <= 0
    ]
    if missing:
        raise ValueError(
            "incomplete production source: expected nonzero card counts for "
            f"{sorted(selected)}, missing/zero for {sorted(missing)} "
            f"(counts={dict(counts)})"
        )


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "cloudflare_env.local.json",
        help=(
            "Publication config JSON. When the file is missing, falls back to "
            f"{DEFAULT_PUBLIC_BASE}. When present but invalid, the build fails."
        ),
    )
    args = parser.parse_args(argv)

    languages = [part.strip() for part in args.languages.split(",") if part.strip()]
    work = args.output_dir / "_source"
    work.mkdir(parents=True, exist_ok=True)
    source_db = work / "global_pack_source_en_ja.sqlite"
    convert_report = convert_v1_search_to_global_pack_source(
        v1_db=args.v1_database,
        output_db=source_db,
        languages=tuple(languages),
    )
    require_nonzero_production_language_counts(convert_report)
    (args.output_dir / "v1_to_global_convert_report.json").write_text(
        json.dumps(convert_report, indent=2) + "\n",
        encoding="utf-8",
    )

    public_base = resolve_public_base_url(args.config)

    release_id = args.catalogue_release_id or "production-packs-en-jp-20260926"
    # include_sealed=True: build_all_packs omits sealed-products when the
    # converted source has zero sealed rows (v1 converter currently inserts none).
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
