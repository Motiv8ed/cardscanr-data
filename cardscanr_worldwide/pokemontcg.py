"""Import the pinned PokémonTCG JSON repository as an independent provider."""

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

PROVIDER_ID = "pokemontcg-data"


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def tree_sha256(paths: list[Path], root: Path) -> str:
    hasher = hashlib.sha256()
    for path in paths:
        hasher.update(path.relative_to(root).as_posix().encode())
        hasher.update(b"\0")
        hasher.update(bytes.fromhex(file_sha256(path)))
    return hasher.hexdigest()


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def iso_date(value: str | None) -> str | None:
    return value.replace("/", "-") if value else None


def _source_record(
    connection: sqlite3.Connection,
    run_id: str,
    snapshot_id: str,
    record_type: str,
    provider_record_id: str,
    source_path: str,
    payload: dict[str, Any],
    parent_id: str | None = None,
) -> str:
    raw = canonical_json(payload)
    raw_sha256 = digest(raw)
    source_id = stable_id(PROVIDER_ID, provider_record_id, raw_sha256[:16])
    connection.execute(
        """insert into source_record
        (id, provider_id, import_run_id, snapshot_id, record_type, provider_record_id,
         provider_parent_id, source_path, source_sha256, raw_payload_json, error)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null)
        on conflict(id) do update set import_run_id=excluded.import_run_id,
        snapshot_id=excluded.snapshot_id""",
        (source_id, PROVIDER_ID, run_id, snapshot_id, record_type, provider_record_id,
         parent_id, source_path, raw_sha256, raw),
    )
    return source_id


def _direct_mapping(
    connection: sqlite3.Connection, record_type: str, provider_record_id: str,
    entity_type: str, entity_id: str, source_id: str,
) -> None:
    connection.execute(
        "insert or replace into provider_entity_mapping values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (PROVIDER_ID, record_type, provider_record_id, entity_type, entity_id,
         "direct_source_record", "verified", source_id, "{}"),
    )


def _import_sets(
    connection: sqlite3.Connection, run_id: str, snapshot_id: str, sets_path: Path,
) -> dict[str, dict[str, Any]]:
    values = json.loads(sets_path.read_text(encoding="utf-8"))
    results = {}
    for value in values:
        provider_id = str(value["id"])
        source_id = _source_record(
            connection, run_id, snapshot_id, "set", provider_id,
            sets_path.as_posix(), value,
        )
        series_provider_id = stable_id("series", normalized_name(value.get("series") or "unknown"))
        series_id = stable_id(PROVIDER_ID, "en", series_provider_id)
        connection.execute(
            """insert into series values (?, ?, ?, ?, ?, ?)
            on conflict(id) do update set canonical_name=excluded.canonical_name""",
            (series_id, PROVIDER_ID, series_provider_id, source_id,
             value.get("series") or "Unknown", canonical_json({"en": value.get("series") or "Unknown"})),
        )
        set_id = stable_id(PROVIDER_ID, "en", "set", provider_id)
        release_date = iso_date(value.get("releaseDate"))
        connection.execute(
            """insert into card_set values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set source_record_id=excluded.source_record_id,
            official_count=excluded.official_count, release_dates_json=excluded.release_dates_json""",
            (set_id, series_id, PROVIDER_ID, provider_id, source_id, value["name"],
             canonical_json({"en": value["name"]}), "promo" if "promo" in value["name"].casefold() else "main",
             value.get("printedTotal"), canonical_json(release_date), "{}"),
        )
        release_id = stable_id(set_id, "en", "INTL")
        connection.execute(
            """insert into set_release values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set source_record_id=excluded.source_record_id,
            official_count=excluded.official_count""",
            (release_id, set_id, "en", "INTL", value["name"], value.get("ptcgoCode") or provider_id,
             release_date, value.get("printedTotal"), "source", source_id),
        )
        _direct_mapping(connection, "set", provider_id, "card_set", set_id, source_id)
        results[provider_id] = {**value, "source_id": source_id, "set_id": set_id, "release_id": release_id}
    return results


def _import_card(
    connection: sqlite3.Connection, run_id: str, snapshot_id: str,
    relative_path: str, set_info: dict[str, Any], value: dict[str, Any],
) -> tuple[str, str]:
    provider_id = str(value["id"])
    source_id = _source_record(
        connection, run_id, snapshot_id, "card", provider_id, relative_path,
        value, str(set_info["id"]),
    )
    design_id = stable_id(PROVIDER_ID, "en", "design", provider_id)
    printing_id = stable_id(PROVIDER_ID, "en", "printing", provider_id)
    variant_id = stable_id(printing_id, "unspecified")
    dex_numbers = value.get("nationalPokedexNumbers") or []
    connection.execute(
        """insert into card_design values (?, ?, ?, ?, ?)
        on conflict(id) do update set canonical_name=excluded.canonical_name,
        national_pokedex_numbers_json=excluded.national_pokedex_numbers_json""",
        (design_id, str(value.get("supertype") or "other").casefold(), value.get("name"),
         canonical_json(dex_numbers), stable_id(PROVIDER_ID, provider_id)),
    )
    hp_value = value.get("hp")
    hp = int(hp_value) if str(hp_value or "").isdigit() else None
    connection.execute(
        """insert into card_printing values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(id) do update set source_record_id=excluded.source_record_id,
        raw_card_json=excluded.raw_card_json""",
        (printing_id, design_id, set_info["release_id"], source_id, str(value.get("number") or provider_id),
         provider_id, value.get("rarity"), value.get("artist"), value.get("supertype"),
         (value.get("subtypes") or [None])[0], hp, canonical_json(value.get("types") or []),
         value.get("regulationMark"), canonical_json(value.get("retreatCost") or []),
         canonical_json(value.get("weaknesses") or []), canonical_json(value.get("resistances") or []),
         "source", canonical_json(value)),
    )
    connection.execute(
        "insert or replace into card_variant values (?, ?, ?, null, null, null, null, 0, ?, ?)",
        (variant_id, printing_id, "unspecified",
         canonical_json({"normalization_note": "PokemonTCG record has no physical finish identity"}), "unknown"),
    )
    connection.execute(
        "insert or replace into card_localisation values (?, 'en', ?, ?, ?, 'source')",
        (printing_id, value.get("name") or provider_id, value.get("flavorText"),
         canonical_json(value.get("rules") or [])),
    )
    for ordinal, attack in enumerate(value.get("attacks") or []):
        connection.execute(
            "insert or replace into attack values (?, ?, 'en', ?, ?, ?, ?)",
            (printing_id, ordinal, attack.get("name") or "Unnamed attack",
             canonical_json(attack.get("cost") or []), str(attack.get("damage") or "") or None,
             attack.get("text")),
        )
    for ordinal, ability in enumerate(value.get("abilities") or []):
        connection.execute(
            "insert or replace into ability values (?, ?, 'en', ?, ?, ?)",
            (printing_id, ordinal, ability.get("type"), ability.get("name"), ability.get("text") or ""),
        )
    for role, url in (value.get("images") or {}).items():
        image_id = stable_id(variant_id, PROVIDER_ID, role, digest(str(url))[:16])
        connection.execute(
            "insert or replace into card_image_candidate values (?, ?, ?, ?, ?, ?, ?, ?)",
            (image_id, variant_id, source_id, PROVIDER_ID,
             "thumbnail" if role == "small" else "display", str(url), "permission_pending", "candidate"),
        )
    _direct_mapping(connection, "card", provider_id, "card_printing", printing_id, source_id)
    return printing_id, source_id


def _import_decks(
    connection: sqlite3.Connection, run_id: str, snapshot_id: str,
    deck_paths: list[Path], root: Path,
) -> int:
    count = 0
    for path in deck_paths:
        for value in json.loads(path.read_text(encoding="utf-8")):
            provider_id = str(value["id"])
            source_id = _source_record(
                connection, run_id, snapshot_id, "deck", provider_id,
                path.relative_to(root).as_posix(), value, path.stem,
            )
            product_id = stable_id(PROVIDER_ID, "sealed", provider_id)
            variant_id = stable_id(product_id, "en", "INTL", "standard")
            connection.execute(
                """insert into sealed_product values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(id) do update set source_record_id=excluded.source_record_id,
                raw_product_json=excluded.raw_product_json""",
                (product_id, PROVIDER_ID, provider_id, source_id, value["name"],
                 "theme_deck", "provisional", canonical_json(value)),
            )
            connection.execute(
                "insert or replace into sealed_product_variant values (?, ?, 'en', 'INTL', ?, 'standard', null, ?)",
                (variant_id, product_id, value["name"], canonical_json({"types": value.get("types") or []})),
            )
            for ordinal, card in enumerate(value.get("cards") or []):
                printing_id = stable_id(PROVIDER_ID, "en", "printing", card.get("id"))
                connection.execute(
                    "insert or replace into product_content values (?, ?, 'card_printing', ?, ?, ?, ?)",
                    (variant_id, ordinal, printing_id, card.get("name"), int(card.get("count") or 1),
                     canonical_json({"rarity": card.get("rarity"), "provider_card_id": card.get("id")})),
                )
            _direct_mapping(connection, "deck", provider_id, "sealed_product", product_id, source_id)
            count += 1
    return count


def _candidate_crosswalks(connection: sqlite3.Connection) -> Counter[str]:
    counters: Counter[str] = Counter()
    tcgdex_sets = connection.execute(
        """select cs.id, cs.canonical_name, cs.official_count, sr.release_date
        from card_set cs join set_release sr on sr.card_set_id=cs.id
        where cs.provider_id='tcgdex-cards-database' and sr.language_code='en'"""
    ).fetchall()
    by_signature: dict[tuple[str, int | None, str | None], list[sqlite3.Row]] = {}
    for value in tcgdex_sets:
        signature = (normalized_name(value["canonical_name"]), value["official_count"], value["release_date"])
        by_signature.setdefault(signature, []).append(value)
    pokemon_sets = connection.execute(
        """select cs.id, cs.provider_record_id, cs.canonical_name, cs.official_count,
        sr.id release_id, sr.release_date, cs.source_record_id
        from card_set cs join set_release sr on sr.card_set_id=cs.id
        where cs.provider_id=?""", (PROVIDER_ID,),
    ).fetchall()
    mapped_releases: dict[str, str] = {}
    for value in pokemon_sets:
        signature = (normalized_name(value["canonical_name"]), value["official_count"], value["release_date"])
        candidates = by_signature.get(signature, [])
        if len(candidates) != 1:
            counters["set_crosswalk_ambiguous_or_missing"] += 1
            continue
        target = candidates[0]
        connection.execute(
            "insert or replace into provider_entity_mapping values (?, 'set', ?, 'card_set', ?, ?, ?, ?, ?)",
            (PROVIDER_ID, value["provider_record_id"], target["id"],
             "exact_normalized_name_official_count_release_date", "candidate", value["source_record_id"],
             canonical_json({"signature": signature})),
        )
        target_release = connection.execute(
            "select id from set_release where card_set_id=? and language_code='en'", (target["id"],),
        ).fetchone()
        if target_release:
            mapped_releases[value["release_id"]] = target_release["id"]
        counters["set_crosswalk_candidate"] += 1
    for source_release, target_release in mapped_releases.items():
        source_cards = connection.execute(
            "select id, collector_number, source_record_id from card_printing where set_release_id=?",
            (source_release,),
        ).fetchall()
        for card in source_cards:
            targets = connection.execute(
                "select id from card_printing where set_release_id=? and collector_number=?",
                (target_release, card["collector_number"]),
            ).fetchall()
            if len(targets) == 1:
                provider_card_id = connection.execute(
                    "select provider_record_id from source_record where id=?", (card["source_record_id"],),
                ).fetchone()[0]
                connection.execute(
                    "insert or replace into provider_entity_mapping values (?, 'card', ?, 'card_printing', ?, ?, ?, ?, ?)",
                    (PROVIDER_ID, provider_card_id, targets[0]["id"],
                     "exact_set_signature_plus_collector_number", "candidate", card["source_record_id"],
                     canonical_json({"target_set_release_id": target_release,
                                     "collector_number": card["collector_number"]})),
                )
                counters["card_crosswalk_candidate"] += 1
            else:
                counters["card_crosswalk_ambiguous_or_missing"] += 1
    return counters


def import_repository(database: Path, source_root: Path, source_version: str) -> dict[str, int]:
    sets_path = source_root / "sets" / "en.json"
    card_paths = sorted((source_root / "cards" / "en").glob("*.json"))
    deck_paths = sorted((source_root / "decks" / "en").glob("*.json"))
    inputs = [sets_path, *card_paths, *deck_paths]
    snapshot_sha = tree_sha256(inputs, source_root)
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    snapshot_id = stable_id(PROVIDER_ID, "snapshot", snapshot_sha[:24])
    counters: Counter[str] = Counter()
    connection = connect(str(database))
    try:
        connection.execute(
            """insert into source_provider values (?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set rights_status=excluded.rights_status,
            attribution_text=excluded.attribution_text, source_version=excluded.source_version""",
            (PROVIDER_ID, "PokémonTCG pokemon-tcg-data", "open_dataset",
             "https://github.com/PokemonTCG/pokemon-tcg-data", "approved_for_mirror",
             "PokémonTCG contributors; repository metadata", "https://github.com/PokemonTCG/pokemon-tcg-data",
             source_version),
        )
        connection.execute(
            "insert into import_run values (?, ?, 'running', ?, ?, '{}', '{}', ?, null, null)",
            (run_id, PROVIDER_ID, str(source_root), snapshot_sha, now),
        )
        connection.execute(
            """insert into source_snapshot values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set import_run_id=excluded.import_run_id,
            source_version=excluded.source_version, fetched_at=excluded.fetched_at""",
            (snapshot_id, PROVIDER_ID, run_id, str(source_root), snapshot_sha, source_version,
             sum(path.stat().st_size for path in inputs), now, str(source_root)),
        )
        sets = _import_sets(connection, run_id, snapshot_id, sets_path)
        counters["sets"] = len(sets)
        for path in card_paths:
            set_info = sets.get(path.stem)
            if not set_info:
                counters["card_files_without_set"] += 1
                continue
            values = json.loads(path.read_text(encoding="utf-8"))
            for value in values:
                _import_card(connection, run_id, snapshot_id, path.relative_to(source_root).as_posix(), set_info, value)
                counters["cards"] += 1
            connection.commit()
        counters["decks"] = _import_decks(connection, run_id, snapshot_id, deck_paths, source_root)
        counters.update(_candidate_crosswalks(connection))
        connection.execute(
            "update import_run set status='completed', counters_json=?, checkpoint_json=?, completed_at=? where id=?",
            (canonical_json(dict(counters)), canonical_json({"complete": True}),
             datetime.now(timezone.utc).isoformat(), run_id),
        )
        connection.execute("insert or replace into catalogue_meta values ('pokemontcg_source_version', ?)", (source_version,))
        connection.commit()
        return dict(counters)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
