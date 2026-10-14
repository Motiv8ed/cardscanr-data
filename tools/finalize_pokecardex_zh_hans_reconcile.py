#!/usr/bin/env python3
"""Phase-1 final reconciliation for PokéCardex zh-Hans acquisition.

Reconciles standard metadata vs downloads/404s/staged files, reviews near-duplicate
art pairs, extends set/card crosswalk against the CardScanR catalogue, classifies
Chinese-exclusive candidates, and associates non-standard variants with base cards.

Does not delete staging assets. Does not restart downloads.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

import crosswalk_pokecardex_tcgdex as xwalk
import pokecardex_zh_hans_acquire as acq
import validate_staging_visual_similarity as vis

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition" / "pokecardex"
META = REPORT / "zh_hans_cards.jsonl"
STAGING = Path(r"D:\CardScanR_Archive\image_staging\pokecardex_zh_hans_20261014")
LIVE = Path(r"D:\CardScanR_Data\cardscanr-data")
TCGDEX_STAGING = Path(r"D:\CardScanR_Archive\image_staging\global_acq_20261013")

VERSION_KIND = {
    0: "standard",
    1: "standard",
    2: "reverse",
    3: "holo_or_special",
    10: "reverse_masterball",
    11: "special_alternate",
    71: "rare_promo_style",
}
VERSION_TO_CARD_VARIANT = {
    0: "unspecified",
    1: "unspecified",
    2: "reverse_holo",
    3: "holo",
    10: "reverse_masterball",
    11: "special_alternate",
    71: "rare_promo",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_meta() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with META.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_progress_best() -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    rank = {"ok": 4, "skip_existing": 3, "quarantine": 2, "permanent_404": 1}
    path = STAGING / "progress.jsonl"
    if not path.exists():
        return by_key
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = str(row.get("stagingKey") or "")
        if not key:
            continue
        prev = by_key.get(key)
        r = rank.get(str(row.get("status")), 0)
        pr = rank.get(str(prev.get("status")), 0) if prev else -1
        if prev is None or r >= pr:
            by_key[key] = row
    return by_key


def meta_card_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "num_card": row.get("printedCollectorNumber"),
        "printedCollectorNumber": row.get("printedCollectorNumber"),
        "sort": row.get("sort"),
        "ordre_affichage": row.get("sort"),
    }


def staging_key_for(row: dict[str, Any]) -> str:
    set_code = str(row.get("providerSetId") or "")
    ver = int(row.get("providerVersionId") or 1)
    token = acq.collector_staging_token(meta_card_dict(row))
    return f"pokecardex|zh-Hans|{set_code}|{token}|v{ver}"


def display_path_for(row: dict[str, Any]) -> Path:
    set_code = str(row.get("providerSetId") or "")
    ver = str(int(row.get("providerVersionId") or 1))
    token = acq.collector_staging_token(meta_card_dict(row))
    return STAGING / "zh-Hans" / acq.safe_seg(set_code) / f"{acq.safe_seg(token)}_v{acq.safe_seg(ver)}" / "display.webp"


def load_catalogue() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    path = LIVE / "data" / "global" / "catalogue" / "cards.jsonl"
    zh: list[dict[str, Any]] = []
    en: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            lang = str(row.get("language") or "")
            if lang == "zh-Hans":
                zh.append(row)
            elif lang == "en":
                en.append(row)
    return zh, en


def index_catalogue(rows: list[dict[str, Any]]) -> tuple[
    dict[tuple[str, str], list[dict[str, Any]]],
    dict[tuple[str, str], list[dict[str, Any]]],
]:
    by_code: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        num = xwalk.norm_num(row.get("printedCollectorNumber") or row.get("normalizedCollectorNumber"))
        providers = row.get("providerSetIds") or {}
        if isinstance(providers, dict):
            for code in providers.values():
                if code:
                    by_code[(str(code).casefold(), num)].append(row)
        csid = str(row.get("canonicalSetId") or "")
        if "tcgdex:" in csid:
            by_code[(csid.split("tcgdex:", 1)[1].casefold(), num)].append(row)
        for name in (row.get("englishCardName"), row.get("nativeCardName")):
            nn = xwalk.norm_name(name)
            if nn:
                by_name[(nn, num)].append(row)
    return by_code, by_name


def review_near_duplicates(limit_pairs: int = 50) -> list[dict[str, Any]]:
    """Recompute near-duplicate art pairs for human review (no quarantine writes)."""
    pairs: list[dict[str, Any]] = []
    by_set: dict[str, list[tuple[str, Path, str]]] = defaultdict(list)
    for asset in STAGING.glob("zh-Hans/*/*/asset.json"):
        display = asset.parent / "display.webp"
        if not display.exists():
            continue
        try:
            meta = json.loads(asset.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        set_id = str(meta.get("providerSetId") or asset.parent.parent.name)
        num = str(meta.get("normalizedCollectorNumber") or asset.parent.name)
        rgb = vis.load_rgb(display)
        if rgb is None:
            continue
        bits = vis.dhash_bits(vis.art_crop(rgb))
        pack = "".join("1" if b else "0" for b in bits.tolist())
        by_set[set_id].append((pack, asset, num))

    for set_id, items in by_set.items():
        for i in range(len(items)):
            for j in range(i + 1, min(i + 60, len(items))):
                a_bits = np.array([c == "1" for c in items[i][0]])
                b_bits = np.array([c == "1" for c in items[j][0]])
                dist = vis.hamming(a_bits, b_bits)
                if dist <= 8 and items[i][2] != items[j][2]:
                    pairs.append(
                        {
                            "set": set_id,
                            "hamming": dist,
                            "a": {
                                "collector": items[i][2],
                                "path": str(items[i][1].parent),
                                "stagingKey": json.loads(items[i][1].read_text(encoding="utf-8")).get(
                                    "stagingKey"
                                ),
                            },
                            "b": {
                                "collector": items[j][2],
                                "path": str(items[j][1].parent),
                                "stagingKey": json.loads(items[j][1].read_text(encoding="utf-8")).get(
                                    "stagingKey"
                                ),
                            },
                            "reviewStatus": "needs_human_review",
                            "note": (
                                "Near-identical art crop across different collectors in the same set. "
                                "May be true alt-art siblings, energy lookalikes, or mis-labelled staging."
                            ),
                        }
                    )
                    if len(pairs) >= limit_pairs:
                        return pairs
    return pairs


def main() -> int:
    REPORT.mkdir(parents=True, exist_ok=True)
    meta = load_meta()
    std = [r for r in meta if int(r.get("providerVersionId") or 1) in {0, 1}]
    nonstd = [r for r in meta if int(r.get("providerVersionId") or 1) not in {0, 1}]
    progress = load_progress_best()

    # --- Image manifest for standard rows ---
    acquired = 0
    gap_404: list[dict[str, Any]] = []
    gap_missing: list[dict[str, Any]] = []
    image_manifest: list[dict[str, Any]] = []
    for row in std:
        key = staging_key_for(row)
        disp = display_path_for(row)
        prog = progress.get(key) or {}
        status = str(prog.get("status") or "")
        on_disk = disp.exists()
        if on_disk:
            acquired += 1
            layer = "acquired"
        elif status == "permanent_404":
            layer = "permanent_404"
            gap_404.append(
                {
                    "stagingKey": key,
                    "set": row.get("providerSetId"),
                    "setName": row.get("setName"),
                    "printedCollectorNumber": row.get("printedCollectorNumber"),
                    "englishCardName": row.get("englishCardName"),
                    "sort": row.get("sort"),
                    "progressReason": prog.get("reason"),
                    "triedUrls": prog.get("triedUrls"),
                }
            )
        else:
            layer = "missing_unexpected"
            gap_missing.append(
                {
                    "stagingKey": key,
                    "set": row.get("providerSetId"),
                    "printedCollectorNumber": row.get("printedCollectorNumber"),
                    "progressStatus": status or None,
                }
            )
        image_manifest.append(
            {
                "stagingKey": key,
                "providerSetId": row.get("providerSetId"),
                "providerCardId": row.get("providerCardId"),
                "printedCollectorNumber": row.get("printedCollectorNumber"),
                "normalizedCollectorNumber": row.get("normalizedCollectorNumber"),
                "stagingToken": acq.collector_staging_token(meta_card_dict(row)),
                "englishCardName": row.get("englishCardName"),
                "displayPath": str(disp) if on_disk else None,
                "onDisk": on_disk,
                "progressStatus": status or None,
                "layer": layer,
                "productionPromotionAllowed": False,
            }
        )

    # Orphan / extra display files (not matching any standard staging key path)
    std_paths = {display_path_for(r).resolve() for r in std}
    all_displays = [p.resolve() for p in STAGING.glob("zh-Hans/*/*/display.webp")]
    orphans = [str(p) for p in all_displays if p not in std_paths]
    # Also count nonstd displays if any
    nonstd_paths = set()
    for row in nonstd:
        p = display_path_for(row)
        if p.exists():
            nonstd_paths.add(p.resolve())
    orphan_standard_extra = [p for p in orphans if Path(p).resolve() not in nonstd_paths]

    tcgdex_count = 0
    if TCGDEX_STAGING.exists():
        for lang in TCGDEX_STAGING.iterdir():
            if lang.is_dir() and not lang.name.startswith("_"):
                tcgdex_count += sum(1 for _ in lang.rglob("display.webp"))

    # --- Set crosswalk (refresh) ---
    discovered = json.loads((REPORT / "chn_sets_discovered.json").read_text(encoding="utf-8"))
    tcgdex = xwalk.ensure_tcgdex_sets()
    set_map = xwalk.match_sets(discovered.get("sets") or [], tcgdex)
    set_by_pc = {r["pokecardexSetId"]: r for r in set_map}

    zh_cat, en_cat = load_catalogue()
    zh_by_code, zh_by_name = index_catalogue(zh_cat)
    en_by_code, en_by_name = index_catalogue(en_cat)

    # Soft-match remaining unmatched sets via EN card-name density
    set_classifications: list[dict[str, Any]] = []
    for sm in set_map:
        code = sm["pokecardexSetId"]
        method = sm.get("matchMethod")
        if sm.get("tcgdexZhCnSetId"):
            cls = "mapped_tcgdex_zh_cn"
        elif sm.get("enBridgeSetId"):
            cls = "en_bridge_parallel_candidate"
        else:
            # sample standard cards for EN name hit rate
            cards = [r for r in std if r.get("providerSetId") == code]
            hits = 0
            checked = 0
            for r in cards[:40]:
                nn = xwalk.norm_name(r.get("englishCardName"))
                num = xwalk.norm_num(r.get("printedCollectorNumber"))
                checked += 1
                if nn and en_by_name.get((nn, num)):
                    hits += 1
            ratio = (hits / checked) if checked else 0.0
            if ratio >= 0.5:
                cls = "en_card_name_parallel_candidate"
                sm = {
                    **sm,
                    "matchMethod": "en_card_name_density",
                    "confidence": round(0.4 + 0.4 * ratio, 3),
                    "enNameHitRatio": round(ratio, 3),
                }
            elif code in {"NRGY"}:
                cls = "utility_energy"
            elif code in {"MP", "SMP", "SPP", "SVP", "SVPU", "30THP"}:
                cls = "promo_or_special_unmapped"
            else:
                cls = "chinese_exclusive_or_uncertain_set"
        set_classifications.append({**sm, "setClassification": cls})

    set_by_pc = {r["pokecardexSetId"]: r for r in set_classifications}

    # --- Card classification (standard) ---
    class_counts: Counter[str] = Counter()
    exclusive_samples: list[dict[str, Any]] = []
    catalogue_hits = 0
    for row in std:
        code = str(row.get("providerSetId") or "")
        sm = set_by_pc.get(code) or {}
        num = xwalk.norm_num(row.get("printedCollectorNumber"))
        en_name = xwalk.norm_name(row.get("englishCardName"))
        tcg = str(sm.get("tcgdexZhCnSetId") or "")
        cat_hit = False
        cat_via = None
        for c in filter(None, [code.casefold(), tcg.casefold()]):
            if zh_by_code.get((c, num)):
                cat_hit = True
                cat_via = "set_code+collector"
                break
        if not cat_hit and en_name and zh_by_name.get((en_name, num)):
            cat_hit = True
            cat_via = "english_name+collector"
        en_parallel = bool(en_name and en_by_name.get((en_name, num)))
        set_cls = sm.get("setClassification") or "unmapped"
        if cat_hit:
            status = "overlaps_catalogue_zh_hans"
            catalogue_hits += 1
        elif tcg:
            status = "known_cn_via_tcgdex_set_absent_from_catalogue"
        elif set_cls in {"en_bridge_parallel_candidate", "en_card_name_parallel_candidate"} or en_parallel:
            status = "parallel_printing_candidate"
        elif set_cls == "utility_energy":
            status = "utility_energy"
        elif set_cls == "promo_or_special_unmapped":
            status = "promo_unmapped_uncertain"
        elif set_cls == "chinese_exclusive_or_uncertain_set":
            status = "chinese_exclusive_candidate"
        else:
            status = "unmapped_uncertain"
        class_counts[status] += 1
        if status == "chinese_exclusive_candidate" and len(exclusive_samples) < 40:
            exclusive_samples.append(
                {
                    "set": code,
                    "setName": row.get("setName"),
                    "num": row.get("printedCollectorNumber"),
                    "name": row.get("englishCardName"),
                }
            )

    # --- Variant → base association ---
    by_base: dict[tuple[str, str], dict[str, Any]] = {}
    for row in std:
        code = str(row.get("providerSetId") or "")
        num = xwalk.norm_num(row.get("printedCollectorNumber"))
        token = acq.collector_staging_token(meta_card_dict(row))
        # Prefer numeric collector for base identity; else sort token
        base_num = num if num and re.fullmatch(r"\d+", str(num)) else token
        by_base[(code, base_num)] = {
            "providerSetId": code,
            "setName": row.get("setName"),
            "collectorKey": base_num,
            "printedCollectorNumber": row.get("printedCollectorNumber"),
            "englishCardName": row.get("englishCardName"),
            "providerCardId": row.get("providerCardId"),
            "baseVersionId": 1,
            "baseStagingKey": staging_key_for(row),
        }

    variant_links: list[dict[str, Any]] = []
    linked = 0
    unlinked = 0
    for row in nonstd:
        code = str(row.get("providerSetId") or "")
        num = xwalk.norm_num(row.get("printedCollectorNumber"))
        token = acq.collector_staging_token(meta_card_dict(row))
        base_num = num if num and re.fullmatch(r"\d+", str(num)) else token
        base = by_base.get((code, base_num))
        vid = int(row.get("providerVersionId") or 0)
        kind = VERSION_KIND.get(vid, f"other_{vid}")
        card_variant = VERSION_TO_CARD_VARIANT.get(vid, f"version_{vid}")
        if base:
            linked += 1
            conf = 0.95 if num and re.fullmatch(r"\d+", str(num)) else 0.7
            relation = "same_collector_nonstandard_finish"
        else:
            unlinked += 1
            conf = 0.0
            relation = "no_standard_base_found"
        variant_links.append(
            {
                "providerSetId": code,
                "providerCardId": row.get("providerCardId"),
                "providerVersionId": vid,
                "versionKind": kind,
                "cardVariant": card_variant,
                "printedCollectorNumber": row.get("printedCollectorNumber"),
                "englishCardName": row.get("englishCardName"),
                "baseCollectorKey": base_num if base else None,
                "baseProviderCardId": (base or {}).get("providerCardId"),
                "baseStagingKey": (base or {}).get("baseStagingKey"),
                "relation": relation,
                "mappingConfidence": conf,
                "originalIdsPreserved": True,
            }
        )

    near_dups = review_near_duplicates()

    # Progress status recount (deduped)
    prog_counts = Counter(str(v.get("status")) for v in progress.values())

    summary = {
        "generatedAtUtc": utc_now(),
        "phase": "final_reconciliation",
        "denominators": {
            "standardMetadataRows": len(std),
            "nonStandardMetadataRows": len(nonstd),
            "metadataRowsTotal": len(meta),
            "stagedDisplayWebp": len(all_displays),
            "standardRowsWithDisplayOnDisk": acquired,
            "progressPermanent404Deduped": len(gap_404),
            "progressOkDeduped": prog_counts.get("ok", 0),
            "progressSkipExistingDeduped": prog_counts.get("skip_existing", 0),
            "preservedTcgdexStaging": tcgdex_count,
            "catalogueZhHans": len(zh_cat),
            "productionCleared": 0,
        },
        "arithmeticCheck": {
            "standard": len(std),
            "onDisk": acquired,
            "permanent404": len(gap_404),
            "missingUnexpected": len(gap_missing),
            "sumOnDiskPlus404PlusMissing": acquired + len(gap_404) + len(gap_missing),
            "balanced": acquired + len(gap_404) + len(gap_missing) == len(std),
            "extraDisplayFilesVsStandardPaths": len(orphan_standard_extra),
            "interpretation": (
                f"{len(std)} standard rows = {acquired} on-disk + {len(gap_404)} true CDN 404s "
                f"+ {len(gap_missing)} unexpected missing. "
                f"Staged webp total {len(all_displays)} exceeds standard on-disk paths by "
                f"{len(all_displays) - acquired} (orphans/extras/nonstd leftovers); "
                "valid assets are preserved — extras are inventoried, not deleted."
            ),
        },
        "gaps404": gap_404,
        "gapsMissingUnexpected": gap_missing,
        "orphanDisplayFiles": orphan_standard_extra[:50],
        "orphanDisplayFileCount": len(orphan_standard_extra),
        "nearDuplicateArtPairs": near_dups,
        "nearDuplicateArtPairCount": len(near_dups),
        "setCrosswalk": {
            "pokecardexSets": len(set_classifications),
            "byClassification": dict(Counter(r["setClassification"] for r in set_classifications)),
            "methods": dict(Counter(r.get("matchMethod") for r in set_classifications)),
        },
        "standardCardClassification": dict(class_counts),
        "catalogueZhHansSoftHits": catalogue_hits,
        "chineseExclusiveStandardCandidates": class_counts.get("chinese_exclusive_candidate", 0),
        "parallelPrintingStandardCandidates": class_counts.get("parallel_printing_candidate", 0),
        "knownCnViaTcgdexAbsentFromCatalogue": class_counts.get(
            "known_cn_via_tcgdex_set_absent_from_catalogue", 0
        ),
        "exclusiveSamples": exclusive_samples,
        "variants": {
            "nonStandardRows": len(nonstd),
            "linkedToStandardBase": linked,
            "unlinked": unlinked,
            "versionKindCounts": dict(Counter(VERSION_KIND.get(int(r.get("providerVersionId") or 0), "other") for r in nonstd)),
        },
        "rights": {
            "metadata": "approved_written_permission_commercial_metadata_2026-10-13",
            "images": "staging_only_pokemon_ip_pending",
            "attributionRequired": "Data courtesy of PokéCardex (https://pokecardex.com)",
            "productionPromotionAllowed": False,
        },
        "artifactPaths": {
            "imageManifest": str(REPORT / "zh_hans_image_manifest.jsonl"),
            "variantLinks": str(REPORT / "zh_hans_variant_links.jsonl"),
            "setClassifications": str(REPORT / "zh_hans_set_classifications.json"),
            "nearDupReview": str(REPORT / "zh_hans_near_duplicate_review.json"),
            "summary": str(REPORT / "zh_hans_final_reconciliation.json"),
        },
    }

    with (REPORT / "zh_hans_image_manifest.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in image_manifest:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (REPORT / "zh_hans_variant_links.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in variant_links:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (REPORT / "zh_hans_set_classifications.json").write_text(
        json.dumps({"generatedAtUtc": utc_now(), "sets": set_classifications}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    (REPORT / "zh_hans_near_duplicate_review.json").write_text(
        json.dumps(
            {
                "generatedAtUtc": utc_now(),
                "pairCount": len(near_dups),
                "pairs": near_dups,
                "action": "review_only_no_auto_delete",
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (REPORT / "zh_hans_final_reconciliation.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    md = [
        "# PokéCardex zh-Hans final reconciliation",
        "",
        f"Generated: {summary['generatedAtUtc']}",
        "",
        "## Arithmetic",
        "",
        summary["arithmeticCheck"]["interpretation"],
        "",
        f"- Standard metadata: **{len(std)}**",
        f"- On-disk displays for standard paths: **{acquired}**",
        f"- True CDN 404s: **{len(gap_404)}**",
        f"- Unexpected missing: **{len(gap_missing)}**",
        f"- Staged webp total: **{len(all_displays)}** (extras/orphans: **{len(orphan_standard_extra)}**, preserved)",
        f"- TCGdex staging preserved: **{tcgdex_count}**",
        "",
        "## Standard card classification",
        "",
        f"`{dict(class_counts)}`",
        "",
        f"- Chinese-exclusive candidates: **{class_counts.get('chinese_exclusive_candidate', 0)}**",
        f"- Parallel printing candidates: **{class_counts.get('parallel_printing_candidate', 0)}**",
        f"- Known CN via TCGdex (absent from catalogue): **{class_counts.get('known_cn_via_tcgdex_set_absent_from_catalogue', 0)}**",
        "",
        "## Variants",
        "",
        f"- Non-standard rows: **{len(nonstd)}**",
        f"- Linked to standard base: **{linked}**",
        f"- Unlinked: **{unlinked}**",
        "",
        "## Near-duplicate art",
        "",
        f"- Pairs for review: **{len(near_dups)}** (no assets deleted)",
        "",
    ]
    (REPORT / "ZH_HANS_FINAL_RECONCILIATION.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in summary if k not in {"gaps404", "exclusiveSamples", "orphanDisplayFiles", "nearDuplicateArtPairs", "gapsMissingUnexpected"}}, indent=2, ensure_ascii=False)[:4500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
