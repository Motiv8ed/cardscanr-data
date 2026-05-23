#!/usr/bin/env python3
"""Diagnose missing collector-number provider records with raw field signals."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from promote_provider_catalog_to_app_catalog import ROOT, selected_languages
from report_provider_blocked_cards import classify_provider_records


SCHEMA_VERSION = "1.0.0"
REPORT_JSON_PATH = ROOT / "reports" / "missing_collector_raw_fields_latest.json"
REPORT_MD_PATH = ROOT / "reports" / "missing_collector_raw_fields_latest.md"

NUMBER_HINT_PATTERN = re.compile(r"(?:#\s*\d+[A-Za-z]?|\b[A-Z]{1,5}[- ]?\d{1,4}[A-Za-z]?\b|\b\d{1,4}/[A-Za-z0-9-]+\b)")
PRODUCT_PATTERN = re.compile(
    r"\b(code card|deck|battle|gym|league|collection|box|pack|product|starter|theme|energy|trainer|stadium|misc|promo|world championship|wcd\d*)\b",
    re.IGNORECASE,
)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_json_if_changed(path: Path, payload: Any) -> bool:
    encoded = json_bytes(payload)
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(encoded)
    tmp_path.replace(path)
    return True


def write_text_if_changed(path: Path, text: str) -> bool:
    encoded = text.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(encoded)
    tmp_path.replace(path)
    return True


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def compact_card_source(card: dict[str, Any]) -> dict[str, Any]:
    basis = card.get("imageCacheIdentityBasis") if isinstance(card.get("imageCacheIdentityBasis"), dict) else {}
    return {
        "providerCardId": card.get("providerCardId"),
        "providerLanguage": card.get("providerLanguage"),
        "cardScanRLanguage": card.get("cardScanRLanguage"),
        "providerSetId": card.get("providerSetId"),
        "providerSetCode": card.get("providerSetCode"),
        "providerSetName": card.get("providerSetName"),
        "name": card.get("name"),
        "cleanName": card.get("cleanName"),
        "cardNumber": card.get("cardNumber"),
        "variants": card.get("variants"),
        "rawKeys": card.get("rawKeys"),
        "hasPriceFields": card.get("hasPriceFields"),
        "hasTcgplayerFields": card.get("hasTcgplayerFields"),
        "hasCardmarketFields": card.get("hasCardmarketFields"),
        "imageEndpoint": card.get("imageEndpoint"),
        "imageEndpointLow": card.get("imageEndpointLow"),
        "imageEndpointHigh": card.get("imageEndpointHigh"),
        "providerCanonicalImageKey": card.get("providerCanonicalImageKey"),
        "imageCacheKey": card.get("imageCacheKey"),
        "imageCacheIdentityBasis": {
            "setId": basis.get("setId"),
            "collectorNumber": basis.get("collectorNumber"),
            "normalizedName": basis.get("normalizedName"),
            "variant": basis.get("variant"),
            "basisConfidence": basis.get("basisConfidence"),
        },
    }


def provider_card_lookup() -> dict[str, dict[str, dict[str, Any]]]:
    root = ROOT / "public" / "v1" / "provider-catalog" / "pokewallet" / "cards"
    lookup: dict[str, dict[str, dict[str, Any]]] = {}
    if not root.exists():
        return lookup
    for path in sorted(root.glob("*/*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        cards = payload.get("cards")
        if not isinstance(cards, list):
            continue
        by_id: dict[str, dict[str, Any]] = {}
        for card in cards:
            if not isinstance(card, dict):
                continue
            card_id = normalize_text(card.get("providerCardId"))
            if card_id:
                by_id[card_id] = card
        rel = path.relative_to(ROOT).as_posix()
        lookup[rel] = by_id
    return lookup


def explicit_number_values(raw_fields: dict[str, Any]) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for key, value in raw_fields.items():
        if key in {
            "name",
            "cleanName",
            "originalName",
            "providerCanonicalImageKey",
            "imageCacheKey",
        }:
            continue
        text = normalize_text(value)
        if not text:
            continue
        if text.lower() in {"unknown", "none", "null", "n/a"}:
            continue
        if len(text) > 40:
            continue
        values.append({"field": key, "value": text})
    return values


def unsafe_title_number_values(title_candidates: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for item in title_candidates:
        text = normalize_text(item.get("value"))
        if text and text not in values:
            values.append(text)
    return values


def set_key(row: dict[str, Any]) -> str:
    language = normalize_text(row.get("language")) or "unknown"
    code = normalize_text(row.get("providerSetCode") or row.get("providerSetId")) or "unknown"
    name = normalize_text(row.get("providerSetName")) or "unknown"
    return f"{language}|{code}|{name}"


def is_product_like(row: dict[str, Any]) -> bool:
    text = " ".join(
        [
            normalize_text(row.get("name")),
            normalize_text(row.get("providerSetCode")),
            normalize_text(row.get("providerSetName")),
            " ".join(str(item) for item in (row.get("looksLike") or [])),
        ]
    )
    return bool(PRODUCT_PATTERN.search(text))


def classify_missing_row(row: dict[str, Any], source_card: dict[str, Any] | None) -> tuple[str, list[str], list[dict[str, str]], list[str]]:
    raw_fields = row.get("rawNumberFields") if isinstance(row.get("rawNumberFields"), dict) else {}
    title_candidates = row.get("titleNumberCandidates") if isinstance(row.get("titleNumberCandidates"), list) else []
    explicit_numbers = explicit_number_values(raw_fields)
    unsafe_numbers = unsafe_title_number_values(title_candidates)
    notes: list[str] = []

    if explicit_numbers:
        notes.append("explicit_number_field_present")
        return "raw_number_available", notes, explicit_numbers, unsafe_numbers

    if unsafe_numbers:
        notes.append("number_like_token_only_in_name_fields")
        return "unsafe_name_only_number", notes, explicit_numbers, unsafe_numbers

    if is_product_like(row):
        notes.append("appears_product_or_unnumbered")
        return "true_unnumbered_or_product", notes, explicit_numbers, unsafe_numbers

    raw_keys = []
    if source_card and isinstance(source_card.get("rawKeys"), list):
        raw_keys = [str(item) for item in source_card["rawKeys"]]
    has_card_info_signal = "card_info" in raw_keys
    has_market_signal = "tcgplayer" in raw_keys or "cardmarket" in raw_keys
    if has_card_info_signal or has_market_signal:
        if has_card_info_signal:
            notes.append("raw_payload_has_card_info_but_no_stored_number")
        if has_market_signal:
            notes.append("raw_payload_has_market_fields")
        return "likely_provider_parser_gap", notes, explicit_numbers, unsafe_numbers

    return "unknown", notes, explicit_numbers, unsafe_numbers


def build_report(languages: list[str], *, include_zh: bool, sample_limit: int) -> dict[str, Any]:
    rows = classify_provider_records(languages, include_zh=include_zh)
    missing = [row for row in rows if str(row.get("reason")) == "missing_collector_number"]

    source_lookup = provider_card_lookup()
    classification_counts: Counter[str] = Counter()
    class_by_language: dict[str, Counter[str]] = defaultdict(Counter)
    top_sets_by_class: dict[str, Counter[str]] = defaultdict(Counter)
    samples_by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    parser_gap_signals: Counter[str] = Counter()

    records: list[dict[str, Any]] = []
    for row in missing:
        provider_file = normalize_text(row.get("providerFile"))
        provider_card_id = normalize_text(row.get("providerCardId"))
        source_card = source_lookup.get(provider_file, {}).get(provider_card_id)
        classification, notes, explicit_numbers, unsafe_numbers = classify_missing_row(row, source_card)

        classification_counts[classification] += 1
        class_by_language[normalize_text(row.get("language")) or "unknown"][classification] += 1
        top_sets_by_class[classification][set_key(row)] += 1

        for note in notes:
            parser_gap_signals[note] += 1

        entry = {
            "classification": classification,
            "classificationNotes": notes,
            "language": row.get("language"),
            "providerCardId": row.get("providerCardId"),
            "providerFile": row.get("providerFile"),
            "providerSetId": row.get("providerSetId"),
            "providerSetCode": row.get("providerSetCode"),
            "providerSetName": row.get("providerSetName"),
            "name": row.get("name"),
            "looksLike": row.get("looksLike"),
            "rawNumberFields": row.get("rawNumberFields"),
            "explicitNumberCandidates": explicit_numbers,
            "unsafeTitleNumberCandidates": unsafe_numbers,
            "titleNumberCandidates": row.get("titleNumberCandidates"),
            "sourceCard": compact_card_source(source_card) if isinstance(source_card, dict) else None,
        }
        records.append(entry)
        if len(samples_by_class[classification]) < sample_limit:
            samples_by_class[classification].append(entry)

    records.sort(
        key=lambda item: (
            str(item.get("classification") or ""),
            str(item.get("language") or ""),
            str(item.get("providerSetCode") or ""),
            str(item.get("name") or ""),
        )
    )

    safe_recoverable = classification_counts.get("raw_number_available", 0)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": now_utc(),
        "languagesRequested": languages,
        "includeZh": include_zh,
        "missingCollectorNumberTotal": len(missing),
        "classificationCounts": dict(sorted(classification_counts.items())),
        "classificationCountsByLanguage": {
            language: dict(sorted(counter.items())) for language, counter in sorted(class_by_language.items())
        },
        "safeRecoverySummary": {
            "safeRecoverableCount": safe_recoverable,
            "remainingBlockedCount": len(missing) - safe_recoverable,
            "safeRecoveryReady": safe_recoverable > 0,
            "recommendation": (
                "No safe recovery candidate found in stored provider fields."
                if safe_recoverable == 0
                else "Safe recovery candidates exist in explicit stored number fields."
            ),
        },
        "parserGapSignals": dict(sorted(parser_gap_signals.items())),
        "topSetsByClassification": {
            category: [
                {
                    "language": key.split("|", 2)[0],
                    "providerSetCode": key.split("|", 2)[1],
                    "providerSetName": key.split("|", 2)[2],
                    "count": count,
                }
                for key, count in counter.most_common(30)
            ]
            for category, counter in sorted(top_sets_by_class.items())
        },
        "samplesByClassification": dict(samples_by_class),
        "records": records,
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Missing Collector Raw Fields",
        "",
        f"- generatedAtUtc: {report['generatedAtUtc']}",
        f"- languages: {', '.join(report['languagesRequested'])}",
        f"- includeZh: {str(report['includeZh']).lower()}",
        f"- missingCollectorNumberTotal: {report['missingCollectorNumberTotal']}",
        "",
        "## Classification Counts",
    ]

    for label, count in sorted(report["classificationCounts"].items(), key=lambda item: (-int(item[1]), item[0])):
        lines.append(f"- {label}: {count}")

    safe = report["safeRecoverySummary"]
    lines.extend(
        [
            "",
            "## Safe Recovery",
            f"- safeRecoverableCount: {safe['safeRecoverableCount']}",
            f"- remainingBlockedCount: {safe['remainingBlockedCount']}",
            f"- recommendation: {safe['recommendation']}",
        ]
    )

    lines.extend(["", "## Parser Gap Signals"])
    for label, count in sorted(report.get("parserGapSignals", {}).items(), key=lambda item: (-int(item[1]), item[0])):
        lines.append(f"- {label}: {count}")

    lines.extend(["", "## Top Sets By Classification"])
    top_sets = report.get("topSetsByClassification", {})
    for category in sorted(top_sets.keys()):
        lines.append(f"- {category}:")
        for item in top_sets.get(category, [])[:8]:
            lines.append(
                f"  - {item['language']} {item['providerSetCode']} ({item['providerSetName']}): {item['count']}"
            )

    lines.extend(["", "## Samples"])
    samples = report.get("samplesByClassification", {})
    for category in sorted(samples.keys()):
        lines.append(f"- {category}:")
        for item in samples.get(category, [])[:8]:
            lines.append(
                "  - "
                f"{item.get('language')} {item.get('providerSetCode')} {item.get('name')} "
                f"explicit={len(item.get('explicitNumberCandidates') or [])} "
                f"unsafeTitle={len(item.get('unsafeTitleNumberCandidates') or [])}"
            )

    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report raw fields for missing collector-number provider records.")
    parser.add_argument("--languages", default="en,jp", help="Comma-separated app-supported languages to evaluate.")
    parser.add_argument("--include-zh", action="store_true", help="Treat ZH as enabled instead of unsupported.")
    parser.add_argument("--sample-limit", type=int, default=50, help="Sample records to include per classification.")
    parser.add_argument("--no-report", action="store_true", help="Print summary only; do not write report files.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    languages = selected_languages(args.languages, include_zh=args.include_zh)
    if "zh" in languages and not args.include_zh:
        languages = [language for language in languages if language != "zh"]
    report = build_report(languages, include_zh=args.include_zh, sample_limit=max(1, int(args.sample_limit)))
    if not args.no_report:
        write_json_if_changed(REPORT_JSON_PATH, report)
        write_text_if_changed(REPORT_MD_PATH, markdown_report(report))

    summary = {
        "generatedAtUtc": report["generatedAtUtc"],
        "missingCollectorNumberTotal": report["missingCollectorNumberTotal"],
        "classificationCounts": report["classificationCounts"],
        "safeRecoverySummary": report["safeRecoverySummary"],
        "parserGapSignals": report["parserGapSignals"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
