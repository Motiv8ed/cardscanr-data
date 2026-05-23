#!/usr/bin/env python3
"""Refresh public/v1/index.json from files already on disk."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / "public"
V1_DIR = PUBLIC_DIR / "v1"
INDEX_PATH = V1_DIR / "index.json"
SCHEMA_VERSION = "1.0.0"

DEFAULT_TTL_SECONDS = 86400
PRICE_TTL_SECONDS = 43200
DIAGNOSTICS_TTL_SECONDS = 900
KNOWN_DIAGNOSTIC_IDS = {
    "latest-build.json": "diagnostics",
    "pokewallet-catalog-foundation-latest.json": "diagnostics_pokewallet_catalog_foundation",
    "pokewallet-jp-price-build-latest.json": "diagnostics_pokewallet_jp_price_build",
    "pokewallet-pro-price-probe-latest.json": "diagnostics_pokewallet_pro_price_probe",
    "pokewallet-pro-trial-discovery-latest.json": "diagnostics_pokewallet_pro_trial_discovery",
    "pokewallet-probe-latest.json": "diagnostics_pokewallet_probe",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_json_if_changed(path: Path, payload: Any) -> bool:
    encoded = json_bytes(payload)
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(encoded)
    os.replace(tmp_path, path)
    return True


def safe_id_part(value: str) -> str:
    return value.replace("\\", "_").replace("/", "_").replace(".", "_")


def file_url(path: Path) -> str:
    return f"/v1/{path.relative_to(V1_DIR).as_posix()}"


def existing_entries_by_id() -> dict[str, dict[str, Any]]:
    if not INDEX_PATH.exists():
        return {}
    try:
        payload = load_json(INDEX_PATH)
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("datasets") if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        return {}
    return {
        str(entry.get("id")): entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }


def existing_index_payload() -> dict[str, Any]:
    if not INDEX_PATH.exists():
        return {}
    try:
        payload = load_json(INDEX_PATH)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def dataset_entry(
    *,
    dataset_id: str,
    path: Path,
    dataset_type: str,
    description: str,
    ts: str,
    existing: dict[str, dict[str, Any]],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    game: str | None = None,
    language: str | None = None,
    schema_version: str | None = SCHEMA_VERSION,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = existing.get(dataset_id, {})
    digest = sha256_file(path)
    updated_at = ts
    if previous.get("sha256") == digest and isinstance(previous.get("updatedAtUtc"), str):
        updated_at = str(previous["updatedAtUtc"])

    entry: dict[str, Any] = {
        "id": dataset_id,
        "url": file_url(path),
        "sha256": digest,
        "type": previous.get("type") or dataset_type,
        "description": previous.get("description") or description,
        "updatedAtUtc": updated_at,
        "recommendedCacheTtlSeconds": int(previous.get("recommendedCacheTtlSeconds") or ttl_seconds),
    }
    if schema_version is not None:
        entry["schemaVersion"] = schema_version
    if game is not None:
        entry["game"] = game
    if language is not None:
        entry["language"] = language
    if extra:
        entry.update(extra)
    return entry


def maybe_add(entries: list[dict[str, Any]], **kwargs: Any) -> None:
    path = kwargs.get("path")
    if isinstance(path, Path) and path.exists():
        entries.append(dataset_entry(**kwargs))


def catalog_set_name(path: Path) -> str:
    try:
        payload = load_json(path)
    except (OSError, json.JSONDecodeError):
        return path.stem
    return str(payload.get("setName") or payload.get("name") or path.stem) if isinstance(payload, dict) else path.stem


def build_entries(ts: str, existing: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    core_files = [
        ("app_config", V1_DIR / "app-config.json", "app_config", "CardScanR remote app settings", None),
        ("api_manifest", V1_DIR / "api-manifest.json", "api_manifest", "CardScanR internal data API manifest", None),
        ("api_notes", V1_DIR / "api-notes.json", "api_notes", "CardScanR internal app data notes", None),
        ("schemas", V1_DIR / "schemas.json", "schemas", "Machine-readable CardScanR cache schema docs", None),
        (
            "supported_languages",
            V1_DIR / "supported-languages.json",
            "supported_languages",
            "CardScanR supported language and catalogue availability manifest",
            None,
        ),
        (
            "supported_markets",
            V1_DIR / "supported-markets.json",
            "supported_markets",
            "CardScanR supported market and pricing availability manifest",
            None,
        ),
        (
            "supported_sources",
            V1_DIR / "supported-sources.json",
            "supported_sources",
            "CardScanR supported price/image source manifest",
            None,
        ),
        ("prices_status", V1_DIR / "prices" / "status.json", "price_status", "CardScanR price freshness/status summary", "pokemon"),
        (
            "tracked_history",
            V1_DIR / "history" / "tracked-cards.json",
            "tracked_history",
            "CardScanR tracked price history summary",
            None,
        ),
        (
            "images_cache_policy",
            V1_DIR / "images" / "cache-policy.json",
            "image_cache_policy",
            "CardScanR image cache policy metadata",
            None,
        ),
        (
            "images_cards_manifest",
            V1_DIR / "images" / "cards-manifest.json",
            "image_manifest",
            "CardScanR card image URL/cache manifest",
            None,
        ),
    ]
    for dataset_id, path, dataset_type, description, game in core_files:
        maybe_add(
            entries,
            dataset_id=dataset_id,
            path=path,
            dataset_type=dataset_type,
            description=description,
            ts=ts,
            existing=existing,
            game=game,
            ttl_seconds=DIAGNOSTICS_TTL_SECONDS if dataset_type == "diagnostics" else DEFAULT_TTL_SECONDS,
        )

    diagnostics_dir = V1_DIR / "diagnostics"
    if diagnostics_dir.exists():
        for path in sorted(diagnostics_dir.glob("*.json"), key=lambda item: item.name.lower()):
            dataset_id = KNOWN_DIAGNOSTIC_IDS.get(path.name, f"diagnostics_{safe_id_part(path.stem)}")
            maybe_add(
                entries,
                dataset_id=dataset_id,
                path=path,
                dataset_type="diagnostics",
                description=f"CardScanR diagnostics: {path.stem}",
                ts=ts,
                existing=existing,
                ttl_seconds=DIAGNOSTICS_TTL_SECONDS,
            )

    sample_root = V1_DIR / "prices" / "pokemon"
    if sample_root.exists():
        for path in sorted(sample_root.glob("*/sample.json"), key=lambda item: item.as_posix().lower()):
            language = path.parent.name
            maybe_add(
                entries,
                dataset_id=f"prices_pokemon_{language}",
                path=path,
                dataset_type="price_current",
                description=f"Pokemon TCG {language.upper()} current tracked prices",
                ts=ts,
                existing=existing,
                ttl_seconds=PRICE_TTL_SECONDS,
                game="pokemon",
                language=language,
            )

    current_root = V1_DIR / "prices" / "current" / "pokemon"
    if current_root.exists():
        for language_dir in sorted([item for item in current_root.iterdir() if item.is_dir()], key=lambda item: item.name.lower()):
            language = language_dir.name
            status_path = language_dir / "status.json"
            maybe_add(
                entries,
                dataset_id=f"prices_current_pokemon_{language}_status",
                path=status_path,
                dataset_type="price_current_status",
                description=f"CardScanR price freshness/status for Pokemon {language.upper()}",
                ts=ts,
                existing=existing,
                game="pokemon",
                language=language,
            )
            for path in sorted(language_dir.glob("*.json"), key=lambda item: item.name.lower()):
                if path.name == "status.json":
                    continue
                maybe_add(
                    entries,
                    dataset_id=f"prices_current_pokemon_{language}_{path.stem}",
                    path=path,
                    dataset_type="price_current",
                    description=f"Pokemon TCG {language.upper()} latest-known current prices for {path.stem}",
                    ts=ts,
                    existing=existing,
                    ttl_seconds=PRICE_TTL_SECONDS,
                    game="pokemon",
                    language=language,
                )

    catalog_root = V1_DIR / "catalog" / "pokemon"
    if catalog_root.exists():
        for language_dir in sorted([item for item in catalog_root.iterdir() if item.is_dir()], key=lambda item: item.name.lower()):
            language = language_dir.name
            maybe_add(
                entries,
                dataset_id=f"catalog_pokemon_{language}_sets",
                path=language_dir / "sets.json",
                dataset_type="catalogue_sets",
                description=f"Pokemon TCG {language.upper()} catalogue sets",
                ts=ts,
                existing=existing,
                game="pokemon",
                language=language,
            )
            cards_dir = language_dir / "cards"
            if cards_dir.exists():
                for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.lower()):
                    maybe_add(
                        entries,
                        dataset_id=f"catalog_pokemon_{language}_cards_{path.stem}",
                        path=path,
                        dataset_type="catalogue_cards",
                        description=f"Pokemon TCG {language.upper()} catalogue cards for {catalog_set_name(path)}",
                        ts=ts,
                        existing=existing,
                        game="pokemon",
                        language=language,
                    )

    provider_root = V1_DIR / "provider-catalog" / "pokewallet"
    provider_files = [
        ("provider_catalog_pokewallet_status", provider_root / "status.json", "provider_catalog_status", "Pokewallet provider catalogue status"),
        (
            "provider_catalog_pokewallet_languages_summary",
            provider_root / "languages-summary.json",
            "provider_catalog_summary",
            "Pokewallet provider catalogue language coverage summary",
        ),
        (
            "provider_catalog_pokewallet_sets_summary",
            provider_root / "sets-summary.json",
            "provider_catalog_summary",
            "Pokewallet provider catalogue set coverage summary",
        ),
        (
            "provider_catalog_pokewallet_cards_manifest",
            provider_root / "cards-manifest.json",
            "provider_catalog_manifest",
            "Pokewallet provider catalogue cards manifest",
        ),
    ]
    for dataset_id, path, dataset_type, description in provider_files:
        maybe_add(
            entries,
            dataset_id=dataset_id,
            path=path,
            dataset_type=dataset_type,
            description=description,
            ts=ts,
            existing=existing,
        )

    cards_root = provider_root / "cards"
    if cards_root.exists():
        for path in sorted(cards_root.glob("*/*.json"), key=lambda item: item.as_posix().lower()):
            language = path.parent.name
            maybe_add(
                entries,
                dataset_id=f"provider_catalog_pokewallet_cards_{language}_{path.stem}",
                path=path,
                dataset_type="provider_catalog_cards",
                description=f"Pokewallet provider catalogue cards for {language} set {path.stem}",
                ts=ts,
                existing=existing,
                game="pokemon",
                language=language,
            )

    if HISTORY_DAILY_ROOT := (V1_DIR / "history" / "daily"):
        if HISTORY_DAILY_ROOT.exists():
            for path in sorted(HISTORY_DAILY_ROOT.glob("*/*/*/tracked.json"), key=lambda item: item.as_posix().lower()):
                try:
                    date, game, language = path.relative_to(HISTORY_DAILY_ROOT).parts[:3]
                except ValueError:
                    continue
                maybe_add(
                    entries,
                    dataset_id=f"daily_tracked_history_{game}_{language}_{date}",
                    path=path,
                    dataset_type="daily_tracked_history",
                    description=f"CardScanR tracked history snapshot for {game} {language.upper()} on {date}",
                    ts=ts,
                    existing=existing,
                    game=game,
                    language=language,
                    extra={"date": date},
                )

    deduped = {entry["id"]: entry for entry in entries}
    return sorted(deduped.values(), key=lambda entry: str(entry.get("id") or ""))


def material_index(payload: dict[str, Any]) -> dict[str, Any]:
    datasets = payload.get("datasets") if isinstance(payload, dict) else []
    material_datasets = []
    for entry in datasets if isinstance(datasets, list) else []:
        if not isinstance(entry, dict):
            continue
        material_datasets.append(
            {
                key: value
                for key, value in entry.items()
                if key not in {"updatedAtUtc", "recommendedCacheTtlSeconds", "description"}
            }
        )
    return {"datasets": sorted(material_datasets, key=lambda item: str(item.get("id") or ""))}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh public/v1/index.json from existing files.")
    parser.add_argument("--dry-run", action="store_true", help="Print summary without writing index.json.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ts = now_utc()
    existing_index = existing_index_payload()
    existing = existing_entries_by_id()
    entries = build_entries(ts, existing)
    next_index = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "cacheVersion": datetime.now(timezone.utc).strftime("%Y.%m.%d.%H%M"),
        "datasets": entries,
    }

    if material_index(existing_index) == material_index(next_index):
        if isinstance(existing_index.get("generatedAtUtc"), str):
            next_index["generatedAtUtc"] = existing_index["generatedAtUtc"]
        if isinstance(existing_index.get("cacheVersion"), str):
            next_index["cacheVersion"] = existing_index["cacheVersion"]

    changed = False if args.dry_run else write_json_if_changed(INDEX_PATH, next_index)
    print(
        json.dumps(
            {
                "schemaVersion": SCHEMA_VERSION,
                "generatedAtUtc": ts,
                "datasetCount": len(entries),
                "changed": changed,
                "dryRun": bool(args.dry_run),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
