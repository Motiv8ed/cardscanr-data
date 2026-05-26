#!/usr/bin/env python3
"""Detailed audit of duplicate ZH canonical identities for safe promotion planning."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from promote_provider_catalog_to_app_catalog import (
    ROOT,
    build_app_set_token_map,
    build_candidate,
    iter_provider_records,
    load_app_sets,
    normalize_catalog_name,
    variant_identity,
)


REPORT_JSON_PATH = ROOT / "reports" / "zh_duplicate_identities_latest.json"
REPORT_MD_PATH = ROOT / "reports" / "zh_duplicate_identities_latest.md"

SIMPLIFIED_TOKENS = {"chs", "zhs", "zh-hans", "cn", "sc", "chs-cn"}
TRADITIONAL_TOKENS = {"cht", "zht", "zh-hant", "tw", "hk", "tc", "cht-tw"}


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def normalize_text_token(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def safe_token(value: Any) -> str:
    text = normalize_text_token(value)
    return re.sub(r"[^a-z0-9]+", "", text)


def printable_row_signature(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        normalize_text_token(item.get("providerSetId")),
        normalize_text_token(item.get("providerSetCode")),
        normalize_text_token(item.get("providerSetName")),
        normalize_text_token(item.get("cardName")),
        normalize_text_token(item.get("originalName")),
        normalize_text_token(item.get("collectorNumber")),
        normalize_text_token(item.get("variantKey")),
        normalize_text_token(item.get("rarity")),
        normalize_text_token(item.get("providerLanguage")),
        normalize_text_token(item.get("printMetadataVariant")),
    )


def simplified_traditional_collision(rows: list[dict[str, Any]]) -> bool:
    langs = {safe_token(row.get("providerLanguage")) for row in rows if row.get("providerLanguage")}
    has_simplified = any(token in SIMPLIFIED_TOKENS for token in langs)
    has_traditional = any(token in TRADITIONAL_TOKENS for token in langs)
    return has_simplified and has_traditional


def set_variant_collision(rows: list[dict[str, Any]]) -> bool:
    set_codes = {normalize_text_token(row.get("providerSetCode")) for row in rows if row.get("providerSetCode")}
    set_ids = {normalize_text_token(row.get("providerSetId")) for row in rows if row.get("providerSetId")}
    set_names = {normalize_text_token(row.get("providerSetName")) for row in rows if row.get("providerSetName")}
    return len(set_codes) > 1 or len(set_ids) > 1 or len(set_names) > 1


def missing_set_identity_normalization(rows: list[dict[str, Any]]) -> bool:
    has_missing = any(
        not normalize_text_token(row.get("providerSetCode")) and not normalize_text_token(row.get("providerSetId"))
        for row in rows
    )
    if has_missing:
        return True

    raw_set_tokens = {normalize_text_token(row.get("providerSetCode") or row.get("providerSetName") or "") for row in rows}
    normalized_set_tokens = {safe_token(value) for value in raw_set_tokens if value}
    return len(raw_set_tokens) > 1 and len(normalized_set_tokens) == 1


def variant_or_rarity_missing_from_identity(rows: list[dict[str, Any]]) -> bool:
    variants = {normalize_text_token(row.get("variantKey")) for row in rows if normalize_text_token(row.get("variantKey"))}
    rarities = {normalize_text_token(row.get("rarity")) for row in rows if normalize_text_token(row.get("rarity"))}
    print_variants = {
        normalize_text_token(row.get("printMetadataVariant")) for row in rows if normalize_text_token(row.get("printMetadataVariant"))
    }
    return len(variants) > 1 or len(rarities) > 1 or len(print_variants) > 1


def classify_root_cause(rows: list[dict[str, Any]], *, distinguishable: bool) -> str:
    provider_ids = [str(row.get("providerCardId") or "").strip() for row in rows]
    has_repeated_provider_id = len(provider_ids) != len(set(provider_ids))

    if has_repeated_provider_id:
        return "actual_duplicate_card_records"
    if simplified_traditional_collision(rows):
        return "simplified_traditional_chinese_collision"
    if set_variant_collision(rows):
        return "same_collector_across_regional_set_variants"
    if missing_set_identity_normalization(rows):
        return "missing_set_identity_normalization"
    if variant_or_rarity_missing_from_identity(rows):
        return "variant_or_rarity_not_included_in_identity"
    if not distinguishable:
        return "provider_duplicate_rows"
    return "actual_duplicate_card_records"


def recommended_strategy(root_cause: str) -> str:
    strategies = {
        "simplified_traditional_chinese_collision": "Use ZH-only disambiguation key with provider script/locale marker and providerCardId suffix.",
        "same_collector_across_regional_set_variants": "Include provider set identity in ZH canonical collision suffix and keep display names unchanged.",
        "missing_set_identity_normalization": "Normalize provider set code/name and fallback to providerSetId when set code is ambiguous.",
        "variant_or_rarity_not_included_in_identity": "Add variant/rarity fingerprint for ZH collision cases before providerCardId suffix.",
        "provider_duplicate_rows": "Treat as provider duplicates; allow ZH-only providerCardId suffix to keep records distinct and auditable.",
        "actual_duplicate_card_records": "De-duplicate by providerCardId and image hash first, then retain one canonical ZH record per unique provider card.",
    }
    return strategies.get(root_cause, "Investigate manually before promotion.")


def collect_zh_duplicate_analysis() -> dict[str, Any]:
    app_set_map = build_app_set_token_map(load_app_sets("zh"))
    enabled_languages = {"zh"}

    duplicate_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    candidate_count = 0
    blocked_non_duplicate_reason_counts: Counter[str] = Counter()

    for record in iter_provider_records(["zh"]):
        candidate, reason = build_candidate(record, app_set_map=app_set_map, enabled_languages=enabled_languages)
        if not candidate:
            blocked_non_duplicate_reason_counts[str(reason)] += 1
            continue
        candidate_count += 1

        card = record.card
        identity_basis = card.get("imageCacheIdentityBasis") if isinstance(card.get("imageCacheIdentityBasis"), dict) else {}
        duplicate_candidates[candidate.identity_key].append(
            {
                "identityKey": candidate.identity_key,
                "providerCardId": str(card.get("providerCardId") or "").strip(),
                "providerSetId": str(card.get("providerSetId") or record.file_set_id or "").strip(),
                "providerSetCode": str(card.get("providerSetCode") or record.file_set_code or "").strip(),
                "providerSetName": str(card.get("providerSetName") or record.file_set_name or "").strip(),
                "cardName": str(candidate.display_name or "").strip(),
                "originalName": str(card.get("name") or "").strip(),
                "collectorNumber": str(candidate.collector_number or "").strip(),
                "imageUrl": str(candidate.image_small or "").strip(),
                "imageUrlHigh": str(candidate.image_large or "").strip(),
                "variantKey": str(candidate.variant_key or "normal").strip(),
                "rarity": str(card.get("rarity") or "").strip(),
                "providerLanguage": str(card.get("providerLanguage") or "").strip(),
                "printMetadata": {
                    "rawKeys": card.get("rawKeys") if isinstance(card.get("rawKeys"), list) else [],
                    "hasCardmarketFields": bool(card.get("hasCardmarketFields")),
                    "hasPriceFields": bool(card.get("hasPriceFields")),
                    "hasTcgplayerFields": bool(card.get("hasTcgplayerFields")),
                },
                "printMetadataVariant": str(identity_basis.get("variant") or variant_identity(card.get("variants")) or "normal"),
                "normalizedName": normalize_catalog_name(candidate.display_name),
                "canonicalBaseIdCandidate": candidate.canonical_base_id,
                "providerFile": record.path.relative_to(ROOT).as_posix(),
            }
        )

    duplicate_groups: list[dict[str, Any]] = []
    root_cause_counts: Counter[str] = Counter()

    for identity_key, rows in sorted(duplicate_candidates.items(), key=lambda item: item[0]):
        if len(rows) <= 1:
            continue

        signatures = {printable_row_signature(row) for row in rows}
        distinguishable = len(signatures) > 1
        root_cause = classify_root_cause(rows, distinguishable=distinguishable)
        root_cause_counts[root_cause] += 1

        duplicate_groups.append(
            {
                "canonicalIdentityKey": identity_key,
                "groupSize": len(rows),
                "providerCardIds": sorted(row["providerCardId"] for row in rows),
                "providerSetIdentities": [
                    {
                        "providerSetId": row.get("providerSetId"),
                        "providerSetCode": row.get("providerSetCode"),
                        "providerSetName": row.get("providerSetName"),
                    }
                    for row in rows
                ],
                "rows": rows,
                "recordsAppearIdentical": not distinguishable,
                "recordsAreDistinguishable": distinguishable,
                "suspectedRootCause": root_cause,
                "recommendedDisambiguationStrategy": recommended_strategy(root_cause),
            }
        )

    duplicate_groups.sort(key=lambda item: (-int(item["groupSize"]), str(item["canonicalIdentityKey"])))
    duplicate_record_count = sum(int(item["groupSize"]) for item in duplicate_groups)

    return {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": now_utc(),
        "language": "zh",
        "provider": "pokewallet",
        "candidateRecordCount": candidate_count,
        "duplicateGroupCount": len(duplicate_groups),
        "duplicateRecordCount": duplicate_record_count,
        "blockedNonDuplicateReasonCounts": dict(sorted(blocked_non_duplicate_reason_counts.items())),
        "duplicateRootCauseCounts": dict(sorted(root_cause_counts.items(), key=lambda item: (-item[1], item[0]))),
        "duplicateGroups": duplicate_groups,
    }


def build_report() -> dict[str, Any]:
    return collect_zh_duplicate_analysis()


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append

    a("# ZH Duplicate Identity Audit")
    a("")
    a(f"Generated: {report.get('generatedAtUtc')}")
    a("")
    a(f"- candidate ZH records: {int(report.get('candidateRecordCount', 0)):,}")
    a(f"- duplicate groups: {int(report.get('duplicateGroupCount', 0)):,}")
    a(f"- duplicate records: {int(report.get('duplicateRecordCount', 0)):,}")
    blocked_counts = report.get("blockedNonDuplicateReasonCounts", {}) if isinstance(report.get("blockedNonDuplicateReasonCounts"), dict) else {}
    a(f"- non-duplicate blocked reasons: {blocked_counts}")
    a("")

    causes = report.get("duplicateRootCauseCounts", {}) if isinstance(report.get("duplicateRootCauseCounts"), dict) else {}
    if causes:
        a("## Root Causes")
        a("")
        for cause, count in causes.items():
            a(f"- {cause}: {int(count):,} groups")
        a("")

    groups = report.get("duplicateGroups", []) if isinstance(report.get("duplicateGroups"), list) else []
    if groups:
        a("## Top Duplicate Groups")
        a("")
        a("| Identity Key | Size | Distinguishable | Root Cause |")
        a("|---|---:|---|---|")
        for group in groups[:30]:
            if not isinstance(group, dict):
                continue
            a(
                "| "
                + f"{group.get('canonicalIdentityKey', '')} | {int(group.get('groupSize', 0)):,} | "
                + f"{'yes' if group.get('recordsAreDistinguishable') else 'no'} | {group.get('suspectedRootCause', '')} |"
            )
        a("")

    a("---")
    a("Generated by tools/report_zh_duplicate_identities.py")
    return "\n".join(lines)


def main() -> int:
    report = build_report()
    markdown = render_markdown(report)
    write_json(REPORT_JSON_PATH, report)
    write_text(REPORT_MD_PATH, markdown)

    print("ZH duplicate identity audit")
    print(f"  candidate records: {int(report.get('candidateRecordCount', 0)):,}")
    print(f"  duplicate groups: {int(report.get('duplicateGroupCount', 0)):,}")
    print(f"  duplicate records: {int(report.get('duplicateRecordCount', 0)):,}")
    print(f"  wrote: {REPORT_JSON_PATH.relative_to(ROOT)}")
    print(f"  wrote: {REPORT_MD_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
