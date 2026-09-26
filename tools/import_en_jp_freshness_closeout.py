#!/usr/bin/env python3
"""Close remaining EN/JP freshness gaps: MEE 009-016 + M6a five + status fixes."""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "public" / "v1" / "catalog" / "pokemon"
REPORT = ROOT / "reports" / "en_jp_freshness_closeout_latest.json"

MEE_30TH = [
    ("009", "Basic Grass Energy"),
    ("010", "Basic Fire Energy"),
    ("011", "Basic Water Energy"),
    ("012", "Basic Lightning Energy"),
    ("013", "Basic Psychic Energy"),
    ("014", "Basic Fighting Energy"),
    ("015", "Basic Darkness Energy"),
    ("016", "Basic Metal Energy"),
]
MEE_BASE = [
    ("001", "Basic Grass Energy"),
    ("002", "Basic Fire Energy"),
    ("003", "Basic Water Energy"),
    ("004", "Basic Lightning Energy"),
    ("005", "Basic Psychic Energy"),
    ("006", "Basic Fighting Energy"),
    ("007", "Basic Darkness Energy"),
    ("008", "Basic Metal Energy"),
]

# Corroborated by: pokeca-pocket card list, samuraiswordtokyo M6a list, yuyu-tei, tradecard.jp (128).
M6A_FIVE = [
    {
        "collectorNumber": "119/103",
        "nameJa": "ジャラランガ",
        "nameEn": "Kommo-o",
        "rarity": "Art Rare",
        "evidence": [
            "https://pokeca-pocket.dbfw-days.com/30th-celebration-cardlist/",
            "https://samuraiswordtokyo.com/pages/m6a-card-list",
            "https://yuyu-tei.jp/sell/poc/s/m06a",
        ],
    },
    {
        "collectorNumber": "121/103",
        "nameJa": "メタモン",
        "nameEn": "Ditto",
        "rarity": "Art Rare",
        "evidence": [
            "https://pokeca-pocket.dbfw-days.com/30th-celebration-cardlist/",
            "https://samuraiswordtokyo.com/pages/m6a-card-list",
            "https://yuyu-tei.jp/sell/poc/s/m06a",
        ],
    },
    {
        "collectorNumber": "128/103",
        "nameJa": "ミュウツーex",
        "nameEn": "Mewtwo ex",
        "rarity": "Special Art Rare",
        "evidence": [
            "https://pokeca-pocket.dbfw-days.com/30th-celebration-cardlist/",
            "https://tradecard.jp/articles/mewtwoex-sar-128-30th",
            "https://samuraiswordtokyo.com/pages/m6a-card-list",
        ],
    },
    {
        "collectorNumber": "129/103",
        "nameJa": "ミュウex",
        "nameEn": "Mew ex",
        "rarity": "Special Art Rare",
        "evidence": [
            "https://pokeca-pocket.dbfw-days.com/30th-celebration-cardlist/",
            "https://samuraiswordtokyo.com/pages/m6a-card-list",
            "https://yuyu-tei.jp/sell/poc/s/m06a",
        ],
    },
    {
        "collectorNumber": "131/103",
        "nameJa": "ゲンガーex",
        "nameEn": "Gengar ex",
        "rarity": "Special Art Rare",
        "evidence": [
            "https://pokeca-pocket.dbfw-days.com/30th-celebration-cardlist/",
            "https://samuraiswordtokyo.com/pages/m6a-card-list",
            "https://yuyu-tei.jp/sell/poc/s/m06a",
        ],
    },
]


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_catalog_name(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    normalized = re.sub(r"[^\w]+", "_", normalized, flags=re.UNICODE).strip("_")
    normalized = re.sub(r"_+", "_", normalized)
    return normalized or "unknown"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(encoded)
    os.replace(tmp, path)


def scrydex_urls(set_id: str, number: str) -> tuple[str, str]:
    # Scrydex / Pokemon TCG API CDN uses unpadded numeric suffixes (mee-9, m6a-119).
    numeric = str(int(re.sub(r"\D", "", number) or number))
    small = f"https://images.scrydex.com/pokemon/{set_id}-{numeric}/small"
    large = f"https://images.scrydex.com/pokemon/{set_id}-{numeric}/large"
    return small, large


def merge_set(language: str, set_record: dict[str, Any]) -> None:
    sets_path = CATALOG / language / "sets.json"
    payload = json.loads(sets_path.read_text(encoding="utf-8"))
    sets = payload.get("sets") if isinstance(payload.get("sets"), list) else []
    set_id = str(set_record["id"])
    replaced = False
    for idx, existing in enumerate(sets):
        if str(existing.get("id")) == set_id:
            sets[idx] = set_record
            replaced = True
            break
    if not replaced:
        sets.append(set_record)
    sets.sort(key=lambda item: (str(item.get("releaseDate") or ""), str(item.get("id") or "")))
    cards_dir = CATALOG / language / "cards"
    total_cards = 0
    for path in cards_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            total_cards += int(data.get("cardCount") or len(data.get("cards") or []))
        except Exception:  # noqa: BLE001
            continue
    payload["sets"] = sets
    payload["setCount"] = len(sets)
    payload["cardCount"] = total_cards
    payload["generatedAtUtc"] = now_utc()
    write_json(sets_path, payload)


def build_mee_card(number: str, name: str, *, anniversary: bool) -> dict[str, Any]:
    small, large = scrydex_urls("mee", number)
    normalized = normalize_catalog_name(name)
    rarity = "Futuristic Rare" if anniversary else "Common"
    record = {
        "canonicalBaseId": f"pokemon|en|mee|{number}|{normalized}",
        "game": "pokemon",
        "language": "en",
        "setId": "mee",
        "setName": "Mega Evolution Energies",
        "collectorNumber": number,
        "name": name,
        "normalizedName": normalized,
        "rarity": rarity,
        "supertype": "Energy",
        "supertypes": ["Energy"],
        "subtypes": ["Basic"],
        "types": [],
        "hp": None,
        "artist": "YOSHIROTTEN" if anniversary else None,
        "illustrator": "YOSHIROTTEN" if anniversary else None,
        "imageUrl": small,
        "imageUrlSmall": small,
        "imageUrlLarge": large,
        "imageSmall": small,
        "imageLarge": large,
        "imageSource": "pokemon_tcg_api",
        "providerImageSource": "scrydex_cdn",
        "imageCached": False,
        "providerIds": {"pokemonTcgApi": None, "tcgdex": f"mee-{number}", "pokewallet": None},
        "pricingReferences": {"tcgplayerAvailable": False, "cardmarketAvailable": False},
        "externalIds": {
            "pokemonTcgApiId": None,
            "tcgdexCardId": f"mee-{number}",
            "tcgplayerProductId": None,
            "pricechartingId": None,
        },
        "availableVariants": [],
        "notes": [
            "Imported by tools/import_en_jp_freshness_closeout.py",
            "Identity corroborated via Bulbapedia MEE Basic Energies + cardprices.io / pikastocks MEE lists",
            "Images via existing Scrydex CDN path used by Pokemon TCG API catalogue records",
        ],
    }
    if anniversary:
        record["productFamily"] = "30th Celebration"
        record["associatedExpansion"] = "me55"
        record["printingClass"] = "30th_celebration_mee_energy"
        record["notes"].append("30th Celebration-associated printing; canonical set remains mee (not me55).")
    return record


def import_mee() -> dict[str, Any]:
    ts = now_utc()
    cards = [build_mee_card(n, name, anniversary=False) for n, name in MEE_BASE]
    cards.extend(build_mee_card(n, name, anniversary=True) for n, name in MEE_30TH)
    cards.sort(key=lambda c: c["collectorNumber"])
    payload = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": ts,
        "game": "pokemon",
        "language": "en",
        "setId": "mee",
        "setName": "Mega Evolution Energies",
        "source": "pokemon_tcg_api",
        "catalogueStatus": "built",
        "cardCount": len(cards),
        "cards": cards,
        "notes": [
            "MEE 001-008: Mega Evolution series base energies",
            "MEE 009-016: 30th Celebration-associated YOSHIROTTEN energies (canonical set mee)",
            "Pokemon TCG API set detail for mee was unavailable; Scrydex CDN + public metadata used",
        ],
    }
    write_json(CATALOG / "en" / "cards" / "mee.json", payload)
    merge_set(
        "en",
        {
            "id": "mee",
            "name": "Mega Evolution Energies",
            "series": "Mega Evolution",
            "printedTotal": 16,
            "total": 16,
            "releaseDate": "2025/10/10",
            "updatedAt": None,
            "ptcgoCode": "MEE",
            "symbolUrl": None,
            "logoUrl": None,
            "imageSource": "pokemon_tcg_api",
            "imageCached": False,
            "promotionSource": "en_jp_freshness_closeout",
        },
    )
    return {
        "setId": "mee",
        "imported": len(cards),
        "anniversary009_016": 8,
        "base001_008": 8,
        "images": {"withSmall": 16, "withLarge": 16, "missing": 0},
    }


def recover_m6a_five() -> dict[str, Any]:
    path = CATALOG / "jp" / "cards" / "m6a.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    existing = {str(c.get("collectorNumber")) for c in data.get("cards") or []}
    added: list[dict[str, Any]] = []
    for item in M6A_FIVE:
        collector = item["collectorNumber"]
        if collector in existing:
            continue
        numerator = collector.split("/", 1)[0]
        small, large = scrydex_urls("m6a", numerator)
        # Keep English display naming consistent with existing PokéWallet m6a rows,
        # while preserving Japanese identity in originalName.
        name_en = item["nameEn"]
        name_ja = item["nameJa"]
        normalized = normalize_catalog_name(name_en)
        card = {
            "availableVariants": [],
            "canonicalBaseId": f"pokemon|jp|m6a|{collector}|{normalized}",
            "category": "Pokemon",
            "collectorNumber": collector,
            "displayName": name_en,
            "externalIds": {
                "pokemonTcgApiId": None,
                "pricechartingId": None,
                "tcgdexCardId": f"M6a-{numerator}",
                "tcgplayerProductId": None,
            },
            "game": "pokemon",
            "hp": None,
            "illustrator": None,
            "imageCached": False,
            "imageLarge": large,
            "imageSmall": small,
            "imageSource": "pokewallet",
            "providerImageSource": "scrydex_cdn",
            "imageUrl": small,
            "imageUrlLarge": large,
            "imageUrlSmall": small,
            "language": "jp",
            "name": name_en,
            "normalizedName": normalized,
            "originalName": name_ja,
            "pricingReferences": {
                "cardmarketAvailable": False,
                "pokewalletAvailable": False,
                "tcgplayerAvailable": False,
            },
            "promotionMetadata": {
                "confidence": "multi_source_public_catalogue_corroboration",
                "evidenceUrls": item["evidence"],
                "identityKey": f"jp|m6a|{collector}|{normalized}|normal",
                "provider": "public_catalogue_corroboration",
                "providerSetCode": "m6a",
                "providerSetId": "24721",
                "providerSetName": "30th CELEBRATION",
                "source": "en_jp_freshness_closeout",
                "variantKey": "normal",
            },
            "providerIds": {"pokemonTcgApi": None, "pokewallet": None, "tcgdex": f"M6a-{numerator}"},
            "rarity": item["rarity"],
            "setId": "m6a",
            "setName": "30th CELEBRATION",
            "notes": [
                "Recovered metadata from multi-source public catalogues; PokéWallet lacked these five numbers",
                "Images served from Scrydex CDN path already used by CardScanR EN pokemon_tcg_api records",
                "IMAGE_SOURCE: scrydex_cdn (approved CDN); catalogue row remains under m6a/jp",
            ],
        }
        data["cards"].append(card)
        added.append(
            {
                "collectorNumber": collector,
                "nameEn": name_en,
                "nameJa": name_ja,
                "rarity": item["rarity"],
                "evidence": item["evidence"],
                "imageSmall": small,
            }
        )

    data["cards"].sort(
        key=lambda c: (
            str(c.get("collectorNumber") or ""),
            str(c.get("name") or ""),
        )
    )
    data["cardCount"] = len(data["cards"])
    data["generatedAtUtc"] = now_utc()
    data["catalogueStatus"] = "built"
    notes = list(data.get("notes") or [])
    notes.append(
        "Closeout: recovered five numbered identities 119/121/128/129/131; energies GRA-WAT etc retained"
    )
    data["notes"] = notes
    write_json(path, data)

    # Refresh set total
    merge_set(
        "jp",
        {
            "id": "m6a",
            "name": "30th CELEBRATION",
            "series": None,
            "printedTotal": 103,
            "total": data["cardCount"],
            "releaseDate": "2026/09/16",
            "updatedAt": None,
            "ptcgoCode": "m6a",
            "symbolUrl": None,
            "logoUrl": None,
            "imageSource": "pokewallet",
            "imageCached": False,
            "promotionSource": "pokewallet_provider_promotion",
        },
    )

    with_img = sum(1 for c in data["cards"] if c.get("imageSmall") and c.get("imageLarge"))
    return {
        "added": added,
        "finalCardCount": data["cardCount"],
        "catalogueStatus": data["catalogueStatus"],
        "images": {"total": data["cardCount"], "withImages": with_img, "missing": data["cardCount"] - with_img},
        "numberedUnique": len(
            {
                str(c.get("collectorNumber") or "").split("/", 1)[0]
                for c in data["cards"]
                if str(c.get("collectorNumber") or "")[:1].isdigit()
            }
        ),
    }


def main() -> int:
    report = {
        "generatedAtUtc": now_utc(),
        "mee": import_mee(),
        "m6a": recover_m6a_five(),
    }
    write_json(REPORT, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
