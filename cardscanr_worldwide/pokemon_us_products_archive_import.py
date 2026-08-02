"""Import archived official US Pokémon TCG product pages into worldwide staging."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json, digest, stable_id

PROVIDER_ID = "pokemon-us-products-official-archive"


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def release_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def product_type(name: str) -> str:
    lower = name.casefold()
    for tokens, result in (
        (("elite trainer box",), "elite_trainer_box"), (("ultra-premium", "ultra premium"), "ultra_premium_collection"),
        (("premium collection",), "premium_collection"), (("special collection",), "special_collection"),
        (("league battle deck",), "league_battle_deck"), (("battle deck",), "battle_deck"),
        (("trainer toolkit",), "trainer_toolkit"), (("build & battle stadium",), "build_battle_stadium"),
        (("build & battle box",), "build_battle_box"), (("booster bundle",), "booster_bundle"),
        (("booster box",), "booster_box"), (("booster pack",), "booster_pack"),
        (("collector chest",), "collector_chest"), (("mini tin",), "mini_tin"), (("tin",), "tin"),
        (("blister",), "blister"), (("collection", "box"), "collection_box"), (("deck",), "theme_deck"),
    ):
        if any(token in lower for token in tokens):
            return result
    return "official_product"


def import_checkpoint(database: Path, checkpoint_path: Path) -> dict[str, int]:
    checkpoint = sqlite3.connect(f"file:{checkpoint_path.resolve()}?mode=ro", uri=True)
    checkpoint.row_factory = sqlite3.Row
    if checkpoint.execute("select count(*) from runs where status='running'").fetchone()[0]:
        checkpoint.close()
        raise RuntimeError("US product archive checkpoint still has a running collector")
    snapshot_sha = file_sha256(checkpoint_path)
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    snapshot_id = stable_id(PROVIDER_ID, "snapshot", snapshot_sha[:24])
    counters: Counter[str] = Counter()
    connection = connect(str(database))
    try:
        connection.execute(
            """insert into source_provider values (?, 'Pokémon international US product gallery archive',
            'official_archive', 'https://www.pokemon.com/us/pokemon-tcg/product-gallery/', 'metadata_only',
            'The Pokémon Company International via the Internet Archive',
            'https://www.pokemon.com/us/legal/terms-of-use/', null)
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
        for row in checkpoint.execute("select * from products where status='parsed' order by provider_record_id"):
            parsed = json.loads(row["parsed_json"])
            source_id = stable_id(PROVIDER_ID, row["provider_record_id"], row["raw_sha256"][:16])
            payload = canonical_json({
                "archive_timestamp": row["archive_timestamp"], "archive_digest": row["archive_digest"],
                "replay_url": row["replay_url"], "parsed": parsed,
            })
            connection.execute(
                """insert into source_record values (?, ?, ?, ?, 'sealed_product', ?, null, ?, ?, ?, null)
                on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
                (source_id, PROVIDER_ID, run_id, snapshot_id, row["provider_record_id"],
                 row["source_url"], digest(payload), payload),
            )
            product_id = stable_id(PROVIDER_ID, "sealed", row["provider_record_id"])
            variant_id = stable_id(product_id, "en", "US", "standard")
            name = parsed["local_name"]
            connection.execute(
                """insert into sealed_product values (?, ?, ?, ?, ?, ?, 'verified', ?)
                on conflict(id) do update set source_record_id=excluded.source_record_id,
                canonical_name=excluded.canonical_name,product_type=excluded.product_type,raw_product_json=excluded.raw_product_json""",
                (product_id, PROVIDER_ID, row["provider_record_id"], source_id, name, product_type(name), payload),
            )
            connection.execute(
                """insert into sealed_product_variant values (?, ?, 'en', 'US', ?, 'standard', ?, ?)
                on conflict(id) do update set release_date=excluded.release_date,attributes_json=excluded.attributes_json""",
                (variant_id, product_id, name, release_date(parsed.get("release_date_text")), canonical_json({
                    "description": parsed.get("description"), "archive_timestamp": row["archive_timestamp"],
                })),
            )
            for ordinal, content in enumerate(parsed.get("contents") or []):
                quantity_match = re.match(r"(\d+)\s+", content)
                connection.execute(
                    "insert or replace into product_content values (?, ?, 'other', null, ?, ?, '{}')",
                    (variant_id, ordinal, content, int(quantity_match.group(1)) if quantity_match else 1),
                )
                counters["product_contents"] += 1
            for ordinal, image in enumerate(parsed.get("images") or []):
                url = image["canonical_url"]
                image_id = stable_id(variant_id, PROVIDER_ID, ordinal, digest(url)[:16])
                connection.execute(
                    "insert or replace into product_image_candidate values (?, ?, ?, ?, 'display', ?, 'link_only', 'candidate', ?)",
                    (image_id, variant_id, source_id, PROVIDER_ID, url,
                     canonical_json({"ordinal": ordinal, "alt": image.get("alt"), "source_url": image.get("source_url")})),
                )
                counters["product_images"] += 1
            connection.execute(
                "insert or replace into provider_entity_mapping values (?, 'sealed_product', ?, 'sealed_product', ?, 'direct_official_archive', 'verified', ?, '{}')",
                (PROVIDER_ID, row["provider_record_id"], product_id, source_id),
            )
            counters["products"] += 1
        for row in checkpoint.execute("select provider_record_id,error from products where status!='parsed'"):
            unresolved_id = stable_id(PROVIDER_ID, "archive-error", row["provider_record_id"])
            connection.execute(
                "insert or replace into unresolved_item values (?, 'source_product', ?, 'en', 'US', 'official_archive_collection_error', ?, ?, 'open', 0)",
                (unresolved_id, row["provider_record_id"], "Official product archive page has not yet been parsed",
                 canonical_json({"error": row["error"]})),
            )
            counters["collection_errors"] += 1
        connection.execute(
            "update import_run set status='completed',counters_json=?,checkpoint_json=?,completed_at=? where id=?",
            (canonical_json(dict(counters)), canonical_json({"complete": counters["collection_errors"] == 0}),
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

