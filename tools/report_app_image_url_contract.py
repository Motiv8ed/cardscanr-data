#!/usr/bin/env python3
"""Report app-facing image URL contract health for small known problem areas."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
V1_DIR = ROOT / "public" / "v1"
APP_CATALOG_ROOT = V1_DIR / "catalog" / "pokemon"
PROVIDER_ROOT = V1_DIR / "provider-catalog" / "pokewallet" / "cards"
IMAGE_FIELDS = ("imageUrl", "imageSmall", "imageLarge", "imageUrlSmall", "imageUrlLarge")
REQUIRED_FIELDS = (*IMAGE_FIELDS, "providerImageSource")


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def iter_card_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.glob("*.json"), key=lambda item: item.name.lower())


def card_records(path: Path) -> list[dict[str, Any]]:
    try:
        payload = load_json(path)
    except (OSError, json.JSONDecodeError):
        return []
    cards = payload.get("cards") if isinstance(payload, dict) else None
    return [card for card in cards if isinstance(card, dict)] if isinstance(cards, list) else []


def is_bad_display_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value.replace("\\", "/").lower()
    return normalized.endswith(".json") or "provider-catalog/" in normalized


def is_relative_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    return not (
        value.startswith("http://")
        or value.startswith("https://")
        or value.startswith("/")
        or value.startswith("data:")
    )


def sample_card(card: dict[str, Any], path: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "setId": card.get("setId"),
        "collectorNumber": card.get("collectorNumber"),
        "name": card.get("name") or card.get("displayName") or card.get("cleanName"),
    }


def summarize_app_catalogue(language: str, *, sample_limit: int) -> dict[str, Any]:
    cards_dir = APP_CATALOG_ROOT / language / "cards"
    files = iter_card_files(cards_dir)
    summary: dict[str, Any] = {
        "language": language,
        "fileCount": len(files),
        "cardCount": 0,
        "missingImageFieldCount": 0,
        "relativeImageUrlCount": 0,
        "unsafeDisplayImageUrlCount": 0,
        "missingImageFieldSamples": [],
        "relativeImageUrlSamples": [],
        "unsafeDisplayImageUrlSamples": [],
        "sampleCards": [],
    }
    for path in files:
        for card in card_records(path):
            summary["cardCount"] += 1
            if len(summary["sampleCards"]) < sample_limit:
                summary["sampleCards"].append(sample_card(card, path))
            missing = [field for field in REQUIRED_FIELDS if field not in card]
            if missing:
                summary["missingImageFieldCount"] += 1
                if len(summary["missingImageFieldSamples"]) < sample_limit:
                    item = sample_card(card, path)
                    item["missingFields"] = missing
                    summary["missingImageFieldSamples"].append(item)
            relative_fields = [field for field in IMAGE_FIELDS if is_relative_url(card.get(field))]
            if relative_fields:
                summary["relativeImageUrlCount"] += 1
                if len(summary["relativeImageUrlSamples"]) < sample_limit:
                    item = sample_card(card, path)
                    item["fields"] = relative_fields
                    summary["relativeImageUrlSamples"].append(item)
            unsafe_fields = [field for field in IMAGE_FIELDS if is_bad_display_url(card.get(field))]
            if unsafe_fields:
                summary["unsafeDisplayImageUrlCount"] += 1
                if len(summary["unsafeDisplayImageUrlSamples"]) < sample_limit:
                    item = sample_card(card, path)
                    item["fields"] = unsafe_fields
                    summary["unsafeDisplayImageUrlSamples"].append(item)
    return summary


def summarize_provider_catalogue(language: str, *, sample_limit: int) -> dict[str, Any]:
    cards_dir = PROVIDER_ROOT / language
    files = iter_card_files(cards_dir)
    summary: dict[str, Any] = {
        "language": language,
        "fileCount": len(files),
        "cardCount": 0,
        "missingImageFieldCount": 0,
        "relativeImageEndpointCount": 0,
        "unsafeDisplayImageUrlCount": 0,
        "missingImageFieldSamples": [],
        "relativeImageEndpointSamples": [],
        "unsafeDisplayImageUrlSamples": [],
        "sampleCards": [],
    }
    for path in files:
        for card in card_records(path):
            summary["cardCount"] += 1
            if len(summary["sampleCards"]) < sample_limit:
                summary["sampleCards"].append(sample_card(card, path))
            missing = [field for field in REQUIRED_FIELDS if field not in card]
            if missing:
                summary["missingImageFieldCount"] += 1
                if len(summary["missingImageFieldSamples"]) < sample_limit:
                    item = sample_card(card, path)
                    item["missingFields"] = missing
                    summary["missingImageFieldSamples"].append(item)
            endpoint_fields = [
                field
                for field in ("imageEndpoint", "imageEndpointLow", "imageEndpointHigh")
                if is_relative_url(card.get(field))
            ]
            if endpoint_fields:
                summary["relativeImageEndpointCount"] += 1
                if len(summary["relativeImageEndpointSamples"]) < sample_limit:
                    item = sample_card(card, path)
                    item["fields"] = endpoint_fields
                    summary["relativeImageEndpointSamples"].append(item)
            unsafe_fields = [field for field in IMAGE_FIELDS if is_bad_display_url(card.get(field))]
            if unsafe_fields:
                summary["unsafeDisplayImageUrlCount"] += 1
                if len(summary["unsafeDisplayImageUrlSamples"]) < sample_limit:
                    item = sample_card(card, path)
                    item["fields"] = unsafe_fields
                    summary["unsafeDisplayImageUrlSamples"].append(item)
    return summary


def build_report(sample_limit: int) -> dict[str, Any]:
    app = {language: summarize_app_catalogue(language, sample_limit=sample_limit) for language in ("en", "jp")}
    provider = {"jp": summarize_provider_catalogue("jp", sample_limit=sample_limit)}
    return {
        "schemaVersion": "1.0.0",
        "checks": {
            "requiredAppImageFields": list(REQUIRED_FIELDS),
            "forbiddenDisplayImageUrlPatterns": ["provider-catalog/", "*.json"],
            "providerCatalogIsInternal": True,
            "imageBinariesBulkStoredInRepo": False,
        },
        "appCatalogue": app,
        "providerCatalogue": provider,
        "safeDisplayImageUrls": all(
            summary["unsafeDisplayImageUrlCount"] == 0 for summary in [*app.values(), *provider.values()]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-limit", type=int, default=5)
    args = parser.parse_args()
    report = build_report(max(0, args.sample_limit))
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    return 0 if report["safeDisplayImageUrls"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
