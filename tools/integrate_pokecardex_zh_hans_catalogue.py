#!/usr/bin/env python3
"""Integrate PokéCardex zh-Hans metadata into worktree catalogue (isolated, reviewable).

- Reads live catalogue as base (all languages preserved).
- Adds PokéCardex printings that are not high-confidence duplicates of existing zh-Hans IDs.
- Links related catalogue rows with pokecardex provider IDs where overlap is high-confidence.
- Artwork placeholders only; attribution required.
- Does NOT touch D:\\CardScanR_Data live tree.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPORT = ROOT / "reports" / "global_image_acquisition" / "pokecardex"
LIVE_CARDS = Path(r"D:\CardScanR_Data\cardscanr-data\data\global\catalogue\cards.jsonl")
OUT_DIR = ROOT / "data" / "global" / "catalogue"
REVIEW_JSONL = REPORT / "catalogue_review" / "pokecardex_zh_hans_printings_review.jsonl"
OVERLAP = REPORT / "zh_hans_catalogue_overlap_matches.jsonl"
AUTO = REPORT / "auto_resolve" / "auto_resolved.jsonl"


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


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    before = Counter()
    live_rows = load_jsonl(LIVE_CARDS)
    for row in live_rows:
        before[str(row.get("language") or "")] += 1

    # High-confidence catalogue links: attach pokecardex ids onto existing rows
    link_by_cid: dict[str, dict[str, Any]] = {}
    for row in load_jsonl(OVERLAP):
        if float(row.get("confidence") or 0) < 0.8:
            continue
        pc = row.get("pokecardex") or {}
        if not pc:
            continue
        link_by_cid[str(row.get("canonicalPrintingId"))] = pc

    auto_accept_keys: set[tuple[str, str]] = set()
    for row in load_jsonl(AUTO):
        if str(row.get("decision") or "").startswith("auto_"):
            auto_accept_keys.add(
                (str(row.get("providerSetId")), str(row.get("printedCollectorNumber")))
            )

    # Start from live catalogue
    merged: list[dict[str, Any]] = []
    linked = 0
    for row in live_rows:
        row = dict(row)
        cid = str(row.get("canonicalPrintingId") or "")
        if cid in link_by_cid and str(row.get("language")) == "zh-Hans":
            pc = link_by_cid[cid]
            providers = dict(row.get("providerCardIds") or {})
            providers["pokecardex"] = f"{pc.get('providerSetId')}-{pc.get('providerCardId')}-v1"
            row["providerCardIds"] = providers
            set_ids = dict(row.get("providerSetIds") or {})
            set_ids["pokecardex"] = str(pc.get("providerSetId"))
            row["providerSetIds"] = set_ids
            prov = list(row.get("metadataProvenance") or [])
            prov.append(
                {
                    "provider": "pokecardex",
                    "providerCardId": providers["pokecardex"],
                    "providerSetId": set_ids["pokecardex"],
                    "linkConfidence": 0.8,
                    "linkMethod": "ja_species_en_plus_collector",
                    "attribution": "Data courtesy of PokéCardex (https://pokecardex.com)",
                }
            )
            row["metadataProvenance"] = prov
            row["attributionRequired"] = "Data courtesy of PokéCardex (https://pokecardex.com)"
            linked += 1
        merged.append(row)

    existing_ids = {str(r.get("canonicalPrintingId")) for r in merged}

    # Add review-package PokéCardex rows (standard + variants) that aren't already present
    added = 0
    skipped_dup = 0
    skipped_not_auto = 0
    for row in load_jsonl(REVIEW_JSONL):
        cid = str(row.get("canonicalPrintingId") or "")
        if cid in existing_ids:
            skipped_dup += 1
            continue
        set_id = str((row.get("providerSetIds") or {}).get("pokecardex") or "")
        printed = str(row.get("printedCollectorNumber") or "")
        # Only auto-accepted standards/variants enter integration package;
        # variants of accepted sets also enter when cardVariant != unspecified
        set_class = str(row.get("setClassification") or "")
        auto_set = (set_id, printed) in auto_accept_keys
        mapped_or_bridge = set_class == "mapped_tcgdex_zh_cn" or set_class.startswith("en_")
        if not auto_set and not mapped_or_bridge:
            skipped_not_auto += 1
            continue
        # Ensure placeholders / rights
        row = dict(row)
        row["imageProvenance"] = []
        row["artworkStatus"] = "placeholder_pending_pokemon_ip_review"
        row["productionPromotionAllowed"] = False
        row["attributionRequired"] = "Data courtesy of PokéCardex (https://pokecardex.com)"
        row["integrationSource"] = "pokecardex_zh_hans_isolated_branch"
        existing_ids.add(cid)
        merged.append(row)
        added += 1

    out_cards = OUT_DIR / "cards.jsonl"
    with out_cards.open("w", encoding="utf-8", newline="\n") as handle:
        for row in merged:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    after = Counter(str(r.get("language") or "") for r in merged)
    # Crosswalk file for pokecardex
    xwalk_rows = []
    for row in merged:
        if str(row.get("language")) != "zh-Hans":
            continue
        providers = row.get("providerCardIds") or {}
        if "pokecardex" in providers:
            xwalk_rows.append(
                {
                    "canonicalPrintingId": row.get("canonicalPrintingId"),
                    "language": "zh-Hans",
                    "region": "CN",
                    "provider": "pokecardex",
                    "providerCardId": providers.get("pokecardex"),
                    "evidence": "pokecardex_metadata_written_permission_2026-10-13",
                    "source": "pokecardex",
                }
            )
    with (OUT_DIR / "provider_crosswalk_pokecardex.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in xwalk_rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    manifest = {
        "generatedAtUtc": utc_now(),
        "purpose": "isolated_branch_catalogue_integration_pokecardex_zh_hans",
        "liveCatalogueUntouched": str(LIVE_CARDS),
        "outputCards": str(out_cards),
        "beforeLanguageCounts": dict(before),
        "afterLanguageCounts": dict(after),
        "zhHansBefore": before.get("zh-Hans", 0),
        "zhHansAfter": after.get("zh-Hans", 0),
        "zhHansDelta": after.get("zh-Hans", 0) - before.get("zh-Hans", 0),
        "linkedExistingZhHansToPokecardex": linked,
        "addedPokecardexRows": added,
        "skippedAlreadyPresent": skipped_dup,
        "skippedNotAutoAccepted": skipped_not_auto,
        "enJaPreserved": {
            "enBefore": before.get("en", 0),
            "enAfter": after.get("en", 0),
            "jaBefore": before.get("ja", 0),
            "jaAfter": after.get("ja", 0),
            "unchanged": before.get("en") == after.get("en") and before.get("ja") == after.get("ja"),
        },
        "artworkPolicy": "placeholders_only",
        "attributionRequired": "Data courtesy of PokéCardex (https://pokecardex.com)",
        "productionPromotionAllowed": False,
        "readiness": {
            "metadataIntegrationReadyForReview": True,
            "searchBinderReady": True,
            "artworkProductionReady": False,
            "mergeToLiveReady": False,
            "verdict": (
                "Chinese metadata is integration-ready on this isolated branch with placeholders "
                "and attribution. Do not merge or promote artwork until Pokémon IP review and "
                "human sign-off on remaining review-queue rows."
            ),
        },
    }
    (OUT_DIR / "pokecardex_integration_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (REPORT / "ZH_HANS_INTEGRATION_READINESS.md").write_text(
        "\n".join(
            [
                "# Chinese metadata integration readiness",
                "",
                f"Generated: {manifest['generatedAtUtc']}",
                "",
                f"- zh-Hans before: **{manifest['zhHansBefore']}**",
                f"- zh-Hans after (worktree): **{manifest['zhHansAfter']}** (Δ **{manifest['zhHansDelta']}**)",
                f"- Linked existing↔PokéCardex: **{linked}**",
                f"- Added PokéCardex rows: **{added}**",
                f"- EN/JA unchanged: **{manifest['enJaPreserved']['unchanged']}**",
                "",
                "## Verdict",
                "",
                manifest["readiness"]["verdict"],
                "",
                f"- metadataIntegrationReadyForReview: **{manifest['readiness']['metadataIntegrationReadyForReview']}**",
                f"- artworkProductionReady: **{manifest['readiness']['artworkProductionReady']}**",
                f"- mergeToLiveReady: **{manifest['readiness']['mergeToLiveReady']}**",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
