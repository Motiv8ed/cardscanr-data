#!/usr/bin/env python3
"""Build production EN/JP packed catalogues from the live v1 search SQLite."""
from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.catalogue_packs import build_all_packs, sha256_file
from cardscanr_search_index.publication import load_publication_config
from cardscanr_search_index.v1_to_global_pack_source import (
    convert_v1_search_to_global_pack_source,
)

DEFAULT_PUBLIC_BASE = "https://assets.cardscanr.com"
# Lowercase UTC stamp so a generated id matches this explicit-id pattern.
_EXPLICIT_RELEASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,80}$")
_GENERATED_RELEASE_PREFIX = "production-packs-en-jp"
_MANIFEST_FILENAMES = (
    "catalogue.packs.manifest.json",
    "catalogue.packs.manifest.public.json",
)


class ReleaseIdError(ValueError):
    """Explicit release id is invalid, or that id already has a local manifest."""


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


def build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--catalogue-release-id",
        default="",
        help=(
            "Immutable catalogue release id. When omitted or blank, a unique id "
            "is generated. Explicit ids must match [a-z0-9][a-z0-9._-]{2,80}."
        ),
    )
    parser.add_argument(
        "--allow-existing-release-id",
        action="store_true",
        help=(
            "Allow a release id that already has a manifest in --output-dir. "
            "Off by default. Checks local manifests only."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "cloudflare_env.local.json",
        help=(
            "Publication config JSON. When the file is missing, falls back to "
            f"{DEFAULT_PUBLIC_BASE}. When present but invalid, the build fails."
        ),
    )
    return parser


def validate_explicit_release_id(raw: str) -> str:
    release_id = raw.strip()
    if not _EXPLICIT_RELEASE_ID_RE.fullmatch(release_id):
        raise ReleaseIdError(
            "invalid --catalogue-release-id "
            f"{raw!r}: expected [a-z0-9][a-z0-9._-]{{2,80}} after trimming whitespace"
        )
    return release_id


def _content_hash_prefix(content_hash: str | None) -> str:
    if content_hash:
        digest = content_hash.strip().lower()
        if re.fullmatch(r"[0-9a-f]{8,}", digest):
            return digest[:8]
    return secrets.token_hex(4)


def generate_release_id(
    *,
    content_hash: str | None = None,
    now: datetime | None = None,
) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    stamp = moment.strftime("%Y%m%dt%H%M%Sz")
    suffix = _content_hash_prefix(content_hash)
    return validate_explicit_release_id(
        f"{_GENERATED_RELEASE_PREFIX}-{stamp}-{suffix}"
    )


def _manifest_release_id(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("catalogueReleaseId")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def persist_release_manifest(output_dir: Path, release_id: str) -> Path | None:
    """Keep a per-release copy after the root manifest is written.

    The next build overwrites ``catalogue.packs.manifest.json``. The copy under
    ``output_dir/<release_id>/`` is what a later reuse check still finds.
    """
    source = output_dir / "catalogue.packs.manifest.json"
    if not source.is_file():
        return None
    destination = output_dir / release_id / "catalogue.packs.manifest.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination


def existing_release_manifest(output_dir: Path, release_id: str) -> Path | None:
    """Return a local manifest already bound to release_id, if one exists.

    Checks the root manifests and the per-release copy. Does not query R2 or
    any other remote store.
    """
    if not output_dir.is_dir():
        return None
    candidates = [output_dir / name for name in _MANIFEST_FILENAMES]
    release_dir = output_dir / release_id
    candidates.extend(release_dir / name for name in _MANIFEST_FILENAMES)
    for path in candidates:
        if path.is_file() and _manifest_release_id(path) == release_id:
            return path
    return None


def resolve_catalogue_release_id(
    explicit: str,
    *,
    output_dir: Path,
    allow_existing: bool = False,
    content_hash: str | None = None,
    now: datetime | None = None,
) -> str:
    """Use an explicit release id, or generate one when it is omitted.

    Generated ids are logged to stderr and then follow the same manifest path
    as an explicit id. Reuse of a local manifest is refused unless
    allow_existing is set.
    """
    if explicit.strip():
        release_id = validate_explicit_release_id(explicit)
        generated = False
    else:
        release_id = generate_release_id(content_hash=content_hash, now=now)
        generated = True
    existing = existing_release_manifest(output_dir, release_id)
    if existing is not None and not allow_existing:
        raise ReleaseIdError(
            f"release id {release_id!r} already exists at {existing}. "
            "Refusing to reuse an immutable release id. "
            "Pass --allow-existing-release-id to reuse it locally, "
            "or pass a different --catalogue-release-id."
        )
    if generated:
        print(f"generated catalogue release id: {release_id}", file=sys.stderr)
    return release_id


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Reject a bad or already-used explicit id before any build output is written.
    release_id: str | None = None
    if args.catalogue_release_id.strip():
        try:
            release_id = resolve_catalogue_release_id(
                args.catalogue_release_id,
                output_dir=args.output_dir,
                allow_existing=args.allow_existing_release_id,
            )
        except ReleaseIdError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

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

    if release_id is None:
        content_hash = sha256_file(source_db) if source_db.is_file() else None
        try:
            release_id = resolve_catalogue_release_id(
                "",
                output_dir=args.output_dir,
                allow_existing=args.allow_existing_release_id,
                content_hash=content_hash,
            )
        except ReleaseIdError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

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
    persist_release_manifest(args.output_dir, release_id)
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
