#!/usr/bin/env python3
"""Dry-run ZH promotion plan using a ZH-only duplicate disambiguation proposal."""

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
)
from report_zh_duplicate_identities import collect_zh_duplicate_analysis


REPORT_JSON_PATH = ROOT / "reports" / "zh_promotion_plan_latest.json"
REPORT_MD_PATH = ROOT / "reports" / "zh_promotion_plan_latest.md"


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


def short_token(value: Any, *, fallback: str) -> str:
    raw = str(value or "").strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "", raw)
    if not raw:
        raw = fallback
    return raw[:10]


def proposed_zh_canonical_id(base_id: str, provider_set_identity: str, provider_card_id: str, duplicate_group_size: int) -> str:
    if duplicate_group_size <= 1:
        return base_id
    set_token = short_token(provider_set_identity, fallback="set")
    card_token = short_token(provider_card_id, fallback="card")
    return f"{base_id}~z{set_token}~p{card_token}"


def build_plan() -> dict[str, Any]:
    duplicate_audit = collect_zh_duplicate_analysis()

    app_set_map = build_app_set_token_map(load_app_sets("zh"))
    enabled_languages = {"zh"}

    valid_candidates: list[dict[str, Any]] = []
    non_duplicate_blockers: Counter[str] = Counter()

    for record in iter_provider_records(["zh"]):
        candidate, reason = build_candidate(record, app_set_map=app_set_map, enabled_languages=enabled_languages)
        if not candidate:
            non_duplicate_blockers[str(reason)] += 1
            continue

        card = record.card
        valid_candidates.append(
            {
                "identityKey": candidate.identity_key,
                "canonicalBaseId": candidate.canonical_base_id,
                "providerCardId": str(card.get("providerCardId") or "").strip(),
                "providerSetIdentity": str(card.get("providerSetId") or card.get("providerSetCode") or card.get("providerSetName") or candidate.app_set_id),
                "language": candidate.provider.language,
                "setId": candidate.app_set_id,
                "collectorNumber": candidate.collector_number,
                "normalizedName": candidate.normalized_name,
            }
        )

    by_identity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid_candidates:
        by_identity[row["identityKey"]].append(row)

    duplicate_groups = {key: rows for key, rows in by_identity.items() if len(rows) > 1}
    current_blocked_count = sum(len(rows) for rows in duplicate_groups.values())
    current_promotable_count = sum(1 for rows in by_identity.values() if len(rows) == 1)

    proposed_id_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    examples: list[dict[str, Any]] = []

    for identity_key, rows in sorted(by_identity.items(), key=lambda item: item[0]):
        group_size = len(rows)
        for row in rows:
            proposed_id = proposed_zh_canonical_id(
                row["canonicalBaseId"],
                row["providerSetIdentity"],
                row["providerCardId"],
                group_size,
            )
            proposed = {
                **row,
                "proposedCanonicalBaseId": proposed_id,
            }
            proposed_id_groups[proposed_id].append(proposed)
            if len(examples) < 40:
                examples.append(
                    {
                        "identityKey": identity_key,
                        "currentCanonicalBaseId": row["canonicalBaseId"],
                        "proposedCanonicalBaseId": proposed_id,
                        "providerCardId": row["providerCardId"],
                        "providerSetIdentity": row["providerSetIdentity"],
                    }
                )

    unresolved_after_proposal = {
        key: rows for key, rows in proposed_id_groups.items() if len(rows) > 1
    }
    unresolved_count = sum(len(rows) for rows in unresolved_after_proposal.values())

    resolved_duplicate_count = max(0, current_blocked_count - unresolved_count)
    remaining_blockers = unresolved_count + sum(non_duplicate_blockers.values())
    final_promotable_count = len(valid_candidates) - unresolved_count
    safe_to_promote_after_fix = remaining_blockers == 0 and final_promotable_count > 0

    return {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": now_utc(),
        "language": "zh",
        "provider": "pokewallet",
        "proposal": {
            "name": "zh_duplicate_disambiguation_v1",
            "description": "Keep existing canonicalBaseId for unique ZH records. For duplicate identity groups only, append a stable ZH-only suffix derived from provider set identity and providerCardId.",
            "rule": "if duplicate_group_size > 1: canonicalBaseId = canonicalBaseId + '~z' + short(providerSetIdentity) + '~p' + short(providerCardId)",
            "enJpUnchanged": True,
            "userFacingDisplayUnchanged": True,
        },
        "current": {
            "candidateRecordCount": len(valid_candidates),
            "currentPromotableCount": current_promotable_count,
            "currentBlockedCount": current_blocked_count,
            "nonDuplicateBlockers": dict(sorted(non_duplicate_blockers.items())),
            "duplicateGroupCount": len(duplicate_groups),
        },
        "afterProposedFix": {
            "resolvedDuplicateCount": resolved_duplicate_count,
            "remainingDuplicateBlockers": unresolved_count,
            "finalPromotableCount": final_promotable_count,
            "remainingBlockers": remaining_blockers,
            "safeToPromoteAfterFix": safe_to_promote_after_fix,
        },
        "remainingDuplicateGroupsAfterProposal": [
            {
                "proposedCanonicalBaseId": key,
                "count": len(rows),
                "providerCardIds": sorted(item.get("providerCardId") for item in rows),
            }
            for key, rows in sorted(unresolved_after_proposal.items(), key=lambda item: (-len(item[1]), item[0]))[:25]
        ],
        "exampleGeneratedCanonicalIds": examples,
        "sourceDuplicateAuditSummary": {
            "duplicateGroupCount": int(duplicate_audit.get("duplicateGroupCount", 0)),
            "duplicateRecordCount": int(duplicate_audit.get("duplicateRecordCount", 0)),
            "duplicateRootCauseCounts": duplicate_audit.get("duplicateRootCauseCounts", {}),
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append

    current = report.get("current", {}) if isinstance(report.get("current"), dict) else {}
    after = report.get("afterProposedFix", {}) if isinstance(report.get("afterProposedFix"), dict) else {}
    proposal = report.get("proposal", {}) if isinstance(report.get("proposal"), dict) else {}

    a("# ZH Promotion Plan (Dry Run)")
    a("")
    a(f"Generated: {report.get('generatedAtUtc')}")
    a("")
    a("## Proposed ZH Identity Rule")
    a("")
    a(f"- name: {proposal.get('name')}")
    a(f"- description: {proposal.get('description')}")
    a(f"- rule: {proposal.get('rule')}")
    a(f"- EN/JP unchanged: {'yes' if proposal.get('enJpUnchanged') else 'no'}")
    a(f"- user-facing display unchanged: {'yes' if proposal.get('userFacingDisplayUnchanged') else 'no'}")
    a("")

    a("## Counts")
    a("")
    a(f"- current blocked count: {int(current.get('currentBlockedCount', 0)):,}")
    a(f"- resolved duplicate count under proposal: {int(after.get('resolvedDuplicateCount', 0)):,}")
    a(f"- final promotable count: {int(after.get('finalPromotableCount', 0)):,}")
    a(f"- remaining blockers: {int(after.get('remainingBlockers', 0)):,}")
    a(f"- safeToPromoteAfterFix: {'yes' if after.get('safeToPromoteAfterFix') else 'no'}")
    a("")

    examples = report.get("exampleGeneratedCanonicalIds", []) if isinstance(report.get("exampleGeneratedCanonicalIds"), list) else []
    if examples:
        a("## Example Generated Canonical IDs")
        a("")
        a("| Current | Proposed | Provider Card ID |")
        a("|---|---|---|")
        for row in examples[:25]:
            if not isinstance(row, dict):
                continue
            a(
                "| "
                + f"{row.get('currentCanonicalBaseId', '')} | {row.get('proposedCanonicalBaseId', '')} | {row.get('providerCardId', '')} |"
            )
        a("")

    a("---")
    a("Generated by tools/report_zh_promotion_plan.py")
    return "\n".join(lines)


def main() -> int:
    report = build_plan()
    markdown = render_markdown(report)
    write_json(REPORT_JSON_PATH, report)
    write_text(REPORT_MD_PATH, markdown)

    after = report.get("afterProposedFix", {}) if isinstance(report.get("afterProposedFix"), dict) else {}
    print("ZH promotion plan (dry run)")
    print(f"  current blocked count: {int((report.get('current') or {}).get('currentBlockedCount', 0)):,}")
    print(f"  resolved duplicate count: {int(after.get('resolvedDuplicateCount', 0)):,}")
    print(f"  final promotable count: {int(after.get('finalPromotableCount', 0)):,}")
    print(f"  remaining blockers: {int(after.get('remainingBlockers', 0)):,}")
    print(f"  safeToPromoteAfterFix: {'yes' if after.get('safeToPromoteAfterFix') else 'no'}")
    print(f"  wrote: {REPORT_JSON_PATH.relative_to(ROOT)}")
    print(f"  wrote: {REPORT_MD_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
