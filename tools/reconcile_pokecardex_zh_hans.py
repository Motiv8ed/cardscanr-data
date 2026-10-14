#!/usr/bin/env python3
"""Reconcile all PokéCardex zh-Hans metadata rows (incl. non-standard versions).

Consumes the PokéCardex↔TCGdex crosswalk when present. Keeps denominators distinct.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition"
PDIR = REPORT / "pokecardex"
LIVE = Path(r"D:\CardScanR_Data\cardscanr-data")
STAGING = Path(r"D:\CardScanR_Archive\image_staging\pokecardex_zh_hans_20261014")
META = PDIR / "zh_hans_cards.jsonl"
CROSSWALK = PDIR / "pokecardex_tcgdex_crosswalk_summary.json"
VERSION_KIND = {
    0: "standard",
    1: "standard",
    2: "reverse",
    3: "holo_or_special",
    10: "reverse_masterball",
    11: "special_alternate",
    71: "rare_promo_style",
}


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

    meta_all: list[dict] = []
    if META.exists():
        with META.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    meta_all.append(json.loads(line))

    meta_std = [r for r in meta_all if int(r.get("providerVersionId") or 1) in {0, 1}]
    meta_nonstd = [r for r in meta_all if int(r.get("providerVersionId") or 1) not in {0, 1}]

    version_counts = Counter(int(r.get("providerVersionId") or 1) for r in meta_all)
    kind_counts = Counter(
        VERSION_KIND.get(int(r.get("providerVersionId") or 1), "other") for r in meta_all
    )

    crosswalk = {}
    if CROSSWALK.exists():
        crosswalk = json.loads(CROSSWALK.read_text(encoding="utf-8"))

    set_map_path = PDIR / "pokecardex_tcgdex_set_crosswalk.json"
    set_by_pc: dict[str, dict] = {}
    if set_map_path.exists():
        for row in json.loads(set_map_path.read_text(encoding="utf-8")).get("sets") or []:
            set_by_pc[str(row.get("pokecardexSetId"))] = row

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

    def classify(rows: list[dict]) -> dict:
        code_match = name_match = mapped_set = neither = 0
        samples = []
        for row in rows:
            set_code = str(row.get("providerSetId") or "")
            num = norm_num(row.get("printedCollectorNumber"))
            sm = set_by_pc.get(set_code) or {}
            tcg = str(sm.get("tcgdexZhCnSetId") or "")
            hit_code = cat_by_code_num.get((set_code.casefold(), num), 0) > 0
            if tcg and cat_by_code_num.get((tcg.casefold(), num), 0) > 0:
                hit_code = True
            hit_name = cat_by_name_num.get((norm_name(row.get("englishCardName")), num), 0) > 0
            in_mapped = bool(tcg)
            if hit_code:
                code_match += 1
            if hit_name:
                name_match += 1
            if in_mapped:
                mapped_set += 1
            if not hit_code and not hit_name and not in_mapped:
                neither += 1
                if len(samples) < 25:
                    samples.append(
                        {
                            "set": set_code,
                            "setName": row.get("setName"),
                            "num": row.get("printedCollectorNumber"),
                            "name": row.get("englishCardName"),
                            "versionId": row.get("providerVersionId"),
                            "versionLabel": row.get("versionLabel"),
                        }
                    )
        return {
            "rows": len(rows),
            "matchByProviderSetCodeAndCollector": code_match,
            "matchByEnglishNameAndCollector": name_match,
            "inMappedTcgdexZhCnSet": mapped_set,
            "noCodeNameOrMappedSet": neither,
            "neitherSamples": samples,
        }

    staged = 0
    if STAGING.exists():
        staged = sum(1 for _ in STAGING.rglob("display.webp")) + sum(
            1 for _ in STAGING.rglob("display.jpg")
        )
    progress = Counter()
    prog_path = STAGING / "progress.jsonl"
    if prog_path.exists():
        for line in prog_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                progress[json.loads(line).get("status")] += 1

    tcgdex_staging = Path(r"D:\CardScanR_Archive\image_staging\global_acq_20261013")
    tcgdex_count = 0
    if tcgdex_staging.exists():
        for lang in tcgdex_staging.iterdir():
            if lang.is_dir() and not lang.name.startswith("_"):
                tcgdex_count += sum(1 for _ in lang.rglob("display.webp"))

    # Variant grouping: same set+collector with multiple version ids
    by_set_num: dict[tuple[str, str], set[int]] = defaultdict(set)
    for row in meta_all:
        by_set_num[
            (str(row.get("providerSetId")), norm_num(row.get("printedCollectorNumber")))
        ].add(int(row.get("providerVersionId") or 1))
    multi_version_keys = sum(1 for vers in by_set_num.values() if len(vers) > 1)
    only_nonstd_keys = sum(
        1 for vers in by_set_num.values() if vers and not (vers & {0, 1})
    )

    std_cls = classify(meta_std)
    nonstd_cls = classify(meta_nonstd)
    all_cls = classify(meta_all)

    payload = {
        "generatedAtUtc": utc_now(),
        "catalogueZhHansPrintings": len(catalogue),
        "pokecardexMetadataRowsTotal": len(meta_all),
        "pokecardexStandardMetadataRows": len(meta_std),
        "pokecardexNonStandardMetadataRows": len(meta_nonstd),
        "versionIdCounts": {str(k): v for k, v in sorted(version_counts.items())},
        "versionKindCounts": dict(kind_counts),
        "collectorKeysWithMultipleVersions": multi_version_keys,
        "collectorKeysOnlyNonStandardVersions": only_nonstd_keys,
        "standardClassify": std_cls,
        "nonStandardClassify": nonstd_cls,
        "allRowsClassify": all_cls,
        "crosswalkSummary": {
            "mappedSets": (crosswalk.get("setCrosswalk") or {}).get("mappedToTcgdexZhCn"),
            "unmappedStandardCandidates": crosswalk.get("genuinelyUnmappedStandardCandidates"),
            "interpretation": crosswalk.get("interpretation"),
        },
        "stagedPokecardexImages": staged,
        "acquireProgressStatusCounts": dict(progress),
        "preservedTcgdexStagingDisplayWebp": tcgdex_count,
        "coverageDenominators": {
            "globalCataloguePrintings": 117665,
            "tcgdexUrlCandidates": 92817,
            "enJaProductionMaster": 68011,
            "tcgdexNonEnJaStagingValidated": tcgdex_count,
            "catalogueZhHans": len(catalogue),
            "pokecardexZhHansMetadataTotal": len(meta_all),
            "pokecardexZhHansStandardMetadata": len(meta_std),
            "pokecardexZhHansNonStandardMetadata": len(meta_nonstd),
            "acquiredPokecardexImages": staged,
            "productionClearedPokecardexImages": 0,
            "interpretation": (
                "Parallel universes — do not collapse into one %. "
                "Non-standard version rows are mostly reverse/masterball/special "
                "variants of the same collector number. Mapped TCGdex zh-cn sets "
                "prove many 'unmatched vs catalogue' rows are known CN printings, "
                "not net-new cards. Zero catalogue set-code overlap ≠ zero duplicates."
            ),
        },
        "nextOpportunities": [
            "Finish gated standard image acquire; retry CS1AC via CDN case variants",
            "Visual/OCR validation + quarantine uncertain identities",
            "Pokémon/IP rights review before production image promotion",
            "Website attribution already required: Data courtesy of PokéCardex",
            "KO: investigate authorised APIs / permitted downloads / collector contributions",
        ],
    }
    out_json = PDIR / "zh_hans_reconciliation.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md = [
        "# PokéCardex zh-Hans reconciliation (all versions)",
        "",
        f"Generated: {payload['generatedAtUtc']}",
        "",
        f"- Catalogue zh-Hans printings: **{len(catalogue)}**",
        f"- PokéCardex metadata rows: **{len(meta_all)}** "
        f"(standard **{len(meta_std)}**, non-standard **{len(meta_nonstd)}**)",
        f"- Version kinds: `{dict(kind_counts)}`",
        f"- Collector keys with multiple versions: **{multi_version_keys}**",
        f"- Standard in mapped TCGdex set: **{std_cls['inMappedTcgdexZhCnSet']}**",
        f"- Standard with no code/name/mapped-set: **{std_cls['noCodeNameOrMappedSet']}**",
        f"- Non-standard with no code/name/mapped-set: **{nonstd_cls['noCodeNameOrMappedSet']}**",
        f"- Staged PokéCardex images: **{staged}**",
        f"- Acquire progress: `{dict(progress)}`",
        f"- Preserved TCGdex staging: **{tcgdex_count}**",
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
    (PDIR / "ZH_HANS_RECONCILIATION.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False)[:3500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
