#!/usr/bin/env python3
"""Report CardScanR static dataset coverage for app-readiness checks."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
V1_DIR = ROOT / "public" / "v1"
MANIFEST_PATH = V1_DIR / "images" / "cards-manifest.json"


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def iter_catalog_cards(v1_dir: Path = V1_DIR):
    catalog_root = v1_dir / "catalog" / "pokemon"
    for language in ["en", "jp"]:
        cards_dir = catalog_root / language / "cards"
        if not cards_dir.exists():
            continue
        for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.lower()):
            payload = load_json(path)
            cards = payload.get("cards") if isinstance(payload, dict) else None
            if not isinstance(cards, list):
                continue
            for card in cards:
                if isinstance(card, dict):
                    yield card


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = load_json(path)
    return payload if isinstance(payload, dict) else None


def count_existing_local_binaries(records: list[dict[str, Any]], root: Path = ROOT) -> int:
    count = 0
    for record in records:
        for field in ["localImageSmallPath", "localImageLargePath"]:
            value = record.get(field)
            if not isinstance(value, str) or not value:
                continue
            path = Path(value)
            if not path.is_absolute():
                path = root / path
            if path.exists() and path.is_file():
                count += 1
    return count


def price_source_counts(v1_dir: Path = V1_DIR) -> Counter[str]:
    counts: Counter[str] = Counter()
    prices_root = v1_dir / "prices" / "current" / "pokemon"
    if not prices_root.exists():
        return counts
    for path in sorted(prices_root.rglob("*.json")):
        if path.name == "status.json":
            continue
        payload = load_json(path)
        if not isinstance(payload, dict):
            continue
        prices = payload.get("prices")
        if isinstance(prices, list):
            for entry in prices:
                if isinstance(entry, dict):
                    source = entry.get("source") or payload.get("source") or "unknown"
                    counts[str(source)] += 1
    return counts


def build_report(v1_dir: Path = V1_DIR, manifest_path: Path = MANIFEST_PATH) -> dict[str, Any]:
    catalog_cards = list(iter_catalog_cards(v1_dir))
    catalog_ids = {str(card.get("canonicalBaseId")) for card in catalog_cards if card.get("canonicalBaseId")}

    manifest = load_manifest(manifest_path)
    records = manifest.get("records", []) if isinstance(manifest, dict) else []
    if not isinstance(records, list):
        records = []
    record_dicts = [record for record in records if isinstance(record, dict)]
    manifest_ids = {
        str(record.get("canonicalCardId"))
        for record in record_dicts
        if isinstance(record.get("canonicalCardId"), str) and record.get("canonicalCardId")
    }
    status_counts = Counter(str(record.get("cacheStatus") or "missing") for record in record_dicts)
    small_url_count = sum(1 for record in record_dicts if isinstance(record.get("imageSmallUrl"), str) and record["imageSmallUrl"])
    large_url_count = sum(1 for record in record_dicts if isinstance(record.get("imageLargeUrl"), str) and record["imageLargeUrl"])
    local_binary_count = count_existing_local_binaries(record_dicts)
    source_counts = price_source_counts(v1_dir)
    total_price_records = sum(source_counts.values())

    manifest_covers_catalog = bool(catalog_ids) and catalog_ids.issubset(manifest_ids)
    manifest_urls_complete = bool(record_dicts) and small_url_count == len(record_dicts) and large_url_count == len(record_dicts)
    no_bad_cache_status = not any(status_counts.get(status, 0) for status in ["failed", "skipped", "missing"])
    cdn_or_cached_ready = any(status_counts.get(status, 0) for status in ["cdn_ready", "cached"])

    app_test_ready = bool(catalog_cards) and bool(record_dicts) and manifest_urls_complete and total_price_records > 0
    production_data_ready = (
        app_test_ready
        and manifest_covers_catalog
        and no_bad_cache_status
        and cdn_or_cached_ready
    )

    return {
        "catalogueCardCount": len(catalog_cards),
        "imageManifestRecordCount": len(record_dicts),
        "cardsWithSmallImageUrl": small_url_count,
        "cardsWithLargeImageUrl": large_url_count,
        "cacheStatusCounts": dict(sorted(status_counts.items())),
        "localCachedBinaryCount": local_binary_count,
        "priceSourceCounts": dict(sorted(source_counts.items())),
        "APP_TEST_READY": "yes" if app_test_ready else "no",
        "PRODUCTION_DATA_READY": "yes" if production_data_ready else "no",
    }


def print_report(report: dict[str, Any]) -> None:
    print("CardScanR dataset coverage")
    print("=" * 32)
    print(f"catalogue card count: {report['catalogueCardCount']}")
    print(f"image manifest record count: {report['imageManifestRecordCount']}")
    print(f"cards with small image URL: {report['cardsWithSmallImageUrl']}")
    print(f"cards with large image URL: {report['cardsWithLargeImageUrl']}")
    print("cacheStatus counts:")
    for status, count in report["cacheStatusCounts"].items():
        print(f"  {status}: {count}")
    print(f"local cached binary count: {report['localCachedBinaryCount']}")
    print("price source counts:")
    for source, count in report["priceSourceCounts"].items():
        print(f"  {source}: {count}")
    print(f"APP_TEST_READY: {report['APP_TEST_READY']}")
    print(f"PRODUCTION_DATA_READY: {report['PRODUCTION_DATA_READY']}")


def main() -> None:
    print_report(build_report())


if __name__ == "__main__":
    main()
