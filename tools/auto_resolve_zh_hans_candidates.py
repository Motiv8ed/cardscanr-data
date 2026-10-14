#!/usr/bin/env python3
"""Confidence-ranked auto-resolution of exclusive/promo/parallel/near-dup candidates.

High-confidence cases are resolved automatically; ambiguous cases stay in review queues.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition" / "pokecardex"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    final = json.loads((REPORT / "zh_hans_final_reconciliation.json").read_text(encoding="utf-8"))
    set_cls = {
        s["pokecardexSetId"]: s
        for s in json.loads((REPORT / "zh_hans_set_classifications.json").read_text(encoding="utf-8")).get(
            "sets"
        )
        or []
    }
    overlap = json.loads((REPORT / "zh_hans_catalogue_overlap_summary.json").read_text(encoding="utf-8"))
    near = json.loads((REPORT / "zh_hans_near_duplicate_review.json").read_text(encoding="utf-8"))
    # Rebuild per-standard-row classification from final summary counts isn't enough —
    # load image manifest + set classifications and apply rules.
    manifest = []
    with (REPORT / "zh_hans_image_manifest.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                manifest.append(json.loads(line))

    meta_by_key = {}
    with (REPORT / "zh_hans_cards.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if int(row.get("providerVersionId") or 1) not in {0, 1}:
                continue
            key = f"{row.get('providerSetId')}|{row.get('providerCardId')}|{row.get('providerVersionId')}"
            meta_by_key[key] = row

    # Load overlap matches keyed by... we need PC side. Index overlap jsonl by PC set+collector+en
    overlap_hits_pc: set[tuple[str, str]] = set()
    with (REPORT / "zh_hans_catalogue_overlap_matches.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("confidence", 0) >= 0.8 and row.get("pokecardex"):
                pc = row["pokecardex"]
                overlap_hits_pc.add(
                    (
                        str(pc.get("providerSetId")),
                        str(pc.get("printedCollectorNumber")),
                    )
                )

    resolved: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()

    for m in manifest:
        set_id = str(m.get("providerSetId") or "")
        sc = set_cls.get(set_id) or {}
        set_class = sc.get("setClassification") or "unmapped"
        printed = str(m.get("printedCollectorNumber") or "")
        name = str(m.get("englishCardName") or "")

        # Prior class from finalize summary was aggregate; re-derive similarly
        if sc.get("tcgdexZhCnSetId"):
            prior = "known_cn_via_tcgdex_set_absent_from_catalogue"
        elif set_class in {"en_bridge_parallel_candidate", "en_card_name_parallel_candidate"}:
            prior = "parallel_printing_candidate"
        elif set_class == "utility_energy":
            prior = "utility_energy"
        elif set_class == "promo_or_special_unmapped":
            prior = "promo_unmapped_uncertain"
        elif set_class == "chinese_exclusive_or_uncertain_set":
            prior = "chinese_exclusive_candidate"
        else:
            prior = "unmapped_uncertain"

        decision = None
        confidence = 0.0
        rationale = ""

        if prior == "known_cn_via_tcgdex_set_absent_from_catalogue":
            decision = "auto_accept_known_cn_printing"
            confidence = 0.95
            rationale = "PokéCardex set maps to TCGdex zh-cn set id"
        elif prior == "utility_energy":
            decision = "auto_accept_utility_energy"
            confidence = 0.9
            rationale = "Energy set; not a unique pokemon printing dispute"
        elif prior == "parallel_printing_candidate":
            decision = "auto_accept_parallel_printing"
            confidence = 0.8
            rationale = "EN bridge / EN name density indicates parallel printing family"
        elif (set_id, printed) in overlap_hits_pc:
            decision = "auto_link_related_to_catalogue"
            confidence = 0.85
            rationale = "High-confidence species+collector overlap with existing catalogue zh-Hans"
            prior = "catalogue_related_via_species_bridge"
        elif prior == "promo_unmapped_uncertain":
            if confidence < 0.75:
                decision = "review_promo_uncertain"
                confidence = 0.4
                rationale = "Promo/special set without TCGdex zh-cn mapping"
        elif prior == "chinese_exclusive_candidate":
            # Auto-accept exclusives when set is fully unmapped AND row has image on disk
            if m.get("onDisk") and set_class == "chinese_exclusive_or_uncertain_set":
                decision = "auto_accept_chinese_exclusive_candidate"
                confidence = 0.78
                rationale = (
                    "Unmapped CN-market set with acquired image; treat as additive metadata candidate "
                    "(not proof of uniqueness vs unpublished TCGdex rows)"
                )
            else:
                decision = "review_exclusive_uncertain"
                confidence = 0.5
                rationale = "Exclusive candidate without enough automated evidence"
        else:
            decision = "review_unmapped"
            confidence = 0.35
            rationale = "Insufficient automated evidence"

        row_out = {
            "providerSetId": set_id,
            "printedCollectorNumber": printed,
            "englishCardName": name,
            "priorClass": prior,
            "setClassification": set_class,
            "decision": decision,
            "confidence": confidence,
            "rationale": rationale,
            "onDisk": bool(m.get("onDisk")),
            "stagingKey": m.get("stagingKey"),
        }
        counts[decision] += 1
        if decision.startswith("auto_"):
            resolved.append(row_out)
        else:
            review.append(row_out)

    # Near-dup auto handling: same set + hamming<=2 → auto_flag_same_art_family; else review
    near_resolved = []
    near_review = []
    for pair in near.get("pairs") or []:
        ham = int(pair.get("hamming") or 99)
        if ham <= 2:
            near_resolved.append({**pair, "decision": "auto_flag_near_identical_art_family", "confidence": 0.85})
        elif ham <= 8:
            near_review.append({**pair, "decision": "review_near_duplicate_art", "confidence": 0.55})
        else:
            near_review.append({**pair, "decision": "review_near_duplicate_art", "confidence": 0.4})

    summary = {
        "generatedAtUtc": utc_now(),
        "standardRowsConsidered": len(manifest),
        "autoResolved": len(resolved),
        "needsReview": len(review),
        "decisionCounts": dict(counts),
        "nearDuplicateAutoFlagged": len(near_resolved),
        "nearDuplicateNeedsReview": len(near_review),
        "catalogueOverlap": {
            "highConfidenceRelated": overlap.get("highConfidenceRelated"),
            "softParallelOrRelated": overlap.get("softParallelOrRelated"),
            "noSoftMatch": overlap.get("noSoftMatch"),
            "exactSetCodeOverlap": overlap.get("exactProviderSetCodeOverlapCount"),
        },
        "thresholds": {
            "autoAcceptMin": 0.75,
            "nearDupAutoFlagMaxHamming": 2,
        },
    }
    out_dir = REPORT / "auto_resolve"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "auto_resolved.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in resolved:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (out_dir / "needs_review.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in review:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (out_dir / "near_dup_auto.json").write_text(
        json.dumps({"resolved": near_resolved, "review": near_review}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / "AUTO_RESOLVE.md").write_text(
        "\n".join(
            [
                "# Automated zh-Hans candidate resolution",
                "",
                f"Generated: {summary['generatedAtUtc']}",
                "",
                f"- Auto-resolved: **{len(resolved)}**",
                f"- Needs review: **{len(review)}**",
                f"- Decisions: `{dict(counts)}`",
                f"- Near-dup auto-flagged: **{len(near_resolved)}**; review: **{len(near_review)}**",
                "",
                "Unmatched provider IDs never auto-create 'new card' claims without evidence.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
