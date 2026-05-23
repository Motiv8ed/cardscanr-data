#!/usr/bin/env python3
"""Build a compact release summary for CardScanR data."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_V1 = ROOT / "public" / "v1"


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def try_load_json(path: Path) -> Any:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def app_catalogue_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    root = PUBLIC_V1 / "catalog" / "pokemon"
    if not root.exists():
        return counts
    for language_dir in sorted([item for item in root.iterdir() if item.is_dir()], key=lambda item: item.name.lower()):
        cards_dir = language_dir / "cards"
        total = 0
        if cards_dir.exists():
            for path in sorted(cards_dir.glob("*.json"), key=lambda item: item.name.lower()):
                payload = try_load_json(path)
                cards = payload.get("cards") if isinstance(payload, dict) else None
                if isinstance(cards, list):
                    total += len([card for card in cards if isinstance(card, dict)])
        counts[language_dir.name] = total
    return dict(sorted(counts.items()))


def image_manifest_counts() -> tuple[dict[str, int], int]:
    path = PUBLIC_V1 / "images" / "cards-manifest.json"
    payload = try_load_json(path)
    records = payload.get("records") if isinstance(payload, dict) else []
    if not isinstance(records, list):
        records = []

    counts: Counter[str] = Counter()
    local_cached = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        language = str(record.get("language") or "unknown")
        counts[language] += 1
        for field in ("localImageSmallPath", "localImageLargePath"):
            raw_path = record.get(field)
            if not isinstance(raw_path, str) or not raw_path:
                continue
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = ROOT / candidate
            if candidate.exists() and candidate.is_file():
                local_cached += 1
    return dict(sorted(counts.items())), local_cached


def provider_counts() -> dict[str, int]:
    payload = try_load_json(PUBLIC_V1 / "provider-catalog" / "pokewallet" / "status.json")
    languages = payload.get("languages") if isinstance(payload, dict) else None
    counts: dict[str, int] = {}
    if isinstance(languages, dict):
        for language, item in languages.items():
            if isinstance(item, dict):
                counts[str(language)] = int(item.get("cardCount") or 0)
    return dict(sorted(counts.items()))


def current_price_count(language: str) -> int:
    root = PUBLIC_V1 / "prices" / "current" / "pokemon" / language
    if not root.exists():
        return 0
    total = 0
    for path in sorted(root.glob("*.json"), key=lambda item: item.name.lower()):
        if path.name == "status.json":
            continue
        payload = try_load_json(path)
        prices = payload.get("prices") if isinstance(payload, dict) else None
        if isinstance(prices, list):
            total += len([record for record in prices if isinstance(record, dict)])
    return total


def blocked_reason_counts(languages: list[str], include_zh: bool) -> dict[str, int]:
    report_path = ROOT / "reports" / "provider_blocked_cards_latest.json"
    payload = try_load_json(report_path)
    if isinstance(payload, dict) and isinstance(payload.get("blockedReasonCounts"), dict):
        return {
            str(key): int(value or 0)
            for key, value in sorted(payload["blockedReasonCounts"].items(), key=lambda item: str(item[0]))
        }

    try:
        from report_provider_blocked_cards import build_report
    except Exception:
        return {}

    try:
        report = build_report(languages, include_zh=include_zh, sample_limit=0)
    except Exception:
        return {}

    blocked = report.get("blockedReasonCounts") if isinstance(report, dict) else {}
    if not isinstance(blocked, dict):
        return {}
    return {
        str(key): int(value or 0)
        for key, value in sorted(blocked.items(), key=lambda item: str(item[0]))
    }


def total_count(values: dict[str, int]) -> int:
    return int(sum(int(value or 0) for value in values.values()))


def parse_languages(raw: str) -> list[str]:
    languages: list[str] = []
    for item in str(raw or "en,jp").split(","):
        value = item.strip().lower()
        if value and value not in languages:
            languages.append(value)
    return languages or ["en", "jp"]


def build_summary(languages: list[str], include_zh: bool) -> dict[str, Any]:
    app_counts = app_catalogue_counts()
    image_counts, local_cached_images = image_manifest_counts()
    provider_lang_counts = provider_counts()
    blocked_counts = blocked_reason_counts(languages, include_zh)

    price_counts = {
        "en": current_price_count("en"),
        "jp": current_price_count("jp"),
    }

    return {
        "appCatalogue": {
            "byLanguage": app_counts,
            "total": total_count(app_counts),
        },
        "imageManifest": {
            "byLanguage": image_counts,
            "total": total_count(image_counts),
        },
        "providerCatalogue": {
            "byLanguage": provider_lang_counts,
            "total": total_count(provider_lang_counts),
        },
        "prices": {
            "en": int(price_counts["en"]),
            "jp": int(price_counts["jp"]),
            "total": int(price_counts["en"] + price_counts["jp"]),
        },
        "blockedRecordsByReason": blocked_counts,
        "localCachedImageCount": int(local_cached_images),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize generated CardScanR release data.")
    parser.add_argument("--languages", default="en,jp", help="Comma-separated languages for blocked-record analysis.")
    parser.add_argument("--include-zh", action="store_true", help="Treat zh as enabled when computing blocked records.")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON instead of pretty JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = build_summary(parse_languages(args.languages), include_zh=args.include_zh)
    if args.compact:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
