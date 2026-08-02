"""Import the official Japanese Pokémon TCG product checkpoint."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from .schema import connect
from .tcgdex import canonical_json, digest, stable_id

PROVIDER_ID = "pokemon-japan-products-official"


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def release_date(value: str | None) -> str | None:
    match = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", value or "")
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}" if match else None


def price_jpy(value: str | None) -> float | None:
    match = re.search(r"([\d,]+)円", value or "")
    return float(match.group(1).replace(",", "")) if match else None


def product_type(official_type: str | None, name: str) -> str:
    if official_type == "拡張パック":
        return "booster_pack"
    if official_type == "構築デッキ":
        return "starter_deck" if "スターター" in name else "constructed_deck"
    if official_type == "周辺グッズ":
        return "accessory_product"
    for token, result in (
        ("ブースターボックス", "booster_box"), ("ボックス", "collection_box"),
        ("スペシャルセット", "special_collection"), ("スターターセット", "starter_deck"),
        ("デッキ", "constructed_deck"), ("パック", "booster_pack"), ("缶", "tin"),
    ):
        if token in name:
            return result
    return "official_product"


def accessory_type(name: str) -> str | None:
    for tokens, result in (
        (("コレクションファイル", "カードファイル", "リフィル"), "binder"),
        (("デッキシールド", "スリーブ"), "sleeves"), (("デッキケース",), "deck_box"),
        (("プレイマット",), "playmat"), (("コイン",), "coin"), (("ダメカン",), "marker"),
        (("カードボックス", "キャリングケース"), "storage"), (("ディスプレイフレーム",), "other"),
    ):
        if any(token in name for token in tokens):
            return result
    return None


def plain_description(value: str | None) -> str | None:
    if not value:
        return None
    return "\n".join(
        line.strip() for line in BeautifulSoup(value, "html.parser").get_text("\n").splitlines() if line.strip()
    )


def import_checkpoint(database: Path, checkpoint_path: Path) -> dict[str, int]:
    checkpoint = sqlite3.connect(f"file:{checkpoint_path.resolve()}?mode=ro", uri=True)
    checkpoint.row_factory = sqlite3.Row
    if checkpoint.execute("select count(*) from runs where status='running'").fetchone()[0]:
        checkpoint.close()
        raise RuntimeError("Japanese product checkpoint still has a running collector")
    snapshot_sha = file_sha256(checkpoint_path)
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    snapshot_id = stable_id(PROVIDER_ID, "snapshot", snapshot_sha[:24])
    counters: Counter[str] = Counter()
    connection = connect(str(database))
    try:
        connection.execute(
            """insert into source_provider values (?, 'Pokémon Japan official TCG products', 'official',
            'https://www.pokemon-card.com/products/', 'metadata_only', 'Pokémon Card Game Japan',
            'https://www.pokemon-card.com/terms/', null)
            on conflict(id) do update set rights_status=excluded.rights_status""", (PROVIDER_ID,),
        )
        connection.execute(
            "insert into import_run values (?, ?, 'running', ?, ?, '{}', '{}', ?, null, null)",
            (run_id, PROVIDER_ID, str(checkpoint_path), snapshot_sha, now),
        )
        connection.execute(
            """insert into source_snapshot values (?, ?, ?, ?, ?, null, ?, ?, ?)
            on conflict(id) do update set import_run_id=excluded.import_run_id,fetched_at=excluded.fetched_at""",
            (snapshot_id, PROVIDER_ID, run_id, str(checkpoint_path), snapshot_sha,
             checkpoint_path.stat().st_size, now, str(checkpoint_path.parent / "raw")),
        )
        for row in checkpoint.execute("select * from products order by release_date_text,local_name,provider_record_id"):
            parsed = json.loads(row["parsed_json"])
            source_id = stable_id(PROVIDER_ID, row["provider_record_id"], row["raw_sha256"][:16])
            connection.execute(
                """insert into source_record values (?, ?, ?, ?, 'sealed_product', ?, null, ?, ?, ?, null)
                on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
                (source_id, PROVIDER_ID, run_id, snapshot_id, row["provider_record_id"],
                 f"checkpoint:products/{row['provider_record_id']}", row["raw_sha256"], row["parsed_json"]),
            )
            name = row["local_name"]
            ptype = product_type(row["product_type"], name)
            product_id = stable_id(PROVIDER_ID, "sealed", row["provider_record_id"])
            variant_id = stable_id(product_id, "ja", "JP", "standard")
            description = plain_description(parsed.get("description"))
            gtin_match = re.search(r"/(\d{12,14})\.html", parsed.get("link_pokemonCenter") or "")
            attrs = {
                "official_product_type": row["product_type"], "msrp": price_jpy(row["price_text"]),
                "currency": "JPY", "price_text": row["price_text"], "stores_available": parsed.get("storesAvailable"),
                "card_list_url": parsed.get("link_cardList"), "detail_page_url": parsed.get("link_detailPage"),
                "pokemon_center_url": parsed.get("link_pokemonCenter"),
                "gtin": gtin_match.group(1) if gtin_match else None,
            }
            connection.execute(
                """insert into sealed_product values (?, ?, ?, ?, ?, ?, 'verified', ?)
                on conflict(id) do update set source_record_id=excluded.source_record_id,
                canonical_name=excluded.canonical_name,product_type=excluded.product_type,
                verification_status=excluded.verification_status,raw_product_json=excluded.raw_product_json""",
                (product_id, PROVIDER_ID, row["provider_record_id"], source_id, name, ptype, row["parsed_json"]),
            )
            connection.execute(
                """insert into sealed_product_variant values (?, ?, 'ja', 'JP', ?, 'standard', ?, ?)
                on conflict(id) do update set release_date=excluded.release_date,attributes_json=excluded.attributes_json""",
                (variant_id, product_id, name, release_date(row["release_date_text"]), canonical_json(attrs)),
            )
            for ordinal, line in enumerate((description or "").splitlines()):
                connection.execute(
                    "insert or replace into product_content values (?, ?, 'other', null, ?, 1, '{}')",
                    (variant_id, ordinal, line),
                )
                counters["product_contents"] += 1
            if row["image_url"]:
                image_id = stable_id(variant_id, PROVIDER_ID, "display", digest(row["image_url"])[:16])
                connection.execute(
                    "insert or replace into product_image_candidate values (?, ?, ?, ?, 'display', ?, 'link_only', 'candidate', ?)",
                    (image_id, variant_id, source_id, PROVIDER_ID, row["image_url"],
                     canonical_json({"role_from_source": "thumbnail"})),
                )
                counters["product_images"] += 1
            atype = accessory_type(name)
            if atype:
                accessory_id = stable_id(PROVIDER_ID, "accessory", row["provider_record_id"])
                connection.execute(
                    "insert or replace into accessory values (?, ?, ?, ?, ?, ?, ?, 'verified')",
                    (accessory_id, PROVIDER_ID, row["provider_record_id"], source_id, name, atype, description),
                )
                counters["accessories"] += 1
            connection.execute(
                "insert or replace into provider_entity_mapping values (?, 'sealed_product', ?, 'sealed_product', ?, 'direct_official_record', 'verified', ?, '{}')",
                (PROVIDER_ID, row["provider_record_id"], product_id, source_id),
            )
            counters["products"] += 1
        connection.execute(
            "update import_run set status='completed',counters_json=?,checkpoint_json=?,completed_at=? where id=?",
            (canonical_json(dict(counters)), canonical_json({"complete": True}),
             datetime.now(timezone.utc).isoformat(), run_id),
        )
        connection.commit()
        return dict(counters)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
        checkpoint.close()
