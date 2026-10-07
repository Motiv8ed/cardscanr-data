#!/usr/bin/env python3
"""Explain every set currently failing identity-level audit.

Writes machine-readable mechanism classifications — no unexplained fails.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"D:\cardscanr-data")
sys.path.insert(0, str(ROOT))

from cardscanr_catalogue_identity import (  # noqa: E402
    collector_position_key,
    name_fingerprint,
    names_compatible,
)

EN = ROOT / "public" / "v1" / "catalog" / "pokemon" / "en"
OUT = Path(
    r"D:\CardScanR\reports\catalogue_integrity_20260830\failing_set_mechanism_report.json"
)
IDENTITY_AUDIT = Path(
    r"D:\CardScanR\reports\catalogue_integrity_20260830\identity_level_set_audit.json"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def explain_set(set_id: str, meta: dict, cards: list[dict]) -> dict:
    sources = Counter(str(c.get("imageSource") or "?") for c in cards)
    by_pos: dict[str, list[dict]] = defaultdict(list)
    for card in cards:
        by_pos[collector_position_key(card.get("collectorNumber"))].append(card)

    duplicate_groups = []
    for pos, members in sorted(by_pos.items()):
        if not pos or len(members) < 2:
            continue
        names = [str(m.get("name") or "") for m in members]
        fingerprints = {name_fingerprint(n) for n in names}
        compatible_pairs = 0
        for i, left in enumerate(members):
            for right in members[i + 1 :]:
                if names_compatible(left.get("name"), right.get("name")):
                    compatible_pairs += 1
        duplicate_groups.append(
            {
                "positionKey": pos,
                "count": len(members),
                "names": names,
                "uniqueNameFingerprints": len(fingerprints),
                "compatiblePairs": compatible_pairs,
                "kind": (
                    "TRUE_DUP_GROUP"
                    if compatible_pairs and len(fingerprints) == 1
                    else "SHARED_LOCAL_DISTINCT_NAMES"
                    if len(fingerprints) > 1
                    else "MIXED"
                ),
            }
        )

    identities = {
        f"{collector_position_key(c.get('collectorNumber'))}|{name_fingerprint(c.get('name'))}"
        for c in cards
    }
    printed = meta.get("printedTotal")
    total = meta.get("total")
    expected = total if isinstance(total, int) else printed
    actual = len(cards)
    unique_identity = len(identities)

    mechanisms: list[str] = []
    if any(g["kind"] == "SHARED_LOCAL_DISTINCT_NAMES" for g in duplicate_groups):
        mechanisms.append("LEGITIMATE_SHARED_LOCAL_NUMBER")
    if sources.get("pokewallet") and sources.get("pokemon_tcg_api"):
        mechanisms.append("OVERLAPPING_PROVIDERS")
    if sources.get("pokewallet") and not sources.get("pokemon_tcg_api"):
        mechanisms.append("POKEWALLET_ONLY_OR_DOMINANT")
    lettered = sum(
        1
        for c in cards
        if any(ch.isalpha() for ch in str(c.get("collectorNumber") or ""))
    )
    if lettered and isinstance(expected, int) and actual > expected:
        mechanisms.append("LETTERED_OR_PREFIXED_EXTRAS_BEYOND_SET_TOTAL")
    if isinstance(expected, int) and actual > expected and not mechanisms:
        mechanisms.append("ACTUAL_GT_SET_TOTAL_METADATA")
    if isinstance(expected, int) and actual < expected:
        mechanisms.append("ACTUAL_LT_SET_TOTAL_METADATA")
    if set_id.endswith("sv") or "shiny" in str(meta.get("name") or "").casefold():
        mechanisms.append("SHINY_OR_VAULT_SUBSET")
    if not mechanisms:
        mechanisms.append("NEEDS_MANUAL_REVIEW")

    return {
        "setId": set_id,
        "name": meta.get("name"),
        "printedTotal": printed,
        "total": total,
        "expectedIdentityCountHint": expected,
        "actualCardCount": actual,
        "actualUniqueIdentityCount": unique_identity,
        "deltaCardsVsExpected": (actual - expected) if isinstance(expected, int) else None,
        "providerSourceBreakdown": dict(sources),
        "letteredCollectorCount": lettered,
        "duplicateGroups": duplicate_groups[:40],
        "duplicateGroupCount": len(duplicate_groups),
        "mechanisms": mechanisms,
        "samples": [
            {
                "name": c.get("name"),
                "collectorNumber": c.get("collectorNumber"),
                "imageSource": c.get("imageSource"),
                "canonicalBaseId": c.get("canonicalBaseId"),
            }
            for c in cards[:5]
        ],
    }


def main() -> int:
    sets_doc = json.loads((EN / "sets.json").read_text(encoding="utf-8"))
    meta_by_id = {str(s.get("id")): s for s in sets_doc.get("sets") or []}
    failing_ids: list[str] = []
    if IDENTITY_AUDIT.exists():
        audit = json.loads(IDENTITY_AUDIT.read_text(encoding="utf-8"))
        failing_ids = [
            str(item.get("setId"))
            for item in audit.get("failingSets") or []
            if item.get("setId")
        ]
    if not failing_ids:
        # Fallback: all sets with count mismatch vs total
        for sid, meta in meta_by_id.items():
            path = EN / "cards" / f"{sid}.json"
            if not path.exists():
                failing_ids.append(sid)
                continue
            cards = json.loads(path.read_text(encoding="utf-8")).get("cards") or []
            expected = meta.get("total") if isinstance(meta.get("total"), int) else meta.get("printedTotal")
            if isinstance(expected, int) and len(cards) != expected:
                failing_ids.append(sid)

    results = []
    unexplained = []
    for set_id in failing_ids:
        meta = meta_by_id.get(set_id) or {"id": set_id}
        path = EN / "cards" / f"{set_id}.json"
        cards = (
            [c for c in (json.loads(path.read_text(encoding="utf-8")).get("cards") or []) if isinstance(c, dict)]
            if path.exists()
            else []
        )
        explained = explain_set(set_id, meta, cards)
        results.append(explained)
        if "NEEDS_MANUAL_REVIEW" in explained["mechanisms"] and len(explained["mechanisms"]) == 1:
            unexplained.append(set_id)

    report = {
        "generatedAtUtc": utc_now(),
        "failingSetCount": len(results),
        "unexplainedSetIds": unexplained,
        "results": results,
    }
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"failingSetCount": len(results), "unexplained": unexplained}, indent=2))
    for item in results:
        print(
            item["setId"],
            item["actualCardCount"],
            "vs",
            item["expectedIdentityCountHint"],
            item["mechanisms"],
            "dups",
            item["duplicateGroupCount"],
        )
    print("wrote", OUT)
    return 1 if unexplained else 0


if __name__ == "__main__":
    raise SystemExit(main())
