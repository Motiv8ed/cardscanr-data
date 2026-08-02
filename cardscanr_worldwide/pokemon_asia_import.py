"""Import completed official Pokémon Asia collector checkpoints into staging."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import connect
from .tcgdex import canonical_json, digest, stable_id

LOCALE_SCOPE = {
    "id": ("id", "ID"), "th": ("th", "TH"),
    "hk": ("zh-tw", "HK"), "tw": ("zh-tw", "TW"),
    "sg": ("en", "SG"), "my": ("en", "MY"), "ph": ("en", "PH"),
}


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def product_release_date(name: str) -> str | None:
    match = re.search(r"\b(\d{2})-(\d{2})-(\d{4})\s*$", name)
    return f"{match.group(3)}-{match.group(1)}-{match.group(2)}" if match else None


def product_type(name: str) -> str:
    lowered = name.casefold()
    if "booster pack" in lowered or "擴充包" in name or "扩充包" in name:
        return "booster_pack"
    if "starter deck" in lowered or "初階牌組" in name or "初阶牌组" in name:
        return "starter_deck"
    if "deck" in lowered or "牌組" in name or "牌组" in name:
        return "theme_deck"
    if "promo" in lowered or "特典卡" in name or "促銷卡" in name or "促销卡" in name:
        return "promotional_pack"
    if "box" in lowered or "boks" in lowered or "盒" in name:
        return "collection_box"
    return "official_card_product"


def _source_record(
    connection: sqlite3.Connection, provider_id: str, run_id: str, snapshot_id: str,
    record_type: str, record_id: str, source_path: str, payload: dict[str, Any], parent_id: str | None = None,
) -> str:
    raw = canonical_json(payload)
    source_id = stable_id(provider_id, record_id, digest(raw)[:16])
    connection.execute(
        """insert into source_record
        (id,provider_id,import_run_id,snapshot_id,record_type,provider_record_id,provider_parent_id,
         source_path,source_sha256,raw_payload_json,error)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null)
        on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
        (source_id, provider_id, run_id, snapshot_id, record_type, record_id, parent_id,
         source_path, digest(raw), raw),
    )
    return source_id


def _direct_mapping(
    connection: sqlite3.Connection, provider_id: str, record_type: str, record_id: str,
    entity_type: str, entity_id: str, source_id: str,
) -> None:
    connection.execute(
        "insert or replace into provider_entity_mapping values (?, ?, ?, ?, ?, 'direct_official_record', 'verified', ?, '{}')",
        (provider_id, record_type, record_id, entity_type, entity_id, source_id),
    )


def _import_product(
    connection: sqlite3.Connection, source: sqlite3.Row, provider_id: str,
    run_id: str, snapshot_id: str, language: str, region: str,
) -> tuple[str, str, str]:
    code = source["expansion_code"]
    classified_product_type = product_type(source["local_name"])
    card_ids = [row[0] for row in source["checkpoint"].execute(
        "select card_id from product_cards where locale=? and expansion_code=? order by cast(card_id as integer)",
        (source["locale"], code),
    ).fetchall()]
    payload = {
        "locale": source["locale"], "expansion_code": code, "local_name": source["local_name"],
        "source_url": source["source_url"], "release_date": product_release_date(source["local_name"]),
        "product_type": classified_product_type, "official_card_ids": card_ids,
    }
    source_id = _source_record(
        connection, provider_id, run_id, snapshot_id, "product", code,
        f"checkpoint:products/{code}", payload,
    )
    series_id = stable_id(provider_id, "series", "official")
    connection.execute(
        """insert into series values (?, ?, 'official', ?, 'Official product chronology', ?)
        on conflict(id) do update set source_record_id=excluded.source_record_id""",
        (series_id, provider_id, source_id, canonical_json({language: "Official product chronology"})),
    )
    set_id = stable_id(provider_id, "set", code)
    connection.execute(
        """insert into card_set values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
        on conflict(id) do update set source_record_id=excluded.source_record_id,
        official_count=excluded.official_count""",
        (set_id, series_id, provider_id, code, source_id, source["local_name"],
         canonical_json({language: source["local_name"]}),
         ("promo" if classified_product_type == "promotional_pack" else
          "deck" if classified_product_type in {"starter_deck", "theme_deck"} else "main"),
         len(card_ids), canonical_json(payload["release_date"])),
    )
    release_id = stable_id(set_id, language, region)
    connection.execute(
        """insert into set_release values (?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?)
        on conflict(id) do update set local_name=excluded.local_name,release_date=excluded.release_date,
        official_count=excluded.official_count,source_record_id=excluded.source_record_id""",
        (release_id, set_id, language, region, source["local_name"], code,
         payload["release_date"], len(card_ids), source_id),
    )
    sealed_id = stable_id(provider_id, "sealed", code)
    sealed_variant_id = stable_id(sealed_id, language, region, "standard")
    connection.execute(
        """insert into sealed_product values (?, ?, ?, ?, ?, ?, 'verified', ?)
        on conflict(id) do update set source_record_id=excluded.source_record_id,raw_product_json=excluded.raw_product_json""",
        (sealed_id, provider_id, code, source_id, source["local_name"], payload["product_type"], canonical_json(payload)),
    )
    connection.execute(
        """insert into sealed_product_variant values (?, ?, ?, ?, ?, 'standard', ?, ?)
        on conflict(id) do update set release_date=excluded.release_date,attributes_json=excluded.attributes_json""",
        (sealed_variant_id, sealed_id, language, region, source["local_name"], payload["release_date"],
         canonical_json({"expansion_code": code, "official_card_count": len(card_ids)})),
    )
    for ordinal, card_id in enumerate(card_ids):
        connection.execute(
            "insert or replace into product_content values (?, ?, 'other', null, ?, 1, ?)",
            (sealed_variant_id, ordinal, f"Official source card ID {card_id}",
             canonical_json({"provider_card_id": card_id})),
        )
    _direct_mapping(connection, provider_id, "product", code, "card_set", set_id, source_id)
    _direct_mapping(connection, provider_id, "product", code, "sealed_product", sealed_id, source_id)
    return set_id, release_id, sealed_variant_id


def _import_card(
    connection: sqlite3.Connection, checkpoint: sqlite3.Connection, card: sqlite3.Row,
    provider_id: str, run_id: str, snapshot_id: str, language: str, region: str,
    releases: dict[str, str],
) -> bool:
    parsed = json.loads(card["parsed_json"])
    provider_card_id = card["card_id"]
    associations = [row[0] for row in checkpoint.execute(
        "select expansion_code from product_cards where locale=? and card_id=? order by expansion_code",
        (card["locale"], provider_card_id),
    ).fetchall()]
    preferred_code = parsed.get("printed_set_code")
    code = preferred_code if preferred_code in releases else (associations[0] if len(associations) == 1 else None)
    payload = {"card_id": provider_card_id, "associations": associations, "parsed": parsed,
               "raw_sha256": card["raw_sha256"], "source_url": card["source_url"]}
    source_id = _source_record(
        connection, provider_id, run_id, snapshot_id, "card", provider_card_id,
        f"checkpoint:cards/{provider_card_id}", payload, code,
    )
    if not code or code not in releases:
        unresolved_id = stable_id(provider_id, "card-set", provider_card_id)
        connection.execute(
            "insert or replace into unresolved_item values (?, 'source_card', ?, ?, ?, 'ambiguous_official_product_membership', ?, ?, 'needs_review', 0)",
            (unresolved_id, provider_card_id, language, region,
             "Official detail could not be assigned to exactly one product/set without guessing",
             canonical_json({"associations": associations, "printed_set_code": preferred_code})),
        )
        return False
    design_id = stable_id(provider_id, "design", provider_card_id)
    printing_id = stable_id(provider_id, "printing", provider_card_id, language, region)
    variant_id = stable_id(printing_id, "unspecified")
    connection.execute(
        """insert into card_design values (?, 'other', ?, ?, ?)
        on conflict(id) do update set canonical_name=excluded.canonical_name,
        national_pokedex_numbers_json=excluded.national_pokedex_numbers_json""",
        (design_id, parsed.get("local_name"), canonical_json(parsed.get("national_pokedex_numbers") or []),
         stable_id(provider_id, provider_card_id)),
    )
    connection.execute(
        """insert into card_printing values (?, ?, ?, ?, ?, ?, null, ?, null, ?, ?, ?, ?, ?, ?, ?, 'verified', ?)
        on conflict(id) do update set source_record_id=excluded.source_record_id,raw_card_json=excluded.raw_card_json""",
        (printing_id, design_id, releases[code], source_id, parsed.get("collector_number") or provider_card_id,
         provider_card_id, parsed.get("illustrator"), parsed.get("stage"), parsed.get("hp"),
         canonical_json(parsed.get("types") or []), parsed.get("regulation_mark"),
         canonical_json(parsed.get("retreat_cost") or []), canonical_json(parsed.get("weaknesses") or []),
         canonical_json(parsed.get("resistances") or []), canonical_json(parsed)),
    )
    connection.execute(
        "insert or replace into card_variant values (?, ?, 'unspecified', null, null, null, null, 0, ?, 'unknown')",
        (variant_id, printing_id, canonical_json({"normalization_note": "Official page does not identify physical finish"})),
    )
    connection.execute(
        "insert or replace into card_localisation values (?, ?, ?, ?, '[]', 'official')",
        (printing_id, language, parsed.get("local_name") or provider_card_id, parsed.get("description")),
    )
    for ordinal, attack in enumerate(parsed.get("attacks") or []):
        if attack.get("name"):
            connection.execute(
                "insert or replace into attack values (?, ?, ?, ?, ?, ?, ?)",
                (printing_id, ordinal, language, attack["name"], canonical_json(attack.get("cost") or []),
                 attack.get("damage"), attack.get("effect")),
            )
    if parsed.get("image_url"):
        image_id = stable_id(variant_id, provider_id, "display", digest(parsed["image_url"])[:16])
        connection.execute(
            "insert or replace into card_image_candidate values (?, ?, ?, ?, 'display', ?, 'link_only', 'candidate')",
            (image_id, variant_id, source_id, provider_id, parsed["image_url"]),
        )
    _direct_mapping(connection, provider_id, "card", provider_card_id, "card_printing", printing_id, source_id)
    connection.execute(
        "update unresolved_item set status='resolved' where entity_type='source_card' and entity_id=? and issue_class='official_detail_not_collected'",
        (provider_card_id,),
    )
    return True


def import_checkpoint(database: Path, checkpoint_path: Path, locale: str) -> dict[str, int]:
    if locale not in LOCALE_SCOPE:
        raise ValueError(f"Unsupported locale {locale}")
    language, region = LOCALE_SCOPE[locale]
    provider_id = f"pokemon-asia-{locale}-official"
    snapshot_sha = file_sha256(checkpoint_path)
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    snapshot_id = stable_id(provider_id, "snapshot", snapshot_sha[:24])
    counters: Counter[str] = Counter()
    checkpoint = sqlite3.connect(f"file:{checkpoint_path.resolve()}?mode=ro", uri=True)
    checkpoint.row_factory = sqlite3.Row
    connection = connect(str(database))
    try:
        active = checkpoint.execute("select count(*) from collector_runs where status='running'").fetchone()[0]
        if active:
            raise RuntimeError("Collector checkpoint still has a running job; import only a stable completed checkpoint")
        connection.execute(
            """insert into source_provider values (?, ?, 'official', ?, 'metadata_only', ?, ?, null)
            on conflict(id) do update set rights_status=excluded.rights_status,attribution_text=excluded.attribution_text""",
            (provider_id, f"Pokémon Asia official trainer site ({locale})", f"https://asia.pokemon-card.com/{locale}",
             "Official Pokémon Card Game trainer site", f"https://asia.pokemon-card.com/{locale}/terms/"),
        )
        connection.execute(
            "insert into import_run values (?, ?, 'running', ?, ?, '{}', '{}', ?, null, null)",
            (run_id, provider_id, str(checkpoint_path), snapshot_sha, now),
        )
        connection.execute(
            """insert into source_snapshot values (?, ?, ?, ?, ?, null, ?, ?, ?)
            on conflict(id) do update set import_run_id=excluded.import_run_id,fetched_at=excluded.fetched_at""",
            (snapshot_id, provider_id, run_id, str(checkpoint_path), snapshot_sha,
             checkpoint_path.stat().st_size, now, str(checkpoint_path.parent / "raw")),
        )
        releases = {}
        products = checkpoint.execute("select * from products where locale=? order by expansion_code", (locale,)).fetchall()
        for product in products:
            augmented = dict(product)
            augmented["checkpoint"] = checkpoint
            _, release_id, _ = _import_product(connection, augmented, provider_id, run_id, snapshot_id, language, region)
            releases[product["expansion_code"]] = release_id
            counters["products"] += 1
        parsed_cards = checkpoint.execute("select * from cards where locale=? and status='parsed' order by cast(card_id as integer)", (locale,)).fetchall()
        for card in parsed_cards:
            if _import_card(connection, checkpoint, card, provider_id, run_id, snapshot_id, language, region, releases):
                counters["cards"] += 1
            else:
                counters["ambiguous_cards"] += 1
        known_ids = checkpoint.execute("select distinct card_id from product_cards where locale=?", (locale,)).fetchall()
        parsed_ids = {row["card_id"] for row in parsed_cards}
        for row in known_ids:
            if row["card_id"] in parsed_ids:
                continue
            unresolved_id = stable_id(provider_id, "detail", row["card_id"])
            connection.execute(
                "insert or replace into unresolved_item values (?, 'source_card', ?, ?, ?, 'official_detail_not_collected', ?, ?, 'open', 0)",
                (unresolved_id, row["card_id"], language, region,
                 "Official card identity enumerated but detail page has not yet been collected",
                 canonical_json({"provider_id": provider_id, "card_id": row["card_id"]})),
            )
            counters["uncollected_card_details"] += 1
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
