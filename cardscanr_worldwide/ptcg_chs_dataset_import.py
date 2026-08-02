"""Import the detailed Simplified-Chinese research dataset with its usage limits preserved."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import connect
from .tcgdex import canonical_json, digest, stable_id

PROVIDER_ID = "ptcg-chs-datasets"
RAW_BASE = "https://raw.githubusercontent.com/duanxr/PTCG-CHS-Datasets"


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def image_url(commit: str, path: str | None) -> str | None:
    if not path:
        return None
    normalized = path.replace("\\", "/").lstrip("/")
    return f"{RAW_BASE}/{commit}/{normalized}"


def dict_map(payload: dict[str, Any], name: str) -> dict[str, str]:
    return {str(row["dictCode"]): row["dictValue"] for row in payload["dict"].get(name, [])}


def product_type(goods_type: str | None, name: str) -> str:
    if goods_type == "1":
        return "booster_pack"
    if goods_type == "2":
        return "constructed_deck"
    if "礼盒" in name:
        return "collection_box"
    if "奖赏包" in name or "促销包" in name:
        return "promotional_pack"
    return "official_card_product"


def collector_parts(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    match = re.match(r"\s*([^/]+?)\s*/\s*([^/]+?)\s*$", value)
    return (match.group(1), match.group(2)) if match else (value.strip(), None)


def import_dataset(database: Path, source_json: Path, source_commit: str) -> dict[str, int]:
    payload = json.loads(source_json.read_text(encoding="utf-8"))
    snapshot_sha = file_sha256(source_json)
    run_id = str(uuid.uuid4())
    snapshot_id = stable_id(PROVIDER_ID, "snapshot", source_commit[:12], snapshot_sha[:16])
    now = datetime.now(timezone.utc).isoformat()
    counters: Counter[str] = Counter()
    type_map = dict_map(payload, "attribute")
    cost_map = dict_map(payload, "ability_cost")
    weakness_map = dict_map(payload, "weakness_type")
    resistance_map = dict_map(payload, "resistance_type")
    connection = connect(str(database))
    try:
        connection.execute(
            """insert into source_provider values (?, 'PTCG-CHS-Datasets detailed Simplified Chinese snapshot',
            'community_dataset', 'https://github.com/duanxr/PTCG-CHS-Datasets',
            'noncommercial_no_redistribution', 'PTCG-CHS-Datasets; source attributes rights to official authorities',
            'https://github.com/duanxr/PTCG-CHS-Datasets#disclaimer-for-usage-of-ptcg-chs-datasets', null)
            on conflict(id) do update set rights_status=excluded.rights_status,terms_url=excluded.terms_url""",
            (PROVIDER_ID,),
        )
        connection.execute(
            "insert into import_run values (?, ?, 'running', ?, ?, '{}', '{}', ?, null, null)",
            (run_id, PROVIDER_ID, str(source_json), snapshot_sha, now),
        )
        connection.execute(
            """insert into source_snapshot values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set import_run_id=excluded.import_run_id,fetched_at=excluded.fetched_at""",
            (snapshot_id, PROVIDER_ID, run_id, str(source_json), snapshot_sha, source_commit,
             source_json.stat().st_size, now, str(source_json.parent)),
        )
        series_ids: dict[str, str] = {}
        for collection in payload["collections"]:
            collection_key = str(collection["id"])
            collection_raw = canonical_json({key: value for key, value in collection.items() if key != "cards"})
            collection_source_id = stable_id(PROVIDER_ID, "collection", collection_key, digest(collection_raw)[:16])
            connection.execute(
                """insert into source_record values (?, ?, ?, ?, 'collection', ?, null, ?, ?, ?, null)
                on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
                (collection_source_id, PROVIDER_ID, run_id, snapshot_id, collection_key,
                 f"json:collections/{collection_key}", digest(collection_raw), collection_raw),
            )
            series_name = collection.get("seriesText") or "其他"
            series_id = series_ids.setdefault(series_name, stable_id(PROVIDER_ID, "series", series_name))
            connection.execute(
                """insert into series values (?, ?, ?, ?, ?, ?)
                on conflict(id) do update set source_record_id=excluded.source_record_id""",
                (series_id, PROVIDER_ID, str(collection.get("series") or "other"), collection_source_id,
                 series_name, canonical_json({"zh-cn": series_name})),
            )
            set_id = stable_id(PROVIDER_ID, "set", collection_key)
            release_id = stable_id(set_id, "zh-cn", "CN")
            collection_name = collection["name"]
            cards = collection.get("cards") or []
            connection.execute(
                """insert into card_set values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set source_record_id=excluded.source_record_id,official_count=excluded.official_count""",
                (set_id, series_id, PROVIDER_ID, collection.get("commodityCode") or collection_key,
                 collection_source_id, collection_name, canonical_json({"zh-cn": collection_name}),
                 "promo" if collection.get("goodsType") == "3" else "main", len(cards),
                 canonical_json(collection.get("salesDate")), canonical_json({"collection_id": collection["id"]})),
            )
            connection.execute(
                """insert into set_release values (?, ?, 'zh-cn', 'CN', ?, ?, ?, ?, 'provisional', ?)
                on conflict(id) do update set local_name=excluded.local_name,official_count=excluded.official_count,
                source_record_id=excluded.source_record_id""",
                (release_id, set_id, collection_name, collection.get("commodityCode") or collection_key,
                 collection.get("salesDate"), len(cards), collection_source_id),
            )
            product_id = stable_id(PROVIDER_ID, "sealed", collection_key)
            product_variant_id = stable_id(product_id, "zh-cn", "CN", "standard")
            connection.execute(
                """insert into sealed_product values (?, ?, ?, ?, ?, ?, 'provisional', ?)
                on conflict(id) do update set source_record_id=excluded.source_record_id,raw_product_json=excluded.raw_product_json""",
                (product_id, PROVIDER_ID, collection_key, collection_source_id, collection_name,
                 product_type(collection.get("goodsType"), collection_name), collection_raw),
            )
            connection.execute(
                """insert into sealed_product_variant values (?, ?, 'zh-cn', 'CN', ?, 'standard', ?, ?)
                on conflict(id) do update set release_date=excluded.release_date,attributes_json=excluded.attributes_json""",
                (product_variant_id, product_id, collection_name, collection.get("salesDate"), canonical_json({
                    "commodity_code": collection.get("commodityCode"), "goods_type": collection.get("goodsType"),
                    "series": series_name, "rights_status": "noncommercial_no_redistribution",
                })),
            )
            cover_url = image_url(source_commit, collection.get("image"))
            if cover_url:
                product_image_id = stable_id(product_variant_id, PROVIDER_ID, "display", digest(cover_url)[:16])
                connection.execute(
                    "insert or replace into product_image_candidate values (?, ?, ?, ?, 'display', ?, 'rights_blocked', 'blocked', ?)",
                    (product_image_id, product_variant_id, collection_source_id, PROVIDER_ID, cover_url,
                     canonical_json({"dataset_path": collection.get("image")})),
                )
                counters["product_images"] += 1
            for ordinal, card in enumerate(cards):
                details = card.get("details") or {}
                source_record_key = f"{collection_key}:{card['id']}:{card.get('hash') or ordinal}"
                card_raw = canonical_json(card)
                source_id = stable_id(PROVIDER_ID, "card", source_record_key, digest(card_raw)[:16])
                connection.execute(
                    """insert into source_record values (?, ?, ?, ?, 'card', ?, ?, ?, ?, ?, null)
                    on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
                    (source_id, PROVIDER_ID, run_id, snapshot_id, source_record_key, collection_key,
                     f"json:collections/{collection_key}/cards/{ordinal}", digest(card_raw), card_raw),
                )
                design_id = stable_id(PROVIDER_ID, "design", card["id"])
                printing_id = stable_id(PROVIDER_ID, "printing", source_record_key, "zh-cn", "CN")
                variant_id = stable_id(printing_id, "depiction-unspecified")
                number, printed_total = collector_parts(details.get("collectionNumber"))
                dex = []
                if str(details.get("pokedexCode") or "").isdigit():
                    dex = [int(details["pokedexCode"])]
                connection.execute(
                    """insert into card_design values (?, 'other', ?, ?, ?)
                    on conflict(id) do update set canonical_name=excluded.canonical_name,
                    national_pokedex_numbers_json=excluded.national_pokedex_numbers_json""",
                    (design_id, details.get("cardName") or card["name"], canonical_json(dex),
                     stable_id(PROVIDER_ID, "card-id", card["id"])),
                )
                card_types = [type_map[str(details["attribute"])]] if str(details.get("attribute")) in type_map else []
                weaknesses = []
                if str(details.get("weaknessType")) in weakness_map:
                    weaknesses.append({"type": weakness_map[str(details["weaknessType"])],
                                       "value": details.get("weaknessFormula")})
                resistances = []
                if str(details.get("resistanceType")) in resistance_map:
                    resistances.append({"type": resistance_map[str(details["resistanceType"])],
                                        "value": details.get("resistanceFormula")})
                retreat = ["无色"] * int(details.get("retreatCost") or 0)
                connection.execute(
                    """insert into card_printing values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'provisional', ?)
                    on conflict(id) do update set source_record_id=excluded.source_record_id,raw_card_json=excluded.raw_card_json""",
                    (printing_id, design_id, release_id, source_id, number or str(card["id"]), source_record_key,
                     details.get("rarityText"), (details.get("illustratorName") or [None])[0],
                     details.get("cardTypeText"), details.get("evolveText"), details.get("hp"),
                     canonical_json(card_types), details.get("regulationMarkText"), canonical_json(retreat),
                     canonical_json(weaknesses), canonical_json(resistances), card_raw),
                )
                connection.execute(
                    "insert or replace into card_variant values (?, ?, 'depiction-unspecified', null, null, null, null, 0, ?, 'unknown')",
                    (variant_id, printing_id, canonical_json({
                        "collection_id": collection["id"], "source_card_id": card["id"],
                        "source_hash": card.get("hash"), "printed_total": printed_total,
                    })),
                )
                rules = [part for part in str(details.get("ruleText") or "").split("|") if part]
                connection.execute(
                    "insert or replace into card_localisation values (?, 'zh-cn', ?, ?, ?, 'official_source_research_only')",
                    (printing_id, details.get("cardName") or card["name"], details.get("pokedexText"), canonical_json(rules)),
                )
                for attack_ordinal, attack in enumerate(details.get("abilityItemList") or []):
                    costs = [cost_map.get(code, code) for code in str(attack.get("abilityCost") or "").split(",") if code]
                    connection.execute(
                        "insert or replace into attack values (?, ?, 'zh-cn', ?, ?, ?, ?)",
                        (printing_id, attack_ordinal, attack.get("abilityName") or f"attack-{attack_ordinal}",
                         canonical_json(costs), None if attack.get("abilityDamage") == "none" else attack.get("abilityDamage"),
                         None if attack.get("abilityText") == "none" else attack.get("abilityText")),
                    )
                for ability_ordinal, ability in enumerate(details.get("cardFeatureItemList") or []):
                    connection.execute(
                        "insert or replace into ability values (?, ?, 'zh-cn', ?, 'ability', ?)",
                        (printing_id, ability_ordinal, ability.get("featureName") or f"ability-{ability_ordinal}",
                         ability.get("featureDesc")),
                    )
                card_url = image_url(source_commit, card.get("image"))
                if card_url:
                    image_id = stable_id(variant_id, PROVIDER_ID, "display", digest(card_url)[:16])
                    connection.execute(
                        "insert or replace into card_image_candidate values (?, ?, ?, ?, 'display', ?, 'rights_blocked', 'blocked')",
                        (image_id, variant_id, source_id, PROVIDER_ID, card_url),
                    )
                    counters["card_images"] += 1
                connection.execute(
                    "insert or replace into product_content values (?, ?, 'card_variant', ?, ?, 1, ?)",
                    (product_variant_id, ordinal, variant_id, details.get("cardName") or card["name"],
                     canonical_json({"source_card_id": card["id"]})),
                )
                connection.execute(
                    "insert or replace into provider_entity_mapping values (?, 'card', ?, 'card_printing', ?, 'direct_dataset_record', 'candidate', ?, '{}')",
                    (PROVIDER_ID, source_record_key, printing_id, source_id),
                )
                counters["card_printings"] += 1
            connection.execute(
                "insert or replace into provider_entity_mapping values (?, 'collection', ?, 'sealed_product', ?, 'direct_dataset_record', 'candidate', ?, '{}')",
                (PROVIDER_ID, collection_key, product_id, collection_source_id),
            )
            counters["collections"] += 1
        blocker_id = stable_id(PROVIDER_ID, "rights", snapshot_sha[:16])
        connection.execute(
            """insert or replace into unresolved_item values (?, 'source_provider', ?, 'zh-cn', 'CN',
            'commercial_redistribution_rights_required', ?, ?, 'blocked_external', 1)""",
            (blocker_id, PROVIDER_ID,
             "Dataset terms restrict use to non-commercial research and prohibit redistribution without written consent",
             canonical_json({
                 "terms_url": "https://github.com/duanxr/PTCG-CHS-Datasets#disclaimer-for-usage-of-ptcg-chs-datasets",
                 "affected_card_printings": counters["card_printings"], "affected_collections": counters["collections"],
                 "required_action": "Obtain written permission from Pokémon Shanghai or the authorized rights holder",
             })),
        )
        connection.execute(
            "update import_run set status='completed',counters_json=?,checkpoint_json=?,completed_at=? where id=?",
            (canonical_json(dict(counters)), canonical_json({"complete": True, "source_commit": source_commit,
                                                             "publication_allowed": False}),
             datetime.now(timezone.utc).isoformat(), run_id),
        )
        connection.commit()
        return dict(counters)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

