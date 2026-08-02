"""Import archived official Korean Pokémon TCG card pages into worldwide staging."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import connect
from .tcgdex import canonical_json, digest, stable_id

PROVIDER_ID = "pokemon-korea-official-archive"
LANGUAGE = "ko"
REGION = "KR"


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def normalized_code(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def normalized_number(value: str | None) -> str:
    text = (value or "").strip().upper()
    numeric = re.fullmatch(r"0*(\d+)", text)
    return str(int(numeric.group(1))) if numeric else re.sub(r"\s+", "", text)


def _source_record(
    connection: sqlite3.Connection, run_id: str, snapshot_id: str, record_type: str,
    provider_record_id: str, source_path: str, payload: dict[str, Any],
) -> str:
    raw = canonical_json(payload)
    source_id = stable_id(PROVIDER_ID, record_type, provider_record_id, digest(raw)[:16])
    connection.execute(
        """insert into source_record values (?, ?, ?, ?, ?, ?, null, ?, ?, ?, null)
        on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
        (source_id, PROVIDER_ID, run_id, snapshot_id, record_type, provider_record_id,
         source_path, digest(raw), raw),
    )
    return source_id


def _mapping(
    connection: sqlite3.Connection, record_type: str, record_id: str, entity_type: str,
    entity_id: str, source_id: str, method: str,
) -> None:
    connection.execute(
        "insert or replace into provider_entity_mapping values (?, ?, ?, ?, ?, ?, 'verified', ?, '{}')",
        (PROVIDER_ID, record_type, record_id, entity_type, entity_id, method, source_id),
    )


def _existing_releases(connection: sqlite3.Connection) -> dict[str, list[str]]:
    releases: dict[str, list[str]] = defaultdict(list)
    for release_id, code in connection.execute(
        "select id,release_code from set_release where language_code=? and region_code=?", (LANGUAGE, REGION),
    ):
        releases[normalized_code(code)].append(release_id)
    return releases


def _prepare_releases(
    connection: sqlite3.Connection, run_id: str, snapshot_id: str,
    cards: list[tuple[sqlite3.Row, dict[str, Any]]], counters: Counter[str],
) -> dict[str, str]:
    existing = _existing_releases(connection)
    grouped: dict[str, list[tuple[sqlite3.Row, dict[str, Any]]]] = defaultdict(list)
    for row, parsed in cards:
        if parsed.get("set_code"):
            grouped[normalized_code(parsed["set_code"])].append((row, parsed))
    releases: dict[str, str] = {}
    for key, members in grouped.items():
        if len(existing.get(key, [])) == 1:
            releases[key] = existing[key][0]
            counters["matched_set_releases"] += 1
            continue
        set_code = str(members[0][1]["set_code"])
        set_names = [name for _, card in members for name in card.get("set_names") or []]
        set_name = Counter(set_names).most_common(1)[0][0] if set_names else set_code
        totals = [
            int(card["printed_total"]) for _, card in members
            if str(card.get("printed_total") or "").isdigit()
        ]
        official_count = max(totals) if totals else len(members)
        payload = {
            "set_code": set_code, "local_name": set_name, "official_count": official_count,
            "archived_card_ids": [row["provider_record_id"] for row, _ in members],
        }
        source_id = _source_record(
            connection, run_id, snapshot_id, "set", set_code,
            f"checkpoint:cards/set/{set_code}", payload,
        )
        series_id = stable_id(PROVIDER_ID, "series", "official")
        connection.execute(
            """insert into series values (?, ?, 'official', ?, 'Official Korean product chronology', ?)
            on conflict(id) do update set source_record_id=excluded.source_record_id""",
            (series_id, PROVIDER_ID, source_id, canonical_json({LANGUAGE: "한국 공식 상품 연표"})),
        )
        set_id = stable_id(PROVIDER_ID, "set", set_code)
        release_id = stable_id(set_id, LANGUAGE, REGION)
        connection.execute(
            """insert into card_set values (?, ?, ?, ?, ?, ?, ?, 'main', ?, 'null', '{}')
            on conflict(id) do update set source_record_id=excluded.source_record_id,
            official_count=excluded.official_count""",
            (set_id, series_id, PROVIDER_ID, set_code, source_id, set_name,
             canonical_json({LANGUAGE: set_name}), official_count),
        )
        connection.execute(
            """insert into set_release values (?, ?, ?, ?, ?, ?, null, ?, 'verified', ?)
            on conflict(id) do update set local_name=excluded.local_name,official_count=excluded.official_count,
            source_record_id=excluded.source_record_id""",
            (release_id, set_id, LANGUAGE, REGION, set_name, set_code, official_count, source_id),
        )
        _mapping(connection, "set", set_code, "card_set", set_id, source_id, "direct_official_archive")
        releases[key] = release_id
        counters["created_set_releases"] += 1
    return releases


def _printing_matches(
    connection: sqlite3.Connection, release_id: str, collector_number: str | None,
) -> list[str]:
    target = normalized_number(collector_number)
    return [
        printing_id for printing_id, number in connection.execute(
            "select id,collector_number from card_printing where set_release_id=?", (release_id,),
        ) if normalized_number(number) == target
    ]


def _ensure_variant(connection: sqlite3.Connection, printing_id: str) -> str:
    candidates = connection.execute(
        """select id from card_variant where card_printing_id=?
        order by case when variant_key in ('depiction-unspecified','unspecified') then 0 else 1 end,id""",
        (printing_id,),
    ).fetchall()
    for (variant_id,) in candidates:
        if variant_id.endswith(":depiction-unspecified") or variant_id.endswith(":unspecified"):
            return variant_id
    if len(candidates) == 1:
        return candidates[0][0]
    variant_id = stable_id(printing_id, "official-depiction-unspecified")
    connection.execute(
        "insert or ignore into card_variant values (?, ?, 'official-depiction-unspecified', null, null, null, null, 0, ?, 'unknown')",
        (variant_id, printing_id, canonical_json({
            "normalization_note": "Official Korean page identifies the depiction but not the physical finish",
        })),
    )
    return variant_id


def _import_card(
    connection: sqlite3.Connection, row: sqlite3.Row, parsed: dict[str, Any], run_id: str,
    snapshot_id: str, release_id: str, counters: Counter[str],
) -> None:
    provider_record_id = row["provider_record_id"]
    payload = {
        "archive_timestamp": row["archive_timestamp"], "archive_digest": row["archive_digest"],
        "replay_url": row["replay_url"], "raw_sha256": row["raw_sha256"], "parsed": parsed,
    }
    source_id = _source_record(
        connection, run_id, snapshot_id, "card", provider_record_id,
        f"checkpoint:cards/{provider_record_id}", payload,
    )
    matches = _printing_matches(connection, release_id, parsed.get("collector_number"))
    if len(matches) == 1:
        printing_id = matches[0]
        variant_id = _ensure_variant(connection, printing_id)
        connection.execute(
            """update card_printing set rarity=coalesce(?,rarity),illustrator=coalesce(?,illustrator),
            stage=coalesce(?,stage),hp=coalesce(?,hp),regulation_mark=coalesce(?,regulation_mark),
            verification_status='verified' where id=?""",
            (parsed.get("rarity"), parsed.get("illustrator"), parsed.get("stage"), parsed.get("hp"),
             parsed.get("regulation_mark"), printing_id),
        )
        counters["matched_printings"] += 1
    elif not matches:
        design_id = stable_id(PROVIDER_ID, "design", provider_record_id)
        printing_id = stable_id(PROVIDER_ID, "printing", provider_record_id, LANGUAGE, REGION)
        variant_id = stable_id(printing_id, "official-depiction-unspecified")
        connection.execute(
            """insert into card_design values (?, 'other', ?, ?, ?)
            on conflict(id) do update set canonical_name=excluded.canonical_name""",
            (design_id, parsed.get("local_name"), canonical_json(parsed.get("national_pokedex_numbers") or []),
             stable_id(PROVIDER_ID, provider_record_id)),
        )
        connection.execute(
            """insert into card_printing values (?, ?, ?, ?, ?, ?, ?, ?, null, ?, ?, ?, ?, ?, ?, ?, 'verified', ?)
            on conflict(id) do update set source_record_id=excluded.source_record_id,raw_card_json=excluded.raw_card_json""",
            (printing_id, design_id, release_id, source_id, parsed.get("collector_number") or provider_record_id,
             provider_record_id, parsed.get("rarity"), parsed.get("illustrator"), parsed.get("stage"), parsed.get("hp"),
             canonical_json(parsed.get("types") or []), parsed.get("regulation_mark"),
             canonical_json(parsed.get("retreat_cost") or []), canonical_json(parsed.get("weaknesses") or []),
             canonical_json(parsed.get("resistances") or []), canonical_json(parsed)),
        )
        connection.execute(
            "insert or replace into card_variant values (?, ?, 'official-depiction-unspecified', null, null, null, null, 0, ?, 'unknown')",
            (variant_id, printing_id, canonical_json({"official_archive": True})),
        )
        counters["created_printings"] += 1
    else:
        unresolved_id = stable_id(PROVIDER_ID, "ambiguous-printing", provider_record_id)
        connection.execute(
            "insert or replace into unresolved_item values (?, 'source_card', ?, ?, ?, 'ambiguous_printing_match', ?, ?, 'needs_review', 0)",
            (unresolved_id, provider_record_id, LANGUAGE, REGION,
             "Official Korean card matches multiple staged printings for the same set and collector number",
             canonical_json({"printing_ids": matches, "parsed": parsed})),
        )
        counters["ambiguous_printings"] += 1
        return
    connection.execute(
        """insert into card_localisation values (?, ?, ?, ?, '[]', 'official')
        on conflict(card_printing_id,language_code) do update set name=excluded.name,
        flavor_text=excluded.flavor_text,translation_status='official'""",
        (printing_id, LANGUAGE, parsed.get("local_name") or provider_record_id, parsed.get("description")),
    )
    connection.execute("delete from attack where card_printing_id=? and language_code=?", (printing_id, LANGUAGE))
    for ordinal, attack in enumerate(parsed.get("attacks") or []):
        if attack.get("name"):
            connection.execute(
                "insert into attack values (?, ?, ?, ?, ?, ?, ?)",
                (printing_id, ordinal, LANGUAGE, attack["name"], canonical_json(attack.get("cost") or []),
                 attack.get("damage"), attack.get("effect")),
            )
    if parsed.get("image_url"):
        image_id = stable_id(variant_id, PROVIDER_ID, "display", digest(parsed["image_url"])[:16])
        connection.execute(
            "insert or replace into card_image_candidate values (?, ?, ?, ?, 'display', ?, 'link_only', 'candidate')",
            (image_id, variant_id, source_id, PROVIDER_ID, parsed["image_url"]),
        )
        counters["image_candidates"] += 1
    _mapping(connection, "card", provider_record_id, "card_printing", printing_id, source_id, "set_collector_exact")
    _mapping(connection, "card", provider_record_id, "card_variant", variant_id, source_id, "official_depiction")


def import_checkpoint(database: Path, checkpoint_path: Path) -> dict[str, int]:
    snapshot_sha = file_sha256(checkpoint_path)
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    snapshot_id = stable_id(PROVIDER_ID, "snapshot", snapshot_sha[:24])
    counters: Counter[str] = Counter()
    checkpoint = sqlite3.connect(f"file:{checkpoint_path.resolve()}?mode=ro", uri=True)
    checkpoint.row_factory = sqlite3.Row
    connection = connect(str(database))
    try:
        if checkpoint.execute("select count(*) from collector_runs where status='running'").fetchone()[0]:
            raise RuntimeError("Korean archive checkpoint still has a running collector")
        connection.execute(
            """insert into source_provider values (?, 'Pokémon Korea official card archive', 'official_archive',
            'https://pokemoncard.co.kr', 'metadata_only', 'Pokémon Korea via the Internet Archive',
            'https://pokemoncard.co.kr/terms', null)
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
        parsed_cards = [
            (row, json.loads(row["parsed_json"])) for row in checkpoint.execute(
                "select * from cards where status='parsed' order by provider_record_id",
            )
        ]
        releases = _prepare_releases(connection, run_id, snapshot_id, parsed_cards, counters)
        for row, parsed in parsed_cards:
            key = normalized_code(parsed.get("set_code"))
            if not key or key not in releases:
                counters["cards_without_set_code"] += 1
                continue
            _import_card(connection, row, parsed, run_id, snapshot_id, releases[key], counters)
            connection.execute(
                """update unresolved_item set status='resolved'
                     where entity_type='source_card' and entity_id=?
                       and issue_class='official_archive_collection_error'""",
                (row["provider_record_id"],),
            )
        errors = checkpoint.execute(
            """select provider_record_id,status,error from cards
                 where status!='parsed' and provider_record_id!='logout' order by provider_record_id""",
        ).fetchall()
        for row in errors:
            unresolved_id = stable_id(PROVIDER_ID, "archive-error", row["provider_record_id"])
            connection.execute(
                "insert or replace into unresolved_item values (?, 'source_card', ?, ?, ?, 'official_archive_collection_error', ?, ?, 'open', 0)",
                (unresolved_id, row["provider_record_id"], LANGUAGE, REGION,
                 "An indexed official Korean archive page has not yet been parsed",
                 canonical_json({"status": row["status"], "error": row["error"]})),
            )
            counters["collection_errors"] += 1
        connection.execute(
            "update import_run set status='completed',counters_json=?,checkpoint_json=?,completed_at=? where id=?",
            (canonical_json(dict(counters)), canonical_json({"complete": not errors}),
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
