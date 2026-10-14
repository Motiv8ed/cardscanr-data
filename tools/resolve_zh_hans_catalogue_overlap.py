#!/usr/bin/env python3
"""Resolve PokéCardex ↔ existing 877 zh-Hans catalogue overlap.

Catalogue zh-Hans uses TCGdex IDs (SV7, SV8a, CSMPiC, …). PokéCardex uses
Chinese-market codes (CSV*C, CS*C, …). Zero set-code overlap is expected and
does NOT imply zero shared cards — match via JA→EN species bridge + collector,
and via pokedex IDs where present.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import crosswalk_pokecardex_tcgdex as xwalk

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition" / "pokecardex"
LIVE = Path(r"D:\CardScanR_Data\cardscanr-data")
META = REPORT / "zh_hans_cards.jsonl"
SPECIES_JA_EN = ROOT / "data" / "pokemon_species_names_ja_en.json"
UA = "CardScanR-CatalogueSync/1.0 (+zh-overlap; rate-limited)"
CACHE = REPORT / "tcgdex_card_cache"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def norm_name(value: object) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"\s*\b(ex|gx|v|vmax|vstar|break)\b", " ", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def base_pokemon_name(english: str) -> str:
    text = str(english or "")
    text = re.sub(r"\s*(ex|EX|gx|GX|VMAX|VSTAR|V|BREAK).*$", "", text)
    text = re.sub(r"'s\s+.*$", "", text)  # drop trainer possessives lightly
    return norm_name(text)


def fetch_set_cards(lang: str, set_id: str) -> list[dict[str, Any]]:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{lang}_{set_id}.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else (data.get("cards") or [])
    url = f"https://api.tcgdex.net/v2/{lang}/sets/{set_id}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        raw = urllib.request.urlopen(req, timeout=45).read()
    except Exception as exc:
        path.write_text(json.dumps({"error": str(exc), "cards": []}), encoding="utf-8")
        return []
    payload = json.loads(raw)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return list(payload.get("cards") or [])


def load_species_ja_en() -> dict[str, str]:
    return dict(json.loads(SPECIES_JA_EN.read_text(encoding="utf-8")).get("names") or {})


def load_catalogue_zh() -> list[dict[str, Any]]:
    rows = []
    path = LIVE / "data" / "global" / "catalogue" / "cards.jsonl"
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get("language") or "") == "zh-Hans":
                rows.append(row)
    return rows


def load_pc_std() -> list[dict[str, Any]]:
    rows = []
    with META.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if int(row.get("providerVersionId") or 1) in {0, 1}:
                rows.append(row)
    return rows


def main() -> int:
    ja_en = load_species_ja_en()
    catalogue = load_catalogue_zh()
    pc = load_pc_std()

    # Index PokéCardex by base english name + collector, and by pokedex+collector
    by_en_num: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_dex_num: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in pc:
        num = xwalk.norm_num(row.get("printedCollectorNumber"))
        en = base_pokemon_name(str(row.get("englishCardName") or ""))
        if en:
            by_en_num[(en, num)].append(row)
        for dex in row.get("pokedexIds") or []:
            if dex is None:
                continue
            by_dex_num[(str(int(dex)), num)].append(row)

    cat_sets = sorted(
        {
            str((row.get("providerSetIds") or {}).get("tcgdex") or "")
            for row in catalogue
            if (row.get("providerSetIds") or {}).get("tcgdex")
        }
    )

    # Prefetch JA/ZH cards for catalogue sets
    ja_by_set_local: dict[str, dict[str, dict[str, Any]]] = {}
    zh_by_set_local: dict[str, dict[str, dict[str, Any]]] = {}
    for sid in cat_sets:
        zh_cards = fetch_set_cards("zh-cn", sid)
        ja_cards = fetch_set_cards("ja", sid)
        zh_by_set_local[sid] = {str(c.get("localId")): c for c in zh_cards}
        ja_by_set_local[sid] = {str(c.get("localId")): c for c in ja_cards}
        # also index zero-padded / int forms
        for c in zh_cards:
            lid = str(c.get("localId") or "")
            try:
                zh_by_set_local[sid][str(int(lid))] = c
            except ValueError:
                pass
        for c in ja_cards:
            lid = str(c.get("localId") or "")
            try:
                ja_by_set_local[sid][str(int(lid))] = c
            except ValueError:
                pass

    matches: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    for row in catalogue:
        sid = str((row.get("providerSetIds") or {}).get("tcgdex") or "")
        num = xwalk.norm_num(row.get("printedCollectorNumber") or row.get("normalizedCollectorNumber"))
        printed = str(row.get("printedCollectorNumber") or "")
        native = str(row.get("nativeCardName") or "")
        cid = str(row.get("canonicalPrintingId") or "")

        zh_card = (zh_by_set_local.get(sid) or {}).get(printed) or (zh_by_set_local.get(sid) or {}).get(num)
        ja_card = (ja_by_set_local.get(sid) or {}).get(printed) or (ja_by_set_local.get(sid) or {}).get(num)
        ja_name = str((ja_card or {}).get("name") or "")
        en_species = ja_en.get(ja_name) or ja_en.get(ja_name.replace("♂", "").replace("♀", ""))
        en_key = base_pokemon_name(en_species or "")

        candidates = []
        method = None
        confidence = 0.0
        if en_key and by_en_num.get((en_key, num)):
            candidates = by_en_num[(en_key, num)]
            method = "ja_species_en_plus_collector"
            confidence = 0.82 if len(candidates) == 1 else 0.65
        elif en_key:
            # same species any collector — weaker parallel evidence
            soft = []
            for (name, _), rows in by_en_num.items():
                if name == en_key:
                    soft.extend(rows)
            if soft:
                candidates = soft[:5]
                method = "ja_species_en_any_collector"
                confidence = 0.45

        if not candidates:
            status = "no_pokecardex_soft_match"
            status_counts[status] += 1
            matches.append(
                {
                    "canonicalPrintingId": cid,
                    "catalogueSetId": sid,
                    "collector": printed,
                    "nativeCardName": native,
                    "jaName": ja_name or None,
                    "enSpecies": en_species,
                    "status": status,
                    "confidence": 0.0,
                }
            )
            continue

        # Prefer candidate whose set is mapped / same series family
        best = candidates[0]
        status = "soft_match_parallel_or_related"
        if confidence >= 0.8 and len(candidates) == 1:
            status = "high_confidence_related_printing"
        status_counts[status] += 1
        matches.append(
            {
                "canonicalPrintingId": cid,
                "catalogueSetId": sid,
                "collector": printed,
                "nativeCardName": native,
                "jaName": ja_name or None,
                "enSpecies": en_species,
                "matchMethod": method,
                "confidence": confidence,
                "status": status,
                "pokecardex": {
                    "providerSetId": best.get("providerSetId"),
                    "providerCardId": best.get("providerCardId"),
                    "setName": best.get("setName"),
                    "englishCardName": best.get("englishCardName"),
                    "printedCollectorNumber": best.get("printedCollectorNumber"),
                },
                "candidateCount": len(candidates),
                "note": (
                    "Catalogue uses TCGdex SV*/CSMPiC IDs; PokéCardex uses Chinese-market codes. "
                    "A soft match means related species/collector evidence — not automatic same canonicalPrintingId."
                ),
            }
        )

    # Set-level code namespace explanation
    pc_sets = sorted({str(r.get("providerSetId")) for r in pc})
    cat_set_list = cat_sets
    exact_code_overlap = sorted(set(s.casefold() for s in pc_sets) & set(s.casefold() for s in cat_set_list))

    summary = {
        "generatedAtUtc": utc_now(),
        "catalogueZhHans": len(catalogue),
        "pokecardexStandard": len(pc),
        "catalogueProviderSetIds": cat_set_list,
        "exactProviderSetCodeOverlap": exact_code_overlap,
        "exactProviderSetCodeOverlapCount": len(exact_code_overlap),
        "anomalyExplanation": (
            "Zero set-code overlap is expected: live catalogue zh-Hans rows use TCGdex IDs "
            f"{cat_set_list}, while PokéCardex CHN shortNames are Chinese-market codes "
            "(CSV*C/CS*C/…). Unmatched provider IDs do not mean physically new cards. "
            "Soft matches use JA species→EN bridge + collector number."
        ),
        "matchStatusCounts": dict(status_counts),
        "highConfidenceRelated": status_counts.get("high_confidence_related_printing", 0),
        "softParallelOrRelated": status_counts.get("soft_match_parallel_or_related", 0),
        "noSoftMatch": status_counts.get("no_pokecardex_soft_match", 0),
        "matchesPath": str(REPORT / "zh_hans_catalogue_overlap_matches.jsonl"),
    }

    with (REPORT / "zh_hans_catalogue_overlap_matches.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in matches:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (REPORT / "zh_hans_catalogue_overlap_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (REPORT / "ZH_HANS_CATALOGUE_OVERLAP.md").write_text(
        "\n".join(
            [
                "# zh-Hans catalogue overlap resolution",
                "",
                f"Generated: {summary['generatedAtUtc']}",
                "",
                summary["anomalyExplanation"],
                "",
                f"- Catalogue zh-Hans: **{len(catalogue)}**",
                f"- Exact set-code overlap: **{len(exact_code_overlap)}**",
                f"- High-confidence related: **{summary['highConfidenceRelated']}**",
                f"- Soft parallel/related: **{summary['softParallelOrRelated']}**",
                f"- No soft match: **{summary['noSoftMatch']}**",
                f"- Status counts: `{dict(status_counts)}`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
