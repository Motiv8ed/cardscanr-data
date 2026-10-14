#!/usr/bin/env python3
"""Prepare PokéCardex zh-Hans metadata for CardScanR catalogue review (not production merge).

Uses existing cardscanr_global_catalogue contracts for canonical IDs.
Artwork stays staged: imageProvenance uses placeholders / staging refs only.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_global_catalogue.contracts import (  # noqa: E402
    build_printing_record,
    canonicalize_language,
)
import finalize_pokecardex_zh_hans_reconcile as fin  # noqa: E402
import pokecardex_zh_hans_acquire as acq  # noqa: E402

REPORT = ROOT / "reports" / "global_image_acquisition" / "pokecardex"
OUT = REPORT / "catalogue_review"
META = REPORT / "zh_hans_cards.jsonl"
VARIANT_LINKS = REPORT / "zh_hans_variant_links.jsonl"
SET_CLASS = REPORT / "zh_hans_set_classifications.json"

VERSION_TO_CARD_VARIANT = fin.VERSION_TO_CARD_VARIANT


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


def enrich_record(record: dict[str, Any], src: dict[str, Any], *, staging_path: str | None) -> dict[str, Any]:
    """Add PokéCardex-specific review fields without breaking schema consumers."""
    record = dict(record)
    # Prefer English name from PokéCardex when present
    if src.get("englishCardName"):
        record["englishCardName"] = src.get("englishCardName")
        aliases = set(record.get("searchAliases") or [])
        aliases.add(str(src["englishCardName"]))
        record["searchAliases"] = sorted(aliases, key=lambda item: (item.casefold(), item))
    if src.get("illustrator"):
        record["illustrator"] = src.get("illustrator")
    if src.get("rarityName"):
        record["rarity"] = src.get("rarityName")
    # Attribution / rights
    record["attributionRequired"] = "Data courtesy of PokéCardex (https://pokecardex.com)"
    record["rightsStatus"] = "approved_written_permission_commercial_metadata_2026-10-13"
    record["productionPromotionAllowed"] = False
    record["artworkStatus"] = "placeholder_pending_pokemon_ip_review"
    # Clear any live image URLs; keep staging pointer for operators only
    record["imageProvenance"] = []
    if staging_path:
        record["stagingImageRef"] = {
            "path": staging_path,
            "productionPromotionAllowed": False,
            "note": "Isolated Archive staging only; do not publish or hotlink.",
        }
    else:
        record["stagingImageRef"] = None
    # Preserve original provider ids
    record.setdefault("providerCardIds", {})["pokecardex"] = str(src.get("providerCardId"))
    record.setdefault("providerSetIds", {})["pokecardex"] = str(src.get("providerSetId"))
    # Annotate provenance
    prov = list(record.get("metadataProvenance") or [])
    if prov:
        prov[0] = {
            **prov[0],
            "providerVersionId": src.get("providerVersionId"),
            "versionLabel": src.get("versionLabel"),
            "sourcePageUrl": src.get("sourcePageUrl"),
            "attribution": "Data courtesy of PokéCardex (https://pokecardex.com)",
        }
    record["metadataProvenance"] = prov
    record["reviewPackage"] = "pokecardex_zh_hans_20261014"
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--standard-only", action="store_true", help="Emit standard printings only")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    meta = load_jsonl(META)
    set_cls = {}
    if SET_CLASS.exists():
        for row in json.loads(SET_CLASS.read_text(encoding="utf-8")).get("sets") or []:
            set_cls[row["pokecardexSetId"]] = row
    variant_by_key = {}
    for row in load_jsonl(VARIANT_LINKS):
        key = (
            str(row.get("providerSetId")),
            str(row.get("providerCardId")),
            int(row.get("providerVersionId") or 0),
        )
        variant_by_key[key] = row

    records: list[dict[str, Any]] = []
    skipped = Counter()
    seen_ids: set[str] = set()
    for src in meta:
        vid = int(src.get("providerVersionId") or 1)
        if args.standard_only and vid not in {0, 1}:
            skipped["nonstandard_skipped"] += 1
            continue
        set_code = str(src.get("providerSetId") or "")
        collector = str(src.get("printedCollectorNumber") or src.get("normalizedCollectorNumber") or "")
        # Energies with em-dash: use sort as collector for identity uniqueness
        card = {
            "num_card": src.get("printedCollectorNumber"),
            "printedCollectorNumber": src.get("printedCollectorNumber"),
            "sort": src.get("sort"),
        }
        token = acq.collector_staging_token(card)
        if not collector or collector in {"—", "-", "–"}:
            collector = token
        variant = VERSION_TO_CARD_VARIANT.get(vid, f"version_{vid}")
        provider_card_id = f"{set_code}-{src.get('providerCardId')}-v{vid}"

        # Disambiguate rare collector collisions with stable provider card id suffix
        collectors_to_try = [collector, f"{collector}~pc{src.get('providerCardId')}"]
        record = None
        for coll in collectors_to_try:
            try:
                candidate = build_printing_record(
                    source_language="zh-cn",
                    provider="pokecardex",
                    provider_set_id=set_code,
                    provider_card_id=provider_card_id,
                    native_set_name=str(src.get("setName") or set_code),
                    native_card_name=str(src.get("englishCardName") or collector),
                    collector_number=str(coll),
                    official_set_total=src.get("setTotalCards"),
                    release_date=None,
                    image_url=None,
                    serie_id=str((set_cls.get(set_code) or {}).get("pokecardexBlock") or ""),
                    serie_name=str((set_cls.get(set_code) or {}).get("pokecardexBlock") or ""),
                    variant=variant,
                )
            except ValueError as exc:
                skipped["build_error"] += 1
                skipped[str(exc)[:40]] += 1
                candidate = None
            if candidate is None:
                continue
            cid = str(candidate.get("canonicalPrintingId"))
            if cid in seen_ids:
                skipped["disambiguation_retry"] += 1
                continue
            seen_ids.add(cid)
            if coll != collector:
                candidate["collectorDisambiguated"] = True
                skipped["disambiguated_collector"] += 1
            record = candidate
            break
        if record is None:
            skipped["unresolved_identity_collision"] += 1
            continue

        # Force language tag to zh-Hans (canonicalize maps zh-cn → zh-Hans)
        assert canonicalize_language("zh-cn") == "zh-Hans"
        disp = fin.display_path_for(src)
        staging_path = str(disp) if disp.exists() else None
        record = enrich_record(record, src, staging_path=staging_path)
        # Variant linkage
        vlink = variant_by_key.get((set_code, str(src.get("providerCardId")), vid))
        if vlink and vid not in {0, 1}:
            record["baseCardLink"] = {
                "baseStagingKey": vlink.get("baseStagingKey"),
                "baseProviderCardId": vlink.get("baseProviderCardId"),
                "relation": vlink.get("relation"),
                "mappingConfidence": vlink.get("mappingConfidence"),
            }
        record["setClassification"] = (set_cls.get(set_code) or {}).get("setClassification")
        records.append(record)

    unique = records

    out_jsonl = OUT / "pokecardex_zh_hans_printings_review.jsonl"
    with out_jsonl.open("w", encoding="utf-8", newline="\n") as handle:
        for row in unique:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    by_variant = Counter(str(r.get("cardVariant")) for r in unique)
    std_count = sum(1 for r in unique if r.get("cardVariant") == "unspecified")
    summary = {
        "generatedAtUtc": utc_now(),
        "purpose": "review_only_metadata_integration_package",
        "notMergedToLiveCatalogue": True,
        "language": "zh-Hans",
        "provider": "pokecardex",
        "rowsEmitted": len(unique),
        "standardUnspecifiedVariantRows": std_count,
        "variantRows": len(unique) - std_count,
        "byCardVariant": dict(by_variant),
        "skipped": dict(skipped),
        "attributionRequired": "Data courtesy of PokéCardex (https://pokecardex.com)",
        "artworkPolicy": "placeholders_only_staging_refs_not_cleared_for_production",
        "integrationNotes": [
            "Use existing search/collection/binder language=zh-Hans paths.",
            "canonicalPrintingId / canonicalBaseId / canonicalSetId from cardscanr_global_catalogue.contracts.",
            "Do not copy stagingImageRef into public CDN or app image fields until Pokémon IP review.",
            "Attribution must remain visible (website already credited; app ideally too).",
        ],
        "outputPath": str(out_jsonl),
    }
    (OUT / "POKECARDEX_ZH_HANS_CATALOGUE_REVIEW.md").write_text(
        "\n".join(
            [
                "# PokéCardex zh-Hans catalogue review package",
                "",
                f"Generated: {summary['generatedAtUtc']}",
                "",
                f"- Rows: **{len(unique)}** (standard/unspecified: **{std_count}**, other variants: **{len(unique) - std_count}**)",
                f"- Live catalogue merge: **not performed** (review only)",
                f"- Artwork: placeholders / staging refs only; productionPromotionAllowed=false",
                f"- Attribution: Data courtesy of PokéCardex (https://pokecardex.com)",
                "",
                f"Output: `{out_jsonl}`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT / "catalogue_review_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
