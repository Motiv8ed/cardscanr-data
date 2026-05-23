#!/usr/bin/env python3
"""Report provider catalogue cards that are not promoted into the app catalogue."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from promote_provider_catalog_to_app_catalog import (
    APP_ROOT,
    PROVIDER_ROOT,
    ROOT,
    SCHEMA_VERSION,
    build_app_set_token_map,
    build_candidate,
    build_existing_identity_indexes,
    collector_identity_key,
    iter_provider_records,
    load_app_sets,
    make_position_key,
    selected_languages,
    summarize_duplicate_groups,
)


REPORT_JSON_PATH = ROOT / "reports" / "provider_blocked_cards_latest.json"
REPORT_MD_PATH = ROOT / "reports" / "provider_blocked_cards_latest.md"

NUMBER_FIELD_NAMES = (
    "collectorNumber",
    "number",
    "cardNumber",
    "cardNo",
    "printedNumber",
    "localId",
    "setNumber",
    "code",
)
NESTED_NUMBER_FIELDS = (
    ("imageCacheIdentityBasis", "collectorNumber"),
)
LETTER_PREFIX_TEST_VALUES = (
    "BST 123",
    "CRE 043",
    "EVS 055",
    "RCL 165",
    "SM-P",
    "S-P",
    "SV-P",
    "001/SV-P",
    "TG01/TG30",
    "GG01/GG70",
    "RC1/RC25",
    "XY-P",
    "055",
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


def get_nested(card: dict[str, Any], path: tuple[str, str]) -> Any:
    current: Any = card
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def raw_number_fields(card: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {field: card.get(field) for field in NUMBER_FIELD_NAMES}
    for path in NESTED_NUMBER_FIELDS:
        fields[".".join(path)] = get_nested(card, path)
    fields["name"] = card.get("name")
    fields["cleanName"] = card.get("cleanName")
    fields["originalName"] = card.get("originalName")
    fields["providerCanonicalImageKey"] = card.get("providerCanonicalImageKey")
    fields["imageCacheKey"] = card.get("imageCacheKey")
    return fields


def title_number_candidates(card: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for field in ("name", "cleanName", "originalName"):
        text = normalize_text(card.get(field))
        if not text:
            continue
        for match in re.finditer(r"(?:#\s*\d+[A-Za-z]?|\b[A-Z]{1,5}[- ]?\d{1,4}[A-Za-z]?\b|\b\d{1,4}/[A-Za-z0-9-]+\b)", text):
            candidates.append({"field": field, "value": match.group(0), "context": text})
    return candidates


def safe_number_value(value: Any) -> str | None:
    text = normalize_text(value)
    if not text or text.lower() in {"unknown", "none", "null", "n/a"}:
        return None
    if len(text) > 32:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._/-]*", text):
        return None
    return re.sub(r"\s+", " ", text)


def recoverable_number(card: dict[str, Any]) -> dict[str, Any]:
    recoverable: list[dict[str, str]] = []
    for field in ("collectorNumber", "number", "cardNumber", "cardNo", "printedNumber", "localId", "setNumber"):
        value = safe_number_value(card.get(field))
        if value:
            recoverable.append({"field": field, "value": value, "confidence": "explicit_provider_number_field"})
    for path in NESTED_NUMBER_FIELDS:
        value = safe_number_value(get_nested(card, path))
        if value:
            recoverable.append({"field": ".".join(path), "value": value, "confidence": "explicit_provider_number_field"})

    unique_values = {item["value"] for item in recoverable}
    if len(unique_values) == 1:
        item = recoverable[0]
        return {
            "recoverable": True,
            "collectorNumber": item["value"],
            "collectorNumberSource": f"provider.{item['field']}",
            "confidence": item["confidence"],
            "notes": [],
        }
    if len(unique_values) > 1:
        return {
            "recoverable": False,
            "collectorNumber": None,
            "collectorNumberSource": None,
            "confidence": "ambiguous_multiple_number_fields",
            "notes": sorted(unique_values),
        }
    title_candidates = title_number_candidates(card)
    return {
        "recoverable": False,
        "collectorNumber": None,
        "collectorNumberSource": None,
        "confidence": "no_safe_number_field",
        "notes": ["title_number_candidates_are_ambiguous"] if title_candidates else [],
    }


def looks_like(record: Any) -> list[str]:
    card = record.card
    name = normalize_text(card.get("cleanName") or card.get("name"))
    set_code = normalize_text(card.get("providerSetCode") or record.file_set_code)
    set_name = normalize_text(card.get("providerSetName") or record.file_set_name)
    text = f"{name} {set_code} {set_name}".lower()
    labels: list[str] = []
    if re.search(r"\bpromo|[-/ ]p\b|pr\b|promotional", text):
        labels.append("promo")
    if re.search(r"world championship|championship|wcd\d*|worlds", text):
        labels.append("world_championship_card")
    if re.search(r"\bmisc\b|unnumbered|pkm|pkmsv|unp|miscellaneous", text):
        labels.append("misc_card")
    if re.search(r"\benergy\b|trainer|stadium|deck|gym|league|battle|theme|product|collection|box|pack", text):
        labels.append("energy_trainer_or_special_product_card")
    if "promo" in labels and not safe_number_value(card.get("cardNumber")):
        labels.append("unnumbered_promo")
    if not labels:
        labels.append("normal_set_card_with_missing_number")
    return labels


def all_provider_languages() -> list[str]:
    if not PROVIDER_ROOT.exists():
        return []
    return sorted(item.name for item in PROVIDER_ROOT.iterdir() if item.is_dir())


def app_languages() -> list[str]:
    if not APP_ROOT.exists():
        return []
    return sorted(item.name for item in APP_ROOT.iterdir() if item.is_dir())


def classify_provider_records(languages: list[str], *, include_zh: bool) -> list[dict[str, Any]]:
    enabled = set(languages)
    if include_zh:
        enabled.add("zh")
    languages_to_scan = sorted(set(all_provider_languages()) | set(app_languages()) | enabled)
    app_set_maps = {language: build_app_set_token_map(load_app_sets(language)) for language in languages_to_scan}
    existing_identity_keys, existing_position_keys, existing_pokewallet_ids = build_existing_identity_indexes(languages_to_scan)

    rows: list[dict[str, Any]] = []
    raw_candidates = []
    for record in iter_provider_records(languages_to_scan):
        card = record.card
        candidate, reason = build_candidate(record, app_set_map=app_set_maps.get(record.language, {}), enabled_languages=enabled)
        provider_id = normalize_text(card.get("providerCardId"))
        represented = bool(provider_id and provider_id in existing_pokewallet_ids)
        identity_key = None
        collector_number = None
        if candidate:
            identity_key = candidate.identity_key
            collector_number = candidate.collector_number
            raw_candidates.append(candidate)
            if identity_key in existing_identity_keys:
                represented = True
            if make_position_key(record.language, candidate.app_set_id, candidate.collector_number) in existing_position_keys:
                represented = True
        if represented:
            reason = "already_represented"

        rows.append(
            {
                "language": record.language,
                "providerCardId": provider_id or None,
                "providerSetId": card.get("providerSetId") or record.file_set_id,
                "providerSetCode": card.get("providerSetCode") or record.file_set_code,
                "providerSetName": card.get("providerSetName") or record.file_set_name,
                "providerFile": record.path.relative_to(ROOT).as_posix(),
                "name": card.get("cleanName") or card.get("name"),
                "rawNumberFields": raw_number_fields(card),
                "titleNumberCandidates": title_number_candidates(card),
                "recoverability": recoverable_number(card),
                "looksLike": looks_like(record) if reason == "missing_collector_number" else [],
                "identityKey": identity_key,
                "collectorNumber": collector_number,
                "represented": represented,
                "reason": reason,
            }
        )

    duplicate_keys, _duplicate_groups = summarize_duplicate_groups(raw_candidates)
    for row in rows:
        if row["reason"] == "promotable" and row.get("identityKey") in duplicate_keys:
            row["reason"] = "duplicate_candidate"
    return rows


def collector_acceptance_report() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for value in LETTER_PREFIX_TEST_VALUES:
        normalized = re.sub(r"\s+", " ", value.strip())
        rows.append(
            {
                "input": value,
                "accepted": bool(normalized),
                "storedCollectorNumber": normalized,
                "identityKeyValue": collector_identity_key(normalized),
            }
        )
    return rows


def build_report(languages: list[str], *, include_zh: bool, sample_limit: int = 50) -> dict[str, Any]:
    rows = classify_provider_records(languages, include_zh=include_zh)
    blocked = [row for row in rows if row["reason"] not in {"already_represented", "promotable"}]
    reason_counts: Counter[str] = Counter(str(row["reason"]) for row in blocked)
    reason_counts_by_language: dict[str, Counter[str]] = defaultdict(Counter)
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_set_counts: Counter[str] = Counter()
    look_counts: Counter[str] = Counter()
    recoverable_missing = 0

    for row in blocked:
        reason = str(row["reason"])
        language = str(row["language"])
        reason_counts_by_language[language][reason] += 1
        if len(samples[reason]) < sample_limit:
            samples[reason].append(row)
        if reason == "missing_collector_number":
            key = f"{language}|{row.get('providerSetCode') or row.get('providerSetId') or 'unknown'}|{row.get('providerSetName') or 'unknown'}"
            missing_set_counts[key] += 1
            for label in row.get("looksLike") or []:
                look_counts[str(label)] += 1
            if row.get("recoverability", {}).get("recoverable") is True:
                recoverable_missing += 1

    missing_rows = [row for row in blocked if row["reason"] == "missing_collector_number"]
    missing_rows.sort(
        key=lambda item: (
            -missing_set_counts[f"{item['language']}|{item.get('providerSetCode') or item.get('providerSetId') or 'unknown'}|{item.get('providerSetName') or 'unknown'}"],
            str(item.get("language")),
            str(item.get("providerSetCode") or ""),
            str(item.get("name") or ""),
        )
    )
    top_missing_cards = missing_rows[:100]

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": now_utc(),
        "languagesRequested": languages,
        "includeZh": include_zh,
        "blockedReasonCounts": dict(sorted(reason_counts.items())),
        "blockedReasonCountsByLanguage": {
            language: dict(sorted(counter.items())) for language, counter in sorted(reason_counts_by_language.items())
        },
        "missingCollectorNumberSummary": {
            "total": reason_counts.get("missing_collector_number", 0),
            "safeRecoverableCount": recoverable_missing,
            "remainingBlockedCount": reason_counts.get("missing_collector_number", 0) - recoverable_missing,
            "looksLikeCounts": dict(sorted(look_counts.items())),
            "top50MissingNumberSets": [
                {
                    "language": key.split("|", 2)[0],
                    "providerSetCode": key.split("|", 2)[1],
                    "providerSetName": key.split("|", 2)[2],
                    "count": count,
                }
                for key, count in missing_set_counts.most_common(50)
            ],
        },
        "letterPrefixCollectorNumberAcceptance": collector_acceptance_report(),
        "samplesByReason": dict(samples),
        "top100MissingNumberCards": top_missing_cards,
    }


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Provider Blocked Cards",
        "",
        f"- generatedAtUtc: {report['generatedAtUtc']}",
        f"- languages: {', '.join(report['languagesRequested'])}",
        f"- includeZh: {str(report['includeZh']).lower()}",
        "",
        "## Blocked Reasons",
    ]
    for reason, count in sorted(report["blockedReasonCounts"].items(), key=lambda item: (-int(item[1]), item[0])):
        lines.append(f"- {reason}: {count}")

    summary = report["missingCollectorNumberSummary"]
    lines.extend(
        [
            "",
            "## Missing Collector Numbers",
            f"- total: {summary['total']}",
            f"- safely recoverable now: {summary['safeRecoverableCount']}",
            f"- remaining blocked: {summary['remainingBlockedCount']}",
            "",
            "## Missing Number Shape",
        ]
    )
    for label, count in sorted(summary["looksLikeCounts"].items(), key=lambda item: (-int(item[1]), item[0])):
        lines.append(f"- {label}: {count}")

    lines.extend(["", "## Top Missing-Number Sets"])
    for item in summary["top50MissingNumberSets"][:20]:
        lines.append(f"- {item['language']} {item['providerSetCode']} ({item['providerSetName']}): {item['count']}")

    lines.extend(["", "## Collector Number Acceptance"])
    for item in report["letterPrefixCollectorNumberAcceptance"]:
        lines.append(
            f"- {item['input']}: {'accepted' if item['accepted'] else 'rejected'} "
            f"(stored={item['storedCollectorNumber']}, identity={item['identityKeyValue']})"
        )

    lines.extend(["", "## Missing-Number Samples"])
    for item in report["top100MissingNumberCards"][:20]:
        raw = item.get("rawNumberFields") or {}
        lines.append(
            f"- {item['language']} {item.get('providerSetCode')} {item.get('name')} "
            f"cardNumber={raw.get('cardNumber')!r} localId={raw.get('localId')!r} "
            f"recoverable={item.get('recoverability', {}).get('recoverable')}"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report provider records blocked from app catalogue promotion.")
    parser.add_argument("--languages", default="en,jp", help="Comma-separated app-supported languages to evaluate.")
    parser.add_argument("--include-zh", action="store_true", help="Treat ZH as enabled instead of unsupported.")
    parser.add_argument("--sample-limit", type=int, default=50, help="Sample records to include per block reason.")
    parser.add_argument("--no-report", action="store_true", help="Print summary only; do not write reports.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    languages = selected_languages(args.languages, include_zh=args.include_zh)
    if "zh" in languages and not args.include_zh:
        languages = [language for language in languages if language != "zh"]
    report = build_report(languages, include_zh=args.include_zh, sample_limit=args.sample_limit)
    if not args.no_report:
        write_json_if_changed(REPORT_JSON_PATH, report)
        write_text_if_changed(REPORT_MD_PATH, markdown_report(report))
    summary = {
        "generatedAtUtc": report["generatedAtUtc"],
        "blockedReasonCounts": report["blockedReasonCounts"],
        "blockedReasonCountsByLanguage": report["blockedReasonCountsByLanguage"],
        "missingCollectorNumberSummary": report["missingCollectorNumberSummary"],
        "letterPrefixCollectorNumberAcceptance": report["letterPrefixCollectorNumberAcceptance"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
