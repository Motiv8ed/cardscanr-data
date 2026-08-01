"""Normalize a lossless TCGdex JSONL export into the local staging database."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import SCHEMA_VERSION
from .schema import connect

PROVIDER_ID = "tcgdex-cards-database"
REGIONS = {
    "ja": "JP", "ko": "KR", "zh-tw": "TW", "zh-cn": "CN",
    "id": "ID", "th": "TH", "es-mx": "MX", "pt-br": "BR",
    "pt-pt": "PT",
}
SUSPICIOUS_NAMES = {
    "おしっこ": "Known implausible TCGdex Japanese name (literal urine) requiring official-list review",
    "ジャニーンのおしっこ": "Known implausible TCGdex Japanese name requiring official-list review",
}
MOJIBAKE = re.compile(r"(?:\ufffd|Ã.|Â.|â€|ðŸ)")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(*parts: object) -> str:
    readable = ":".join(str(part).strip().replace(":", "_") for part in parts)
    return readable if len(readable) <= 180 else f"{readable[:110]}:{digest(readable)[:24]}"


def translated(value: Any, language: str) -> str | None:
    if isinstance(value, dict):
        item = value.get(language)
        return str(item) if item is not None else None
    return str(value) if value is not None else None


def region_for(language: str) -> str:
    return REGIONS.get(language, "INTL")


def variant_rows(raw_variants: Any, language: str) -> list[dict[str, Any]]:
    if isinstance(raw_variants, list):
        rows = []
        for value in raw_variants:
            if not isinstance(value, dict):
                continue
            languages = value.get("languages")
            if isinstance(languages, list) and language not in languages:
                continue
            stamps = value.get("stamp") or []
            if not isinstance(stamps, list):
                stamps = [stamps]
            key_parts = [
                value.get("type") or "unspecified",
                value.get("subtype"), value.get("size"), value.get("foil"),
                "+".join(sorted(str(stamp) for stamp in stamps)) or None,
            ]
            rows.append({
                "variant_key": "/".join(str(part) for part in key_parts if part),
                "finish": value.get("type"),
                "foil_pattern": value.get("foil"),
                "subtype": value.get("subtype"),
                "stamp": "+".join(sorted(str(stamp) for stamp in stamps)) or None,
                "oversized": value.get("size") == "jumbo",
                "attributes": value,
                "recognition_status": "recognized",
            })
        return rows or [unspecified_variant("language-filtered-no-variant")]
    if isinstance(raw_variants, dict):
        rows = []
        for finish in ("normal", "holo", "reverse"):
            if raw_variants.get(finish):
                rows.append({
                    "variant_key": finish,
                    "finish": finish,
                    "foil_pattern": None,
                    "subtype": None,
                    "stamp": None,
                    "oversized": False,
                    "attributes": raw_variants,
                    "recognition_status": "reported",
                })
        if raw_variants.get("jumbo"):
            rows.append({
                "variant_key": "jumbo", "finish": None, "foil_pattern": None,
                "subtype": None, "stamp": None, "oversized": True,
                "attributes": raw_variants, "recognition_status": "reported",
            })
        return rows or [unspecified_variant("legacy-variant-flags-empty")]
    return [unspecified_variant("source-has-no-variant-data")]


def unspecified_variant(reason: str) -> dict[str, Any]:
    return {
        "variant_key": "unspecified", "finish": None, "foil_pattern": None,
        "subtype": None, "stamp": None, "oversized": False,
        "attributes": {"normalization_note": reason}, "recognition_status": "unknown",
    }


def _upsert_source_record(
    connection: sqlite3.Connection, run_id: str, snapshot_id: str, record: dict[str, Any]
) -> str:
    source_id = stable_id(PROVIDER_ID, record["provider_record_id"], record["source_sha256"][:16])
    connection.execute(
        """insert into source_record
        (id, provider_id, import_run_id, snapshot_id, record_type, provider_record_id,
         provider_parent_id, source_path, source_sha256, raw_payload_json, error)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(id) do update set import_run_id=excluded.import_run_id""",
        (source_id, PROVIDER_ID, run_id, snapshot_id, record["record_type"], record["provider_record_id"],
         "/".join(record["provider_record_id"].split("/")[:-1]) or None,
         record["source_path"], record["source_sha256"],
         canonical_json(record.get("payload")) if "payload" in record else None,
         record.get("error")),
    )
    return source_id


def _name(names: dict[str, Any]) -> str:
    for language in ("en", "ja", "fr", "de", "es", "it", "pt"):
        if names.get(language):
            return str(names[language])
    return str(next(iter(names.values()), "Unnamed"))


def _import_series(connection: sqlite3.Connection, record: dict[str, Any], source_id: str) -> None:
    payload = record["payload"]
    names = payload.get("name") or {}
    series_id = stable_id("tcgdex", record["source_domain"], "series", payload["id"])
    connection.execute(
        """insert into series values (?, ?, ?, ?, ?, ?)
        on conflict(id) do update set source_record_id=excluded.source_record_id,
        canonical_name=excluded.canonical_name, local_names_json=excluded.local_names_json""",
        (series_id, PROVIDER_ID, record["provider_record_id"], source_id, _name(names), canonical_json(names)),
    )


def _set_kind(name: str) -> str:
    lowered = name.casefold()
    if "promo" in lowered:
        return "promo"
    if "trainer kit" in lowered or "deck" in lowered:
        return "deck"
    return "main"


def _import_set(connection: sqlite3.Connection, record: dict[str, Any], source_id: str) -> None:
    payload = record["payload"]
    names = payload.get("name") or {}
    domain = record["source_domain"]
    set_id = stable_id("tcgdex", domain, "set", payload["id"])
    serie = payload.get("serie") or {}
    series_id = stable_id("tcgdex", domain, "series", serie.get("id", "unknown"))
    if not connection.execute("select 1 from series where id=?", (series_id,)).fetchone():
        connection.execute(
            "insert into series values (?, ?, ?, ?, ?, ?)",
            (series_id, PROVIDER_ID, f"embedded-series/{serie.get('id', 'unknown')}", source_id,
             _name(serie.get("name") or {}), canonical_json(serie.get("name") or {})),
        )
    release_date = payload.get("releaseDate")
    connection.execute(
        """insert into card_set values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(id) do update set source_record_id=excluded.source_record_id,
        local_names_json=excluded.local_names_json, release_dates_json=excluded.release_dates_json,
        third_party_json=excluded.third_party_json""",
        (set_id, series_id, PROVIDER_ID, record["provider_record_id"], source_id,
         _name(names), canonical_json(names), _set_kind(_name(names)),
         (payload.get("cardCount") or {}).get("official"), canonical_json(release_date),
         canonical_json(payload.get("thirdParty") or {})),
    )
    for language, local_name in names.items():
        release_id = stable_id(set_id, language, region_for(language))
        date = translated(release_date, language)
        connection.execute(
            """insert into set_release values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set local_name=excluded.local_name,
            release_date=excluded.release_date, official_count=excluded.official_count,
            source_record_id=excluded.source_record_id""",
            (release_id, set_id, language, region_for(language), str(local_name), payload.get("id"),
             date, (payload.get("cardCount") or {}).get("official"), "source", source_id),
        )
    for market, market_id in (payload.get("thirdParty") or {}).items():
        connection.execute(
            "insert or replace into marketplace_mapping values (?, ?, ?, ?, ?, ?)",
            ("set_release", set_id, market, str(market_id), source_id, "candidate"),
        )


def _ensure_embedded_set(connection: sqlite3.Connection, record: dict[str, Any], source_id: str) -> str:
    payload = record["payload"]
    embedded = payload.get("set") or {}
    domain = record["source_domain"]
    set_id = stable_id("tcgdex", domain, "set", embedded.get("id", "unknown"))
    if connection.execute("select 1 from card_set where id=?", (set_id,)).fetchone():
        return set_id
    synthetic = {**record, "payload": embedded, "provider_record_id": f"embedded-set/{embedded.get('id', 'unknown')}"}
    _import_set(connection, synthetic, source_id)
    return set_id


def _quality_issue(name: str) -> str | None:
    if name in SUSPICIOUS_NAMES:
        return SUSPICIOUS_NAMES[name]
    if MOJIBAKE.search(name) or any(ord(character) < 32 for character in name):
        return "Encoding or control-character anomaly in source name"
    return None


def _import_localized_attacks(connection: sqlite3.Connection, printing_id: str, language: str, payload: dict[str, Any]) -> None:
    for ordinal, attack in enumerate(payload.get("attacks") or []):
        name = translated(attack.get("name"), language)
        if not name:
            continue
        damage = attack.get("damage")
        connection.execute(
            "insert or replace into attack values (?, ?, ?, ?, ?, ?, ?)",
            (printing_id, ordinal, language, name, canonical_json(attack.get("cost") or []),
             str(damage) if damage is not None else None, translated(attack.get("effect"), language)),
        )
    for ordinal, ability in enumerate(payload.get("abilities") or []):
        effect = translated(ability.get("effect"), language)
        if not effect:
            continue
        connection.execute(
            "insert or replace into ability values (?, ?, ?, ?, ?, ?)",
            (printing_id, ordinal, language, ability.get("type"),
             translated(ability.get("name"), language), effect),
        )


def _import_card(connection: sqlite3.Connection, record: dict[str, Any], source_id: str) -> None:
    payload = record["payload"]
    names = payload.get("name") or {}
    domain = record["source_domain"]
    set_id = _ensure_embedded_set(connection, record, source_id)
    collector_number = Path(record["source_path"]).stem
    design_id = stable_id("tcgdex", domain, (payload.get("set") or {}).get("id", "unknown"), collector_number)
    connection.execute(
        """insert into card_design values (?, ?, ?, ?, ?)
        on conflict(id) do update set canonical_name=excluded.canonical_name,
        national_pokedex_numbers_json=excluded.national_pokedex_numbers_json""",
        (design_id, str(payload.get("category") or "other").casefold(), _name(names),
         canonical_json(payload.get("dexId") or []), design_id),
    )
    for language, local_name_value in names.items():
        local_name = str(local_name_value)
        release_id = stable_id(set_id, language, region_for(language))
        if not connection.execute("select 1 from set_release where id=?", (release_id,)).fetchone():
            embedded_set = payload.get("set") or {}
            connection.execute(
                "insert into set_release values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (release_id, set_id, language, region_for(language),
                 translated(embedded_set.get("name"), language) or _name(embedded_set.get("name") or {}),
                 embedded_set.get("id"), translated(embedded_set.get("releaseDate"), language),
                 (embedded_set.get("cardCount") or {}).get("official"), "source", source_id),
            )
        printing_id = stable_id(design_id, language, region_for(language))
        issue = _quality_issue(local_name)
        status = "quarantined" if issue else "source"
        retreat = payload.get("retreat")
        retreat_json = canonical_json(retreat if isinstance(retreat, list) else ([] if retreat is None else [retreat]))
        connection.execute(
            """insert into card_printing values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set source_record_id=excluded.source_record_id,
            verification_status=excluded.verification_status, raw_card_json=excluded.raw_card_json""",
            (printing_id, design_id, release_id, source_id, collector_number, collector_number,
             payload.get("rarity"), payload.get("illustrator"), payload.get("category"), payload.get("stage"),
             payload.get("hp"), canonical_json(payload.get("types") or []), payload.get("regulationMark"),
             retreat_json, canonical_json(payload.get("weaknesses") or []),
             canonical_json(payload.get("resistances") or []), status, canonical_json(payload)),
        )
        rules = []
        for key in ("effect", "description"):
            value = translated(payload.get(key), language)
            if value:
                rules.append(value)
        connection.execute(
            "insert or replace into card_localisation values (?, ?, ?, ?, ?, ?)",
            (printing_id, language, local_name, translated(payload.get("description"), language),
             canonical_json(rules), "source"),
        )
        _import_localized_attacks(connection, printing_id, language, payload)
        variants = variant_rows(payload.get("variants"), language)
        for variant in variants:
            variant_id = stable_id(printing_id, variant["variant_key"])
            connection.execute(
                "insert or replace into card_variant values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (variant_id, printing_id, variant["variant_key"], variant["finish"],
                 variant["foil_pattern"], variant["subtype"], variant["stamp"],
                 int(variant["oversized"]), canonical_json(variant["attributes"]),
                 variant["recognition_status"]),
            )
            for market, market_id in (variant["attributes"].get("thirdParty") or {}).items():
                connection.execute(
                    "insert or replace into marketplace_mapping values (?, ?, ?, ?, ?, ?)",
                    ("card_variant", variant_id, market, str(market_id), source_id, "candidate"),
                )
        for market, market_id in (payload.get("thirdParty") or {}).items():
            connection.execute(
                "insert or replace into marketplace_mapping values (?, ?, ?, ?, ?, ?)",
                ("card_printing", printing_id, market, str(market_id), source_id, "candidate"),
            )
        if issue:
            unresolved_id = stable_id("quality", printing_id, language, digest(issue)[:12])
            connection.execute(
                "insert or replace into unresolved_item values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (unresolved_id, "card_printing", printing_id, language, region_for(language),
                 "source_text_quality", issue, canonical_json({"name": local_name, "source_path": record["source_path"]}),
                 "needs_review", 0),
            )


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def import_jsonl(database: Path, jsonl: Path, source_version: str) -> dict[str, int]:
    connection = connect(str(database))
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    counters: Counter[str] = Counter()
    try:
        connection.execute(
            "insert or replace into source_provider values (?, ?, ?, ?, ?, ?, ?, ?)",
            (PROVIDER_ID, "TCGdex cards-database", "open_dataset", "https://github.com/tcgdex/cards-database",
             "approved_for_mirror", "TCGdex contributors; MIT License",
             "https://github.com/tcgdex/cards-database/blob/master/LICENSE", source_version),
        )
        connection.execute(
            "insert into import_run values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, PROVIDER_ID, "running", str(jsonl), file_sha256(jsonl), "{}", "{}", now, None, None),
        )
        input_sha256 = file_sha256(jsonl)
        snapshot_id = stable_id(PROVIDER_ID, "snapshot", input_sha256[:24])
        connection.execute(
            """insert into source_snapshot values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set import_run_id=excluded.import_run_id,
            source_version=excluded.source_version, fetched_at=excluded.fetched_at""",
            (snapshot_id, PROVIDER_ID, run_id, str(jsonl), input_sha256, source_version,
             jsonl.stat().st_size, now, str(jsonl)),
        )
        records: list[tuple[dict[str, Any], str]] = []
        with jsonl.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                record = json.loads(line)
                source_id = _upsert_source_record(connection, run_id, snapshot_id, record)
                counters[f"source_{record['record_type']}"] += 1
                if record.get("error"):
                    counters["source_errors"] += 1
                else:
                    records.append((record, source_id))
                if line_number % 1000 == 0:
                    connection.commit()
        order = {"series": 0, "set": 1, "card": 2}
        records.sort(key=lambda pair: (order[pair[0]["record_type"]], pair[0]["index"]))
        for index, (record, source_id) in enumerate(records, 1):
            if record["record_type"] == "series":
                _import_series(connection, record, source_id)
            elif record["record_type"] == "set":
                _import_set(connection, record, source_id)
            else:
                _import_card(connection, record, source_id)
            counters[f"normalized_{record['record_type']}"] += 1
            if index % 500 == 0:
                connection.execute(
                    "update import_run set checkpoint_json=? where id=?",
                    (canonical_json({"normalized_records": index}), run_id),
                )
                connection.commit()
        table_counts = {}
        for table in ("series", "card_set", "set_release", "card_design", "card_printing",
                      "card_variant", "card_localisation", "attack", "ability",
                      "marketplace_mapping", "unresolved_item"):
            table_counts[table] = connection.execute(f"select count(*) from {table}").fetchone()[0]
        counters.update({f"rows_{key}": value for key, value in table_counts.items()})
        connection.execute("insert or replace into catalogue_meta values ('schema_version', ?)", (str(SCHEMA_VERSION),))
        connection.execute("insert or replace into catalogue_meta values ('tcgdex_source_version', ?)", (source_version,))
        connection.execute("insert or replace into catalogue_meta values ('last_import_run_id', ?)", (run_id,))
        connection.execute(
            "update import_run set status='completed', counters_json=?, checkpoint_json=?, completed_at=? where id=?",
            (canonical_json(dict(counters)), canonical_json({"normalized_records": len(records), "complete": True}),
             datetime.now(timezone.utc).isoformat(), run_id),
        )
        connection.commit()
        return dict(counters)
    except Exception as error:
        connection.rollback()
        connection.execute(
            "update import_run set status='failed', error_summary=?, completed_at=? where id=?",
            (f"{type(error).__name__}: {error}", datetime.now(timezone.utc).isoformat(), run_id),
        )
        connection.commit()
        raise
    finally:
        connection.close()
