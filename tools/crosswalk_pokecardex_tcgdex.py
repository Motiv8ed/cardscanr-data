#!/usr/bin/env python3
"""Build PokéCardex ↔ TCGdex canonical set/card crosswalk for zh-Hans / zh-cn.

Zero matching set codes against the *CardScanR catalogue* does NOT imply zero
duplicate cards: PokéCardex and TCGdex zh-cn share many set IDs (exact and
half-set transforms like CS15C ↔ CS1.5C). Card-level matches use collector
number + English name corroboration within mapped sets.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition" / "pokecardex"
META = REPORT / "zh_hans_cards.jsonl"
LIVE = Path(r"D:\CardScanR_Data\cardscanr-data")
UA = "CardScanR-CatalogueSync/1.0 (+crosswalk; rate-limited)"
VERSION_KIND = {
    0: "standard",
    1: "standard",
    2: "reverse",
    3: "holo_or_special",
    10: "reverse_masterball",
    11: "special_alternate",
    71: "rare_promo_style",
}
# Curated bridges where automated ID/name matching is insufficient.
# zh-cn id optional; en id used for parallel-language evidence only.
MANUAL_SET_ALIASES: dict[str, dict[str, str]] = {
    "151C": {"enId": "sv3pt5", "method": "manual_alias_collect_151"},
    "30THP": {"enId": "30th", "method": "manual_alias_30th_promo"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def norm_name(value: object) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm_num(value: object) -> str:
    text = str(value or "").strip()
    left = text.split("/", 1)[0].strip()
    digits = re.sub(r"^\D+", "", left)
    digits = re.sub(r"\D+$", "", digits) or left
    try:
        return str(int(digits))
    except ValueError:
        return left.casefold()


def half_set_tcgdex_id(pokecardex_code: str) -> str | None:
    """Map PokéCardex CS15C / CSV95C / CSM15C → TCGdex CS1.5C / CSV9.5C / CSM1.5C."""
    m = re.fullmatch(r"([A-Za-z]+)(\d)5([A-Za-z]*)", str(pokecardex_code or ""))
    if not m:
        return None
    return f"{m.group(1)}{m.group(2)}.5{m.group(3)}"


def token_overlap(a: str, b: str) -> float:
    ta = set(norm_name(a).split())
    tb = set(norm_name(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_tcgdex_sets() -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for lang in ("zh-cn", "en", "ja"):
        path = REPORT / f"tcgdex_sets_{lang}.json"
        if not path.exists():
            req = urllib.request.Request(
                f"https://api.tcgdex.net/v2/{lang}/sets",
                headers={"User-Agent": UA},
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                path.write_bytes(resp.read())
        data = load_json(path)
        out[lang] = data if isinstance(data, list) else []
    return out


def match_sets(
    pc_sets: list[dict[str, Any]],
    tcgdex: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    zh = {str(s.get("id") or ""): s for s in tcgdex["zh-cn"]}
    zh_cf = {k.casefold(): v for k, v in zh.items()}
    en = {str(s.get("id") or ""): s for s in tcgdex["en"]}
    en_by_name = {norm_name(s.get("name")): s for s in tcgdex["en"] if norm_name(s.get("name"))}

    rows: list[dict[str, Any]] = []
    used_zh: set[str] = set()
    for pc in pc_sets:
        code = str(pc.get("shortName") or "")
        name = str(pc.get("fullName") or "")
        match: dict[str, Any] = {
            "pokecardexSetId": code,
            "pokecardexSetName": name,
            "pokecardexBlock": pc.get("block"),
            "tcgdexZhCnSetId": None,
            "tcgdexZhCnSetName": None,
            "tcgdexReleaseDate": None,
            "tcgdexOfficialCardCount": None,
            "matchMethod": "unmatched",
            "confidence": 0.0,
            "enBridgeSetId": None,
            "enBridgeSetName": None,
        }
        # 0) Manual aliases (CN exclusives / naming mismatches)
        alias = MANUAL_SET_ALIASES.get(code)
        if alias:
            en_id = str(alias.get("enId") or "")
            zh_id = str(alias.get("zhCnId") or "")
            if zh_id and zh_id.casefold() in zh_cf and zh_id not in used_zh:
                hit = zh_cf[zh_id.casefold()]
                used_zh.add(str(hit.get("id")))
                match.update(
                    {
                        "tcgdexZhCnSetId": hit.get("id"),
                        "tcgdexZhCnSetName": hit.get("name"),
                        "tcgdexReleaseDate": hit.get("releaseDate"),
                        "tcgdexOfficialCardCount": (hit.get("cardCount") or {}).get("official")
                        if isinstance(hit.get("cardCount"), dict)
                        else hit.get("cardCount"),
                        "enBridgeSetId": en_id or None,
                        "enBridgeSetName": (en.get(en_id) or {}).get("name"),
                        "matchMethod": alias.get("method") or "manual_alias",
                        "confidence": 0.9,
                    }
                )
                rows.append(match)
                continue
            if en_id and en_id in en:
                match["enBridgeSetId"] = en_id
                match["enBridgeSetName"] = en[en_id].get("name")
                match["matchMethod"] = alias.get("method") or "manual_alias_en_bridge"
                match["confidence"] = 0.7
                rows.append(match)
                continue

        # 1) Exact ID (case-insensitive)
        if code.casefold() in zh_cf:
            hit = zh_cf[code.casefold()]
            tid = str(hit.get("id"))
            if tid not in used_zh:
                used_zh.add(tid)
                match.update(
                    {
                        "tcgdexZhCnSetId": tid,
                        "tcgdexZhCnSetName": hit.get("name"),
                        "tcgdexReleaseDate": hit.get("releaseDate"),
                        "tcgdexOfficialCardCount": (hit.get("cardCount") or {}).get("official")
                        if isinstance(hit.get("cardCount"), dict)
                        else hit.get("cardCount"),
                        "matchMethod": "exact_set_id",
                        "confidence": 1.0,
                    }
                )
                rows.append(match)
                continue
        # 2) Half-set transform CS15C -> CS1.5C
        half = half_set_tcgdex_id(code)
        if half and half.casefold() in zh_cf:
            hit = zh_cf[half.casefold()]
            tid = str(hit.get("id"))
            if tid not in used_zh:
                used_zh.add(tid)
                match.update(
                    {
                        "tcgdexZhCnSetId": tid,
                        "tcgdexZhCnSetName": hit.get("name"),
                        "tcgdexReleaseDate": hit.get("releaseDate"),
                        "tcgdexOfficialCardCount": (hit.get("cardCount") or {}).get("official")
                        if isinstance(hit.get("cardCount"), dict)
                        else hit.get("cardCount"),
                        "matchMethod": "half_set_transform",
                        "confidence": 0.95,
                    }
                )
                rows.append(match)
                continue
        # 3) English name exact
        nn = norm_name(name)
        if nn and nn in en_by_name:
            en_hit = en_by_name[nn]
            match["enBridgeSetId"] = en_hit.get("id")
            match["enBridgeSetName"] = en_hit.get("name")
            # Prefer a zh-cn set with same id as EN if present, else name-only bridge
            en_id = str(en_hit.get("id") or "")
            if en_id.casefold() in zh_cf:
                hit = zh_cf[en_id.casefold()]
                tid = str(hit.get("id"))
                if tid not in used_zh:
                    used_zh.add(tid)
                    match.update(
                        {
                            "tcgdexZhCnSetId": tid,
                            "tcgdexZhCnSetName": hit.get("name"),
                            "tcgdexReleaseDate": hit.get("releaseDate"),
                            "tcgdexOfficialCardCount": (hit.get("cardCount") or {}).get("official")
                            if isinstance(hit.get("cardCount"), dict)
                            else hit.get("cardCount"),
                            "matchMethod": "en_name_then_zh_id",
                            "confidence": 0.85,
                        }
                    )
                    rows.append(match)
                    continue
            match["matchMethod"] = "en_name_bridge_only"
            match["confidence"] = 0.55
            rows.append(match)
            continue
        # 4) Fuzzy name vs EN (high threshold)
        best = None
        best_score = 0.0
        for s in tcgdex["en"]:
            score = token_overlap(name, str(s.get("name") or ""))
            if score > best_score:
                best_score = score
                best = s
        if best and best_score >= 0.8:
            match["enBridgeSetId"] = best.get("id")
            match["enBridgeSetName"] = best.get("name")
            match["matchMethod"] = "en_name_fuzzy"
            match["confidence"] = round(0.45 + 0.4 * best_score, 3)
            rows.append(match)
            continue
        rows.append(match)
    return rows


def load_metadata() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not META.exists():
        return rows
    with META.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_catalogue_zh() -> list[dict[str, Any]]:
    path = LIVE / "data" / "global" / "catalogue" / "cards.jsonl"
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get("language") or "") == "zh-Hans":
                rows.append(row)
    return rows


def card_crosswalk(
    meta: list[dict[str, Any]],
    set_map: list[dict[str, Any]],
    catalogue: list[dict[str, Any]],
) -> dict[str, Any]:
    set_by_pc = {r["pokecardexSetId"]: r for r in set_map}
    # Catalogue indexes via mapped TCGdex codes + native provider codes
    cat_by_code_num: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    cat_by_name_num: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in catalogue:
        num = norm_num(row.get("printedCollectorNumber") or row.get("normalizedCollectorNumber"))
        providers = row.get("providerSetIds") or {}
        codes: set[str] = set()
        if isinstance(providers, dict):
            for v in providers.values():
                if v:
                    codes.add(str(v).casefold())
        csid = str(row.get("canonicalSetId") or "")
        if "tcgdex:" in csid:
            codes.add(csid.split("tcgdex:", 1)[1].casefold())
        for code in codes:
            cat_by_code_num[(code, num)].append(row)
        for name in (row.get("englishCardName"), row.get("nativeCardName")):
            nn = norm_name(name)
            if nn:
                cat_by_name_num[(nn, num)].append(row)

    version_counts = Counter()
    kind_counts = Counter()
    match_counts = Counter()
    additive_std = 0
    additive_variant = 0
    samples_additive: list[dict[str, Any]] = []
    samples_matched: list[dict[str, Any]] = []
    card_rows_out: list[dict[str, Any]] = []

    for row in meta:
        vid = int(row.get("providerVersionId") or 1)
        version_counts[vid] += 1
        kind = VERSION_KIND.get(vid, f"other_{vid}")
        kind_counts[kind] += 1
        pc_set = str(row.get("providerSetId") or "")
        sm = set_by_pc.get(pc_set) or {}
        tcg_set = sm.get("tcgdexZhCnSetId")
        num = norm_num(row.get("printedCollectorNumber") or row.get("normalizedCollectorNumber"))
        en_name = norm_name(row.get("englishCardName"))

        hit_tcgdex_synth = bool(tcg_set and num)
        # Catalogue hit via pokecardex code OR mapped tcgdex code OR english name
        cat_hit = False
        cat_via = None
        for code in filter(None, [pc_set.casefold(), str(tcg_set or "").casefold()]):
            if cat_by_code_num.get((code, num)):
                cat_hit = True
                cat_via = "set_code+collector"
                break
        if not cat_hit and en_name and cat_by_name_num.get((en_name, num)):
            cat_hit = True
            cat_via = "english_name+collector"

        if hit_tcgdex_synth and cat_hit:
            status = "duplicate_in_catalogue_and_tcgdex_set"
        elif hit_tcgdex_synth:
            status = "in_tcgdex_zh_cn_set_not_in_catalogue"
        elif cat_hit:
            status = "in_catalogue_unmapped_or_bridge"
        else:
            status = "genuinely_unmapped_candidate"
        match_counts[status] += 1

        is_std = vid in {0, 1}
        if status == "genuinely_unmapped_candidate":
            if is_std:
                additive_std += 1
            else:
                additive_variant += 1
            if len(samples_additive) < 40:
                samples_additive.append(
                    {
                        "set": pc_set,
                        "setName": row.get("setName"),
                        "num": row.get("printedCollectorNumber"),
                        "name": row.get("englishCardName"),
                        "versionId": vid,
                        "versionLabel": row.get("versionLabel"),
                        "kind": kind,
                    }
                )
        elif len(samples_matched) < 15 and hit_tcgdex_synth:
            samples_matched.append(
                {
                    "pokecardexSetId": pc_set,
                    "tcgdexZhCnSetId": tcg_set,
                    "num": num,
                    "name": row.get("englishCardName"),
                    "syntheticTcgdexCardId": f"{tcg_set}-{num}",
                    "catalogueVia": cat_via,
                }
            )

        card_rows_out.append(
            {
                "pokecardexSetId": pc_set,
                "tcgdexZhCnSetId": tcg_set,
                "setMatchMethod": sm.get("matchMethod"),
                "setConfidence": sm.get("confidence"),
                "collector": num,
                "englishCardName": row.get("englishCardName"),
                "providerVersionId": vid,
                "versionKind": kind,
                "syntheticTcgdexCardId": f"{tcg_set}-{num}" if tcg_set and num else None,
                "status": status,
                "catalogueMatchVia": cat_via,
            }
        )

    mapped_sets = sum(1 for r in set_map if r.get("tcgdexZhCnSetId"))
    bridge_only = sum(1 for r in set_map if not r.get("tcgdexZhCnSetId") and r.get("enBridgeSetId"))
    unmatched_sets = sum(1 for r in set_map if r.get("matchMethod") == "unmatched")

    # Standard rows that land in a mapped TCGdex set are "known CN printings"
    # even when absent from CardScanR catalogue — not necessarily net-new physical cards.
    std_in_mapped = sum(
        1
        for r in meta
        if int(r.get("providerVersionId") or 1) in {0, 1}
        and (set_by_pc.get(str(r.get("providerSetId") or "")) or {}).get("tcgdexZhCnSetId")
    )
    std_total = sum(1 for r in meta if int(r.get("providerVersionId") or 1) in {0, 1})

    return {
        "generatedAtUtc": utc_now(),
        "metadataRowsTotal": len(meta),
        "versionIdCounts": {str(k): v for k, v in sorted(version_counts.items())},
        "versionKindCounts": dict(kind_counts),
        "setCrosswalk": {
            "pokecardexSets": len(set_map),
            "mappedToTcgdexZhCn": mapped_sets,
            "enBridgeOnly": bridge_only,
            "unmatchedSets": unmatched_sets,
            "methods": dict(Counter(r.get("matchMethod") for r in set_map)),
        },
        "cardMatchStatusCounts": dict(match_counts),
        "standardRows": std_total,
        "standardRowsInMappedTcgdexSet": std_in_mapped,
        "genuinelyUnmappedStandardCandidates": additive_std,
        "genuinelyUnmappedNonStandardCandidates": additive_variant,
        "interpretation": (
            "Rows in a mapped TCGdex zh-cn set are treated as known Chinese printings "
            "(often missing from the CardScanR catalogue). Genuinely unmapped candidates "
            "lack both a mapped TCGdex zh-cn set and a catalogue soft-match — these need "
            "human/visual review before calling them new printings. Non-standard version "
            "rows (reverse / masterball / special) are variants of the same collector "
            "number, not automatically additive printings."
        ),
        "samplesAdditive": samples_additive,
        "samplesMatched": samples_matched,
        "setMap": set_map,
        "cardRowsPath": str(REPORT / "pokecardex_tcgdex_card_crosswalk.jsonl"),
        "noteOnTcgdexCardsApi": (
            "TCGdex zh-cn set endpoints currently return cardCount but empty cards[]. "
            "Card IDs are therefore synthesized as {setId}-{normalizedCollector} for "
            "provenance linking; visual/OCR validation remains required."
        ),
    }, card_rows_out


def write_markdown(summary: dict[str, Any]) -> None:
    sc = summary["setCrosswalk"]
    lines = [
        "# PokéCardex ↔ TCGdex crosswalk",
        "",
        f"Generated: {summary['generatedAtUtc']}",
        "",
        "## Set crosswalk",
        "",
        f"- PokéCardex CHN sets: **{sc['pokecardexSets']}**",
        f"- Mapped to TCGdex zh-cn set id: **{sc['mappedToTcgdexZhCn']}**",
        f"- EN name bridge only: **{sc['enBridgeOnly']}**",
        f"- Unmatched sets: **{sc['unmatchedSets']}**",
        f"- Methods: `{sc['methods']}`",
        "",
        "## Card / version reconcile (all metadata rows)",
        "",
        f"- Metadata rows: **{summary['metadataRowsTotal']}**",
        f"- Version kinds: `{summary['versionKindCounts']}`",
        f"- Standard rows: **{summary['standardRows']}**",
        f"- Standard rows inside mapped TCGdex sets: **{summary['standardRowsInMappedTcgdexSet']}**",
        f"- Genuinely unmapped standard candidates: **{summary['genuinelyUnmappedStandardCandidates']}**",
        f"- Genuinely unmapped non-standard candidates: **{summary['genuinelyUnmappedNonStandardCandidates']}**",
        f"- Status counts: `{summary['cardMatchStatusCounts']}`",
        "",
        "## Interpretation",
        "",
        summary["interpretation"],
        "",
        summary["noteOnTcgdexCardsApi"],
        "",
    ]
    (REPORT / "POKECARDEX_TCGDEX_CROSSWALK.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-card-jsonl", action="store_true", default=True)
    args = parser.parse_args()
    REPORT.mkdir(parents=True, exist_ok=True)

    discovered = load_json(REPORT / "chn_sets_discovered.json")
    pc_sets = discovered.get("sets") or []
    tcgdex = ensure_tcgdex_sets()
    set_map = match_sets(pc_sets, tcgdex)
    meta = load_metadata()
    catalogue = load_catalogue_zh()
    summary, card_rows = card_crosswalk(meta, set_map, catalogue)

    (REPORT / "pokecardex_tcgdex_set_crosswalk.json").write_text(
        json.dumps({"generatedAtUtc": utc_now(), "sets": set_map}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    # Slim summary without full setMap duplication in nested form for the main JSON
    slim = {k: v for k, v in summary.items() if k != "setMap"}
    slim["setMapPath"] = str(REPORT / "pokecardex_tcgdex_set_crosswalk.json")
    (REPORT / "pokecardex_tcgdex_crosswalk_summary.json").write_text(
        json.dumps(slim, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if args.write_card_jsonl:
        out = REPORT / "pokecardex_tcgdex_card_crosswalk.jsonl"
        with out.open("w", encoding="utf-8", newline="\n") as handle:
            for row in card_rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    write_markdown(summary)
    print(json.dumps(slim, indent=2, ensure_ascii=False)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
