#!/usr/bin/env python3
"""Acquire Korean catalogue metadata from TCGdex (metadata only; no image downloads).

Reconciles against existing 239 KO catalogue rows before proposing new records.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_global_catalogue.contracts import build_printing_record  # noqa: E402

REPORT = ROOT / "reports" / "global_image_acquisition"
OUT = REPORT / "korean"
LIVE = Path(r"D:\CardScanR_Data\cardscanr-data")
UA = "CardScanR-CatalogueSync/1.0 (+ko-metadata-only; no-image-download)"
CACHE = OUT / "tcgdex_cache"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read())


def load_catalogue_ko() -> list[dict[str, Any]]:
    rows = []
    path = LIVE / "data" / "global" / "catalogue" / "cards.jsonl"
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get("language") or "") == "ko":
                rows.append(row)
    return rows


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    sets_path = CACHE / "sets_ko.json"
    if sets_path.exists():
        sets = json.loads(sets_path.read_text(encoding="utf-8"))
    else:
        sets = get_json("https://api.tcgdex.net/v2/ko/sets")
        sets_path.write_text(json.dumps(sets, ensure_ascii=False), encoding="utf-8")

    catalogue = load_catalogue_ko()
    cat_by_provider: dict[str, dict[str, Any]] = {}
    cat_by_set_num: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in catalogue:
        pid = str((row.get("providerCardIds") or {}).get("tcgdex") or "")
        if pid:
            cat_by_provider[pid] = row
        sid = str((row.get("providerSetIds") or {}).get("tcgdex") or "")
        num = str(row.get("normalizedCollectorNumber") or row.get("printedCollectorNumber") or "")
        if sid:
            cat_by_set_num[(sid.casefold(), num)].append(row)

    proposed: list[dict[str, Any]] = []
    existing_hits = 0
    set_stats = []
    # Sample / bounded fetch: process all sets but cards arrays may be empty on list endpoint;
    # fetch per-set detail.
    for s in sets:
        sid = str(s.get("id") or "")
        if not sid:
            continue
        detail_path = CACHE / f"set_{sid}.json"
        if detail_path.exists():
            detail = json.loads(detail_path.read_text(encoding="utf-8"))
        else:
            try:
                detail = get_json(f"https://api.tcgdex.net/v2/ko/sets/{sid}")
            except Exception as exc:
                detail = {"id": sid, "error": str(exc), "cards": []}
            detail_path.write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")
        cards = list(detail.get("cards") or [])
        # If cards empty but cardCount present, record gap — cannot invent card rows
        cc = detail.get("cardCount") or {}
        official = cc.get("official") if isinstance(cc, dict) else cc
        set_stats.append(
            {
                "id": sid,
                "name": detail.get("name") or s.get("name"),
                "cardsReturned": len(cards),
                "officialCount": official,
                "releaseDate": detail.get("releaseDate") or s.get("releaseDate"),
            }
        )
        serie = detail.get("serie") if isinstance(detail.get("serie"), dict) else {}
        for card in cards:
            provider_card_id = str(card.get("id") or "")
            local_id = str(card.get("localId") or "")
            if not provider_card_id or not local_id:
                continue
            if provider_card_id in cat_by_provider:
                existing_hits += 1
                continue
            if cat_by_set_num.get((sid.casefold(), str(int(local_id)) if local_id.isdigit() else local_id)):
                existing_hits += 1
                continue
            # Do not pass image URL — metadata only package
            rec = build_printing_record(
                source_language="ko",
                provider="tcgdex",
                provider_set_id=sid,
                provider_card_id=provider_card_id,
                native_set_name=str(detail.get("name") or sid),
                native_card_name=str(card.get("name") or local_id),
                collector_number=local_id,
                official_set_total=int(official) if str(official).isdigit() else None,
                release_date=detail.get("releaseDate"),
                image_url=None,
                serie_id=str(serie.get("id") or "") or None,
                serie_name=str(serie.get("name") or "") or None,
                variant="unspecified",
            )
            rec["artworkStatus"] = "not_acquired_metadata_only"
            rec["productionPromotionAllowed"] = False
            rec["integrationSource"] = "tcgdex_ko_metadata_isolated"
            proposed.append(rec)

    with (OUT / "ko_tcgdex_metadata_candidates.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in proposed:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (OUT / "ko_set_fetch_stats.json").write_text(
        json.dumps({"generatedAtUtc": utc_now(), "sets": set_stats}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    cards_returned = sum(s["cardsReturned"] for s in set_stats)
    official_sum = sum(int(s["officialCount"] or 0) for s in set_stats if s.get("officialCount") is not None)
    summary = {
        "generatedAtUtc": utc_now(),
        "imagesDownloaded": 0,
        "catalogueKoExisting": len(catalogue),
        "tcgdexKoSets": len(sets),
        "tcgdexCardsReturnedInSetDetails": cards_returned,
        "tcgdexOfficialCountSum": official_sum,
        "existingReconciledHits": existing_hits,
        "newMetadataCandidates": len(proposed),
        "apiLimitation": (
            "Many TCGdex ko set detail payloads return cardCount but empty cards[]. "
            "Candidates are emitted only for concrete card objects returned by the API. "
            "Official-count sum remains the coverage proxy for remaining gap."
        ),
        "nextSteps": [
            "When TCGdex exposes ko card lists, re-run to fill candidates.",
            "Classify image hosts before any KO image staging.",
            "Seek written permission for non-API image sources.",
        ],
    }
    (OUT / "ko_metadata_acquire_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (OUT / "KOREAN_METADATA_ACQUIRE.md").write_text(
        "\n".join(
            [
                "# Korean TCGdex metadata acquisition",
                "",
                f"Generated: {summary['generatedAtUtc']}",
                "",
                f"- Existing catalogue KO: **{len(catalogue)}**",
                f"- TCGdex sets: **{len(sets)}**",
                f"- Cards returned in set details: **{cards_returned}**",
                f"- Official count sum (proxy): **{official_sum}**",
                f"- Reconciled existing hits: **{existing_hits}**",
                f"- New metadata candidates: **{len(proposed)}**",
                f"- Images downloaded: **0**",
                "",
                summary["apiLimitation"],
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
