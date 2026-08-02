"""Import a completed official mainland-China product checkpoint."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json, digest, stable_id

PROVIDER_ID = "pokemon-cn-official"


def file_sha256(path: Path) -> str:
    import hashlib
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def classify_product(name: str, contents: str | None) -> str:
    text = f"{name} {contents or ''}"
    if "补充包" in text or "寶石包" in text or "宝石包" in text:
        return "booster_pack"
    if "卡组构筑套装" in text or "卡組構築套裝" in text:
        return "trainer_toolkit"
    if "卡组" in text or "卡組" in text:
        return "starter_deck"
    if "礼盒" in text or "禮盒" in text:
        return "collection_box"
    if "收藏册" in text or "收藏冊" in text:
        return "accessory_collection"
    return "official_product"


def accessory_type(name: str, contents: str | None) -> str | None:
    text = f"{name} {contents or ''}"
    for patterns, value in (
        (("收藏册", "收藏冊", "卡册", "卡冊"), "binder"),
        (("卡套", "牌套"), "sleeves"), (("卡组收纳盒", "卡組收納盒"), "deck_box"),
        (("对战卡垫", "對戰卡墊"), "playmat"), (("硬币", "硬幣"), "coin"),
        (("骰子",), "dice"),
    ):
        if any(pattern in text for pattern in patterns):
            return value
    return None


def parse_msrp(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*元", text)
    return float(match.group(1)) if match else None


def import_checkpoint(database: Path, checkpoint_path: Path) -> dict[str, int]:
    checkpoint = sqlite3.connect(f"file:{checkpoint_path.resolve()}?mode=ro", uri=True)
    checkpoint.row_factory = sqlite3.Row
    if checkpoint.execute("select count(*) from runs where status='running'").fetchone()[0]:
        checkpoint.close()
        raise RuntimeError("China product checkpoint still has a running collector")
    snapshot_sha = file_sha256(checkpoint_path)
    run_id = str(uuid.uuid4())
    snapshot_id = stable_id(PROVIDER_ID, "snapshot", snapshot_sha[:24])
    now = datetime.now(timezone.utc).isoformat()
    counters: Counter[str] = Counter()
    connection = connect(str(database))
    try:
        connection.execute(
            """insert into source_provider values (?, 'Pokémon China official TCG products', 'official',
            'https://www.pokemon.cn', 'metadata_only', 'Official Pokémon Website in China',
            'https://www.pokemon.cn/terms', null)
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
        for row in checkpoint.execute("select * from products order by provider_record_id"):
            parsed = json.loads(row["parsed_json"])
            sanitized = {**parsed, "images": [
                {key: value for key, value in image.items() if key != "source_url"}
                for image in parsed.get("images") or []
            ]}
            raw = canonical_json(sanitized)
            source_id = stable_id(PROVIDER_ID, row["provider_record_id"], digest(raw)[:16])
            connection.execute(
                """insert into source_record values (?, ?, ?, ?, 'sealed_product', ?, null, ?, ?, ?, null)
                on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
                (source_id, PROVIDER_ID, run_id, snapshot_id, row["provider_record_id"],
                 row["source_url"], digest(raw), raw),
            )
            ptype = classify_product(row["local_name"], row["contents_text"])
            product_id = stable_id(PROVIDER_ID, "sealed", row["provider_record_id"])
            variant_id = stable_id(product_id, "zh-cn", "CN", "standard")
            connection.execute(
                """insert into sealed_product values (?, ?, ?, ?, ?, ?, 'verified', ?)
                on conflict(id) do update set source_record_id=excluded.source_record_id,raw_product_json=excluded.raw_product_json""",
                (product_id, PROVIDER_ID, row["provider_record_id"], source_id,
                 row["local_name"], ptype, raw),
            )
            connection.execute(
                """insert into sealed_product_variant values (?, ?, 'zh-cn', 'CN', ?, 'standard', ?, ?)
                on conflict(id) do update set release_date=excluded.release_date,attributes_json=excluded.attributes_json""",
                (variant_id, product_id, row["local_name"], row["release_date"], canonical_json({
                    "msrp": parse_msrp(row["msrp_text"]), "currency": "CNY", "msrp_text": row["msrp_text"],
                    "contents_text": row["contents_text"], "article_title": parsed.get("article_title"),
                })),
            )
            if row["contents_text"]:
                connection.execute(
                    "insert or replace into product_content values (?, 0, 'other', null, ?, 1, '{}')",
                    (variant_id, row["contents_text"]),
                )
            for ordinal, image in enumerate(parsed.get("images") or []):
                url = image["canonical_url"]
                image_id = stable_id(variant_id, PROVIDER_ID, ordinal, digest(url)[:16])
                connection.execute(
                    "insert or replace into product_image_candidate values (?, ?, ?, ?, 'display', ?, 'link_only', 'candidate', ?)",
                    (image_id, variant_id, source_id, PROVIDER_ID, url, canonical_json({
                        "alt": image.get("alt"), "ordinal": ordinal,
                    })),
                )
                counters["product_images"] += 1
            atype = accessory_type(row["local_name"], row["contents_text"])
            if atype:
                accessory_id = stable_id(PROVIDER_ID, "accessory", row["provider_record_id"])
                connection.execute(
                    "insert or replace into accessory values (?, ?, ?, ?, ?, ?, ?, 'verified')",
                    (accessory_id, PROVIDER_ID, row["provider_record_id"], source_id,
                     row["local_name"], atype, row["contents_text"]),
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
    finally:
        connection.close()
        checkpoint.close()
