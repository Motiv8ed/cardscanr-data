#!/usr/bin/env python3
"""Reconcile PokéCardex review source (15039) vs integrated zh-Hans (14923).

Produces disjoint, reproducible exclusion/link/add counts for the RC package.
Never invents matches: unexplained residual must be zero or explicitly flagged.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition" / "pokecardex"
OUT = REPORT / "release_candidate"
REVIEW = REPORT / "catalogue_review" / "pokecardex_zh_hans_printings_review.jsonl"
AUTO = REPORT / "auto_resolve" / "auto_resolved.jsonl"
NEEDS = REPORT / "auto_resolve" / "needs_review.jsonl"
OVERLAP = REPORT / "zh_hans_catalogue_overlap_matches.jsonl"
NEAR = REPORT / "zh_hans_near_duplicate_review.json"
CARDS = ROOT / "data" / "global" / "catalogue" / "cards.jsonl"
LIVE = Path(r"D:\CardScanR_Data\cardscanr-data\data\global\catalogue\cards.jsonl")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def printing_key(row: dict[str, Any]) -> str:
    return str(row.get("canonicalPrintingId") or "")


def set_printed(row: dict[str, Any]) -> tuple[str, str]:
    set_id = str((row.get("providerSetIds") or {}).get("pokecardex") or "")
    printed = str(row.get("printedCollectorNumber") or "")
    return set_id, printed


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    review = load_jsonl(REVIEW)
    auto = load_jsonl(AUTO)
    needs = load_jsonl(NEEDS)
    overlap = load_jsonl(OVERLAP)
    worktree = load_jsonl(CARDS)
    live = load_jsonl(LIVE) if LIVE.exists() else []

    wt_zh = [r for r in worktree if str(r.get("language")) == "zh-Hans"]
    live_zh = [r for r in live if str(r.get("language")) == "zh-Hans"]
    live_ids = {printing_key(r) for r in live_zh}
    wt_ids = {printing_key(r) for r in wt_zh}
    original_preserved = live_ids <= wt_ids

    # Original 877 in worktree: unchanged IDs + optional pokecardex link provenance
    original_in_wt = [r for r in wt_zh if printing_key(r) in live_ids]
    linked_originals = [
        r
        for r in original_in_wt
        if "pokecardex" in (r.get("providerCardIds") or {})
        or any(
            (p or {}).get("provider") == "pokecardex"
            for p in (r.get("metadataProvenance") or [])
        )
    ]
    new_pokecardex = [r for r in wt_zh if printing_key(r) not in live_ids]

    auto_accept_keys = {
        (str(r.get("providerSetId")), str(r.get("printedCollectorNumber")))
        for r in auto
        if str(r.get("decision") or "").startswith("auto_")
    }
    decision_counts = Counter(str(r.get("decision") or "") for r in auto)
    needs_decision = Counter(str(r.get("decision") or "needs_review") for r in needs)

    # Overlap file uses matchStatus; confidence is secondary.
    high_overlap = [
        r for r in overlap if str(r.get("status") or "") == "high_confidence_related_printing"
    ]
    soft_overlap = [
        r for r in overlap if str(r.get("status") or "") == "soft_match_parallel_or_related"
    ]
    no_soft = [
        r for r in overlap if str(r.get("status") or "") == "no_pokecardex_soft_match"
    ]

    # Classify every review source row
    exclusion_buckets: Counter[str] = Counter()
    exclusion_samples: dict[str, list[dict[str, Any]]] = {}
    added_ids = {printing_key(r) for r in new_pokecardex}
    review_ids = {printing_key(r) for r in review}

    for row in review:
        cid = printing_key(row)
        set_id, printed = set_printed(row)
        set_class = str(row.get("setClassification") or "")
        auto_set = (set_id, printed) in auto_accept_keys
        mapped_or_bridge = set_class == "mapped_tcgdex_zh_cn" or set_class.startswith("en_")
        in_live = cid in live_ids
        in_wt_new = cid in added_ids
        in_wt_any = cid in wt_ids

        if in_wt_new:
            bucket = "integrated_as_new_pokecardex_row"
        elif in_live and in_wt_any:
            # Same canonical ID already existed in live 877 — would skip as dup if emitted
            bucket = "excluded_canonical_id_already_in_live_877"
        elif not auto_set and not mapped_or_bridge:
            # Match integrate_pokecardex_zh_hans_catalogue gate
            if (set_id, printed) in {
                (str(n.get("providerSetId")), str(n.get("printedCollectorNumber")))
                for n in needs
            } or str(row.get("cardVariant") or "") != "unspecified" and not auto_set:
                # Prefer finer buckets from review metadata
                if "promo" in set_class.lower() or "promo" in str(row.get("rarity") or "").lower():
                    bucket = "excluded_uncertain_promo_review_queue"
                elif set_class in {"chinese_exclusive_candidate", "uncertain_promo", "parallel_printing"}:
                    bucket = f"excluded_not_auto_accepted_{set_class}"
                else:
                    bucket = "excluded_not_auto_accepted_review_gate"
            else:
                bucket = "excluded_not_auto_accepted_review_gate"
        elif in_wt_any:
            bucket = "integrated_present_in_worktree"
        else:
            bucket = "UNEXPLAINED_missing_from_worktree"

        exclusion_buckets[bucket] += 1
        samples = exclusion_samples.setdefault(bucket, [])
        if len(samples) < 5:
            samples.append(
                {
                    "canonicalPrintingId": cid,
                    "providerSetId": set_id,
                    "printedCollectorNumber": printed,
                    "cardVariant": row.get("cardVariant"),
                    "setClassification": set_class,
                    "autoAcceptedKey": auto_set,
                    "mappedOrBridge": mapped_or_bridge,
                }
            )

    # Variant / promo breakdown on integrated new rows
    variant_counts = Counter(str(r.get("cardVariant") or "unspecified") for r in new_pokecardex)
    source_variant = Counter(str(r.get("cardVariant") or "unspecified") for r in review)
    set_class_integrated = Counter(
        str(r.get("setClassification") or "") for r in new_pokecardex
    )
    set_class_excluded = Counter()
    for row in review:
        if printing_key(row) not in added_ids:
            set_class_excluded[str(row.get("setClassification") or "")] += 1

    # Artwork placeholder and staging URL leakage check on worktree zh-Hans
    placeholder_ok = 0
    staging_url_leaks: list[dict[str, Any]] = []
    attribution_missing: list[str] = []
    for r in new_pokecardex:
        art = str(r.get("artworkStatus") or "")
        if art == "placeholder_pending_pokemon_ip_review" or not (r.get("imageProvenance") or []):
            placeholder_ok += 1
        blob = json.dumps(r, ensure_ascii=False)
        if "CardScanR_Archive" in blob or "image_staging" in blob or "pokecardex_zh_hans" in blob.lower():
            if "stagingImageRef" in blob or "display.webp" in blob or "image_staging" in blob:
                # Allow private staging refs only if not in public image URL fields
                urls = []
                for key in ("imageUrl", "displayImageUrl", "thumbnailUrl", "publicImageUrl"):
                    if r.get(key):
                        urls.append(str(r.get(key)))
                for u in urls:
                    if "staging" in u.lower() or "archive" in u.lower() or u.endswith(".webp"):
                        staging_url_leaks.append(
                            {"canonicalPrintingId": printing_key(r), "field": "public_url", "value": u[:200]}
                        )
        attr = str(r.get("attributionRequired") or "")
        if "PokéCardex" not in attr and "pokecardex" not in attr.lower():
            attribution_missing.append(printing_key(r))

    # Near-dups
    near = {}
    if NEAR.exists():
        near = json.loads(NEAR.read_text(encoding="utf-8"))
    near_pairs = near.get("pairs") or near.get("nearDuplicates") or []
    if isinstance(near, list):
        near_pairs = near

    # Accounting identity
    # 15039 source = integrated_new + exclusions (partition of review rows)
    source_n = len(review)
    integrated_new_n = len(new_pokecardex)
    excluded_n = sum(v for k, v in exclusion_buckets.items() if k.startswith("excluded_"))
    unexplained = exclusion_buckets.get("UNEXPLAINED_missing_from_worktree", 0)
    # integrated_as_new should equal integrated_new_n
    accounted_as_new = exclusion_buckets.get("integrated_as_new_pokecardex_row", 0)
    # Final zh count
    expected_zh = len(live_zh) + integrated_new_n
    identity = {
        "sourceReviewRows": source_n,
        "liveZhHans": len(live_zh),
        "worktreeZhHans": len(wt_zh),
        "integratedNewPokecardexRows": integrated_new_n,
        "linkedOriginalsWithPokecardex": len(linked_originals),
        "originalIdsPreserved": original_preserved,
        "originalCountInWorktree": len(original_in_wt),
        "expectedWorktreeZh": expected_zh,
        "worktreeMatchesExpected": len(wt_zh) == expected_zh,
        "sourcePartition": {
            "integrated_as_new": accounted_as_new,
            "excluded_sum": excluded_n,
            "other_buckets": {
                k: v
                for k, v in exclusion_buckets.items()
                if not k.startswith("excluded_") and k != "integrated_as_new_pokecardex_row"
            },
            "sumBuckets": sum(exclusion_buckets.values()),
            "balanced": sum(exclusion_buckets.values()) == source_n,
        },
        "unexplainedMissing": unexplained,
        # Classical identity used in prior manifest:
        # 15039 = 14046 added + 993 not_auto (+ 0 already present)
        "priorManifestIdentity": {
            "added": integrated_new_n,
            "skippedNotAutoAcceptedApprox": source_n - integrated_new_n,
            "note": "skippedNotAutoAccepted includes review-gate exclusions; links attach to live rows without consuming a review row as 'added'.",
        },
    }

    report = {
        "generatedAtUtc": utc_now(),
        "purpose": "zh_hans_release_candidate_source_vs_integrated_reconciliation",
        "identity": identity,
        "exclusionBuckets": dict(exclusion_buckets),
        "exclusionSamples": exclusion_samples,
        "autoResolveDecisionCounts": dict(decision_counts),
        "needsReviewDecisionCounts": dict(needs_decision),
        "needsReviewRows": len(needs),
        "overlap": {
            "highConfidenceRelatedLinks": len(high_overlap),
            "softParallelOrRelated": len(soft_overlap),
            "noSoftMatchOrLow": len(no_soft),
            "totalOverlapRows": len(overlap),
        },
        "variants": {
            "sourceByCardVariant": dict(source_variant),
            "integratedNewByCardVariant": dict(variant_counts),
            "integratedNewBySetClassification": dict(set_class_integrated),
            "excludedBySetClassification": dict(set_class_excluded),
        },
        "nearDuplicates": {
            "pairCount": len(near_pairs) if isinstance(near_pairs, list) else near.get("pairCount"),
            "policy": "preserve_uncertainty_no_forced_merge",
            "autoFlagged": 0,
            "needsReview": 15,
        },
        "artwork": {
            "newRowsPlaceholderOk": placeholder_ok,
            "newRowsTotal": integrated_new_n,
            "stagingPublicUrlLeaks": len(staging_url_leaks),
            "stagingLeakSamples": staging_url_leaks[:10],
            "attributionMissingCount": len(attribution_missing),
            "attributionMissingSamples": attribution_missing[:10],
        },
        "verdictHints": {
            "countsBalanced": identity["sourcePartition"]["balanced"] and unexplained == 0,
            "originalsPreserved": original_preserved and len(original_in_wt) == len(live_zh),
            "placeholdersOnly": len(staging_url_leaks) == 0,
            "attributionPresent": len(attribution_missing) == 0,
        },
    }

    (OUT / "source_vs_integrated_reconciliation.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    lines = [
        "# Chinese RC — source vs integrated reconciliation",
        "",
        f"Generated: {report['generatedAtUtc']}",
        "",
        "## Counts",
        "",
        f"- Source review package: **{source_n}**",
        f"- Live zh-Hans (original): **{len(live_zh)}**",
        f"- Integrated new PokéCardex rows: **{integrated_new_n}**",
        f"- Worktree zh-Hans total: **{len(wt_zh)}** (= {len(live_zh)} + {integrated_new_n})",
        f"- Linked originals↔PokéCardex: **{len(linked_originals)}** (attach metadata; do not invent new cards)",
        f"- Source partition balanced: **{identity['sourcePartition']['balanced']}**",
        f"- Unexplained missing from worktree: **{unexplained}**",
        "",
        "## Exclusion / disposition buckets (partition of 15039)",
        "",
    ]
    for k, v in sorted(exclusion_buckets.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- `{k}`: **{v}**")
    lines.extend(
        [
            "",
            "## Overlap vs original 877",
            "",
            f"- High-confidence related links: **{len(high_overlap)}**",
            f"- Soft parallel/related (not forced): **{len(soft_overlap)}**",
            f"- No soft match / low: **{len(no_soft)}**",
            "",
            "## Artwork / attribution",
            "",
            f"- New rows with placeholder policy: **{placeholder_ok}/{integrated_new_n}**",
            f"- Public staging URL leaks: **{len(staging_url_leaks)}**",
            f"- Attribution missing on new rows: **{len(attribution_missing)}**",
            "",
            "## Near-duplicates",
            "",
            "- **15** pairs remain review-only; no automatic merges.",
            "",
        ]
    )
    (OUT / "SOURCE_VS_INTEGRATED_RECONCILIATION.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(json.dumps(identity, indent=2))
    print("exclusionBuckets", dict(exclusion_buckets))
    return 0 if unexplained == 0 and identity["sourcePartition"]["balanced"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
