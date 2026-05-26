#!/usr/bin/env python3
"""Audit ZH provider catalogue readiness for app catalogue promotion."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ZH_PROVIDER_CARDS_DIR = ROOT / "public" / "v1" / "provider-catalog" / "pokewallet" / "cards" / "zh"
REPORT_JSON_PATH = ROOT / "reports" / "zh_catalogue_readiness_latest.json"
REPORT_MD_PATH = ROOT / "reports" / "zh_catalogue_readiness_latest.md"


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def try_load_json(path: Path) -> Any:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def non_empty(value: Any) -> bool:
    return bool(str(value or "").strip())


def normalize_identity(record: dict[str, Any]) -> str:
    direct = str(record.get("cardScanRImageCacheCandidateKey") or record.get("imageCacheKey") or "").strip().lower()
    if direct:
        return direct

    identity_basis = record.get("imageCacheIdentityBasis")
    if not isinstance(identity_basis, dict):
        identity_basis = {}

    set_code = str(record.get("providerSetCode") or identity_basis.get("setId") or "").strip().lower()
    collector = str(record.get("cardNumber") or identity_basis.get("collectorNumber") or "").strip().lower()
    normalized_name = str(identity_basis.get("normalizedName") or record.get("cleanName") or record.get("name") or "").strip().lower()
    return f"zh|{set_code}|{collector}|{normalized_name}".strip("|")


def load_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not ZH_PROVIDER_CARDS_DIR.exists():
        return records

    for path in sorted(ZH_PROVIDER_CARDS_DIR.glob("*.json"), key=lambda item: item.name.lower()):
        payload = try_load_json(path)
        if not isinstance(payload, dict):
            continue
        cards = payload.get("cards")
        if not isinstance(cards, list):
            continue
        for card in cards:
            if not isinstance(card, dict):
                continue
            row = dict(card)
            row["_sourceFile"] = path.name
            row["_setIdentity"] = str(card.get("providerSetId") or card.get("providerSetCode") or card.get("providerSetName") or path.stem)
            row["_identity"] = normalize_identity(card)
            records.append(row)
    return records


def build_report() -> dict[str, Any]:
    records = load_records()
    total = len(records)

    by_set: Counter[str] = Counter()
    identity_counts: Counter[str] = Counter()

    with_provider_card_id = 0
    with_usable_name = 0
    with_set_identity = 0
    with_collector_number = 0
    with_image_url = 0

    for row in records:
        set_identity = str(row.get("_setIdentity") or "").strip()
        if set_identity:
            by_set[set_identity] += 1

        if non_empty(row.get("providerCardId")):
            with_provider_card_id += 1
        if non_empty(row.get("name")) or non_empty(row.get("cleanName")):
            with_usable_name += 1
        if non_empty(row.get("providerSetId")) or non_empty(row.get("providerSetCode")) or non_empty(row.get("providerSetName")):
            with_set_identity += 1
        if non_empty(row.get("cardNumber")):
            with_collector_number += 1
        if non_empty(row.get("imageEndpoint")) or non_empty(row.get("imageEndpointHigh")) or non_empty(row.get("imageEndpointLow")):
            with_image_url += 1

        identity_key = str(row.get("_identity") or "").strip().lower()
        if identity_key:
            identity_counts[identity_key] += 1

    duplicate_identities = {key: count for key, count in identity_counts.items() if count > 1}
    duplicate_identity_records = sum(count for count in duplicate_identities.values())

    blocked_reason_counts: Counter[str] = Counter()
    blocked_by_set_reason: Counter[tuple[str, str]] = Counter()
    promotable_records = 0

    for row in records:
        reasons: list[str] = []
        set_identity = str(row.get("_setIdentity") or "").strip()
        identity_key = str(row.get("_identity") or "").strip().lower()

        if not non_empty(row.get("providerCardId")):
            reasons.append("missing_provider_card_id")
        if not (non_empty(row.get("name")) or non_empty(row.get("cleanName"))):
            reasons.append("missing_usable_name")
        if not (non_empty(row.get("providerSetId")) or non_empty(row.get("providerSetCode")) or non_empty(row.get("providerSetName"))):
            reasons.append("missing_set_identity")
        if not non_empty(row.get("cardNumber")):
            reasons.append("missing_collector_number")
        if not (non_empty(row.get("imageEndpoint")) or non_empty(row.get("imageEndpointHigh")) or non_empty(row.get("imageEndpointLow"))):
            reasons.append("missing_image_url")
        if identity_key and identity_counts.get(identity_key, 0) > 1:
            reasons.append("duplicate_canonical_identity")

        if reasons:
            for reason in reasons:
                blocked_reason_counts[reason] += 1
                blocked_by_set_reason[(set_identity or "unknown_set", reason)] += 1
        else:
            promotable_records += 1

    promotable_ratio = (promotable_records / total) if total else 0.0
    safe_to_promote_now = (
        total > 0
        and promotable_ratio >= 0.98
        and duplicate_identity_records == 0
        and blocked_reason_counts.get("missing_provider_card_id", 0) == 0
        and blocked_reason_counts.get("missing_usable_name", 0) == 0
        and blocked_reason_counts.get("missing_set_identity", 0) == 0
        and blocked_reason_counts.get("missing_collector_number", 0) == 0
        and blocked_reason_counts.get("missing_image_url", 0) == 0
    )

    readiness_status = "safe_to_promote" if safe_to_promote_now else "needs_normalization"
    readiness_recommendation = (
        "ZH provider records look consistent enough for controlled app-catalogue promotion. Start with a small promotion batch and validate app behavior."
        if safe_to_promote_now
        else "ZH provider records still need normalization before promotion. Resolve blocked reasons and duplicate identities first."
    )

    top_blocked_set_reasons = [
        {
            "setIdentity": set_identity,
            "reason": reason,
            "count": count,
        }
        for (set_identity, reason), count in sorted(blocked_by_set_reason.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))[:20]
    ]

    top_blocked_sets = [
        {
            "setIdentity": set_identity,
            "blockedRecords": count,
        }
        for set_identity, count in sorted(
            (
                (set_identity, sum(value for (candidate_set, _), value in blocked_by_set_reason.items() if candidate_set == set_identity))
                for set_identity in {key[0] for key in blocked_by_set_reason}
            ),
            key=lambda item: (-item[1], item[0]),
        )[:10]
    ]

    return {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "language": "zh",
        "provider": "pokewallet",
        "zhProviderCardCount": total,
        "zhSetCount": len(by_set),
        "recordsWithProviderCardId": with_provider_card_id,
        "recordsWithUsableNameOrOriginalName": with_usable_name,
        "recordsWithSetIdentity": with_set_identity,
        "recordsWithCollectorNumber": with_collector_number,
        "recordsWithImageUrl": with_image_url,
        "duplicateCanonicalIdentity": {
            "duplicateIdentityKeyCount": len(duplicate_identities),
            "duplicateIdentityRecordCount": duplicate_identity_records,
            "topDuplicateIdentityExamples": [
                {"identity": key, "count": count}
                for key, count in sorted(duplicate_identities.items(), key=lambda item: (-item[1], item[0]))[:20]
            ],
        },
        "estimatedPromotableZhRecords": promotable_records,
        "estimatedPromotableRatio": promotable_ratio,
        "blockedReasonCounts": dict(sorted(blocked_reason_counts.items(), key=lambda item: (-item[1], item[0]))),
        "topBlockedSets": top_blocked_sets,
        "topBlockedSetReasons": top_blocked_set_reasons,
        "readiness": {
            "status": readiness_status,
            "safeToPromoteNow": safe_to_promote_now,
            "recommendation": readiness_recommendation,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append

    a("# ZH Catalogue Readiness Audit")
    a("")
    a(f"Generated: {report.get('generatedAtUtc', 'n/a')}")
    a("")

    a(f"- ZH provider card count: {int(report.get('zhProviderCardCount', 0)):,}")
    a(f"- ZH set count: {int(report.get('zhSetCount', 0)):,}")
    a(f"- Records with provider card ID: {int(report.get('recordsWithProviderCardId', 0)):,}")
    a(f"- Records with usable name/original name: {int(report.get('recordsWithUsableNameOrOriginalName', 0)):,}")
    a(f"- Records with set identity: {int(report.get('recordsWithSetIdentity', 0)):,}")
    a(f"- Records with collector number: {int(report.get('recordsWithCollectorNumber', 0)):,}")
    a(f"- Records with image URL: {int(report.get('recordsWithImageUrl', 0)):,}")

    duplicates = report.get("duplicateCanonicalIdentity", {}) if isinstance(report.get("duplicateCanonicalIdentity"), dict) else {}
    a(f"- Duplicate canonical identity keys: {int(duplicates.get('duplicateIdentityKeyCount', 0)):,}")
    a(f"- Duplicate canonical identity records: {int(duplicates.get('duplicateIdentityRecordCount', 0)):,}")
    a(f"- Estimated promotable ZH records: {int(report.get('estimatedPromotableZhRecords', 0)):,}")
    a(f"- Estimated promotable ratio: {float(report.get('estimatedPromotableRatio', 0.0)) * 100:.2f}%")

    readiness = report.get("readiness", {}) if isinstance(report.get("readiness"), dict) else {}
    a(f"- Safe to promote now: {'yes' if readiness.get('safeToPromoteNow') else 'no'}")
    a(f"- Readiness status: {readiness.get('status', 'unknown')}")
    a(f"- Recommendation: {readiness.get('recommendation', '')}")
    a("")

    blocked = report.get("blockedReasonCounts", {}) if isinstance(report.get("blockedReasonCounts"), dict) else {}
    if blocked:
        a("## Blocked Reasons")
        a("")
        for reason, count in blocked.items():
            a(f"- {reason}: {int(count):,}")
        a("")

    top_set_reasons = report.get("topBlockedSetReasons", []) if isinstance(report.get("topBlockedSetReasons"), list) else []
    if top_set_reasons:
        a("## Top Blocked Sets/Reasons")
        a("")
        a("| Set | Reason | Count |")
        a("|-----|--------|------:|")
        for row in top_set_reasons:
            if not isinstance(row, dict):
                continue
            a(f"| {row.get('setIdentity', '')} | {row.get('reason', '')} | {int(row.get('count', 0)):,} |")
        a("")

    a("---")
    a("Generated by tools/report_zh_catalogue_readiness.py")
    return "\n".join(lines)


def main() -> int:
    report = build_report()
    markdown = render_markdown(report)
    write_json(REPORT_JSON_PATH, report)
    write_text(REPORT_MD_PATH, markdown)

    readiness = report.get("readiness", {}) if isinstance(report.get("readiness"), dict) else {}
    print("ZH catalogue readiness audit")
    print(f"  zh provider card count: {int(report.get('zhProviderCardCount', 0)):,}")
    print(f"  zh set count: {int(report.get('zhSetCount', 0)):,}")
    print(f"  promotable records: {int(report.get('estimatedPromotableZhRecords', 0)):,}")
    print(f"  promotable ratio: {float(report.get('estimatedPromotableRatio', 0.0)) * 100:.2f}%")
    print(f"  readiness status: {readiness.get('status', 'unknown')}")
    print(f"  safe to promote now: {'yes' if readiness.get('safeToPromoteNow') else 'no'}")
    print(f"  wrote: {REPORT_JSON_PATH.relative_to(ROOT)}")
    print(f"  wrote: {REPORT_MD_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
