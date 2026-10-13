#!/usr/bin/env python3
"""Reconcile PokéCardex zh-Hans metadata vs catalogue; estimate net-new printings."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition"
LIVE = Path(r"D:\CardScanR_Data\cardscanr-data")
STAGING = Path(r"D:\CardScanR_Archive\image_staging\pokecardex_zh_hans_20261014")
META = REPORT / "pokecardex" / "zh_hans_cards.jsonl"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def norm_num(value: object) -> str:
    text = str(value or "").strip()
    left = text.split("/", 1)[0].strip()
    digits = re.sub(r"^\D+", "", left)
    digits = re.sub(r"\D+$", "", digits) or left
    try:
        return str(int(digits))
    except ValueError:
        return left.casefold()


def norm_name(value: object) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def main() -> int:
    catalogue = []
    cards_path = LIVE / "data" / "global" / "catalogue" / "cards.jsonl"
    with cards_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get("language") or "") == "zh-Hans":
                catalogue.append(row)

    meta_std = []
    if META.exists():
        with META.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if int(row.get("providerVersionId") or 1) in {0, 1}:
                    meta_std.append(row)

    cat_by_code_num: dict[tuple[str, str], int] = Counter()
    cat_by_name_num: dict[tuple[str, str], int] = Counter()
    for row in catalogue:
        num = norm_num(row.get("printedCollectorNumber") or row.get("normalizedCollectorNumber"))
        providers = row.get("providerSetIds") or {}
        if isinstance(providers, dict):
            for code in providers.values():
                if code:
                    cat_by_code_num[(str(code).casefold(), num)] += 1
        for name in (row.get("englishCardName"), row.get("nativeCardName")):
            nn = norm_name(name)
            if nn:
                cat_by_name_num[(nn, num)] += 1

    code_match = 0
    name_match = 0
    neither = 0
    by_set = Counter()
    neither_samples = []
    for row in meta_std:
        set_code = str(row.get("providerSetId") or "").casefold()
        num = norm_num(row.get("printedCollectorNumber"))
        by_set[set_code] += 1
        hit_code = cat_by_code_num.get((set_code, num), 0) > 0
        hit_name = cat_by_name_num.get((norm_name(row.get("englishCardName")), num), 0) > 0
        if hit_code:
            code_match += 1
        if hit_name:
            name_match += 1
        if not hit_code and not hit_name:
            neither += 1
            if len(neither_samples) < 25:
                neither_samples.append(
                    {
                        "set": row.get("providerSetId"),
                        "setName": row.get("setName"),
                        "num": row.get("printedCollectorNumber"),
                        "name": row.get("englishCardName"),
                    }
                )

    staged = 0
    if STAGING.exists():
        staged = sum(1 for _ in STAGING.rglob("display.webp")) + sum(
            1 for _ in STAGING.rglob("display.jpg")
        )

    # TCGdex staging preserved
    tcgdex_staging = Path(r"D:\CardScanR_Archive\image_staging\global_acq_20261013")
    tcgdex_count = 0
    if tcgdex_staging.exists():
        for lang in tcgdex_staging.iterdir():
            if lang.is_dir() and not lang.name.startswith("_"):
                tcgdex_count += sum(1 for _ in lang.rglob("display.webp"))

    payload = {
        "generatedAtUtc": utc_now(),
        "catalogueZhHansPrintings": len(catalogue),
        "pokecardexStandardMetadataRows": len(meta_std),
        "pokecardexSets": len(by_set),
        "matchByProviderSetCodeAndCollector": code_match,
        "matchByEnglishNameAndCollector": name_match,
        "noCodeOrNameMatchApproxNetNewOrUnmapped": neither,
        "neitherSamples": neither_samples,
        "stagedPokecardexImages": staged,
        "preservedTcgdexStagingDisplayWebp": tcgdex_count,
        "coverageDenominators": {
            "globalCataloguePrintings": 117665,
            "tcgdexUrlCandidates": 92817,
            "enJaProductionMaster": 68011,
            "tcgdexNonEnJaStagingValidated": tcgdex_count,
            "catalogueZhHans": len(catalogue),
            "pokecardexZhHansStandardMetadata": len(meta_std),
            "interpretation": (
                "These are parallel universes. PokéCardex zh-Hans metadata (8.9k standard) "
                "greatly exceeds the current CardScanR zh-Hans catalogue (877). "
                "Zero set-code overlap with TCGdex codes is expected; name+number soft-match "
                "estimates residual mapping / net-new candidates."
            ),
        },
        "nextOpportunities": [
            "Build set-code crosswalk PokéCardex ↔ TCGdex/official CN set IDs",
            "Continue gated image staging for remaining standard printings",
            "Pokémon/IP rights review before any production image promotion",
            "Website attribution: Data courtesy of PokéCardex",
            "KO still blocked (PokéCardex has no KO region)",
        ],
    }
    out_json = REPORT / "pokecardex" / "zh_hans_reconciliation.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md = [
        "# PokéCardex zh-Hans reconciliation",
        "",
        f"Generated: {payload['generatedAtUtc']}",
        "",
        f"- Catalogue zh-Hans printings: **{len(catalogue)}**",
        f"- PokéCardex standard metadata rows: **{len(meta_std)}** across **{len(by_set)}** sets",
        f"- Set-code + collector matches: **{code_match}**",
        f"- English-name + collector soft matches: **{name_match}**",
        f"- No code/name match (approx unmapped / net-new candidates): **{neither}**",
        f"- Staged PokéCardex images (isolated): **{staged}**",
        f"- Preserved TCGdex non-EN/JA staging: **{tcgdex_count}**",
        "",
        "## Denominator hygiene",
        "",
        payload["coverageDenominators"]["interpretation"],
        "",
        "## Next",
        "",
    ]
    for step in payload["nextOpportunities"]:
        md.append(f"- {step}")
    md.append("")
    (REPORT / "pokecardex" / "ZH_HANS_RECONCILIATION.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False)[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
