"""Import the versioned Pikaqian Simplified-Chinese dataset as a corroborating source."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json, digest, stable_id

PROVIDER_ID = "pokemon-tcg-kb-pikaqian"


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def release_date(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.replace("-", "/").split("/")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return None


def import_dataset(database: Path, source_database: Path, source_commit: str) -> dict[str, int]:
    source = sqlite3.connect(f"file:{source_database.resolve()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    snapshot_sha = file_sha256(source_database)
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    snapshot_id = stable_id(PROVIDER_ID, "snapshot", source_commit[:12], snapshot_sha[:16])
    counters: Counter[str] = Counter()
    connection = connect(str(database))
    try:
        connection.execute(
            """insert into source_provider values (?, 'pokemon-tcg-kb Pikaqian Simplified Chinese snapshot',
            'community_dataset', 'https://github.com/calchulus/pokemon-tcg-kb', 'permission_pending',
            'pokemon-tcg-kb / Pikaqian', null, null)
            on conflict(id) do update set rights_status=excluded.rights_status""", (PROVIDER_ID,),
        )
        connection.execute(
            "insert into import_run values (?, ?, 'running', ?, ?, '{}', '{}', ?, null, null)",
            (run_id, PROVIDER_ID, str(source_database), snapshot_sha, now),
        )
        connection.execute(
            """insert into source_snapshot values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(id) do update set import_run_id=excluded.import_run_id,fetched_at=excluded.fetched_at""",
            (snapshot_id, PROVIDER_ID, run_id, str(source_database), snapshot_sha, source_commit,
             source_database.stat().st_size, now, str(source_database)),
        )
        series_source_id = None
        series_id = stable_id(PROVIDER_ID, "series", "mainland-china")
        for set_row in source.execute(
            "select * from sets where source='pikaqian' and language='zh-CN' order by release_date,set_id",
        ):
            payload = canonical_json(dict(set_row))
            set_source_id = stable_id(PROVIDER_ID, "set", set_row["set_id"], digest(payload)[:16])
            connection.execute(
                """insert into source_record values (?, ?, ?, ?, 'set', ?, null, ?, ?, ?, null)
                on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
                (set_source_id, PROVIDER_ID, run_id, snapshot_id, set_row["set_id"],
                 f"sqlite:sets/{set_row['set_id']}", digest(payload), payload),
            )
            series_source_id = series_source_id or set_source_id
            connection.execute(
                """insert into series values (?, ?, 'mainland-china', ?, 'Mainland China releases', ?)
                on conflict(id) do update set source_record_id=excluded.source_record_id""",
                (series_id, PROVIDER_ID, series_source_id, canonical_json({"en": "Mainland China releases"})),
            )
            set_id = stable_id(PROVIDER_ID, "set", set_row["set_id"])
            release_id = stable_id(set_id, "zh-cn", "CN")
            connection.execute(
                """insert into card_set values (?, ?, ?, ?, ?, ?, ?, 'main', ?, ?, ?)
                on conflict(id) do update set source_record_id=excluded.source_record_id,official_count=excluded.official_count""",
                (set_id, series_id, PROVIDER_ID, set_row["set_id"], set_source_id, set_row["name"],
                 canonical_json({"en": set_row["name"]}), set_row["card_count_official"],
                 canonical_json(release_date(set_row["release_date"])), canonical_json({"source": "pikaqian"})),
            )
            connection.execute(
                """insert into set_release values (?, ?, 'zh-cn', 'CN', ?, ?, ?, ?, 'provisional', ?)
                on conflict(id) do update set official_count=excluded.official_count,source_record_id=excluded.source_record_id""",
                (release_id, set_id, set_row["name"], set_row["set_id"], release_date(set_row["release_date"]),
                 set_row["card_count_official"], set_source_id),
            )
            connection.execute(
                "insert or replace into provider_entity_mapping values (?, 'set', ?, 'card_set', ?, 'direct_dataset_record', 'candidate', ?, '{}')",
                (PROVIDER_ID, set_row["set_id"], set_id, set_source_id),
            )
            cards = source.execute(
                "select * from cards where source='pikaqian' and set_id=? order by local_id,card_id",
                (set_row["set_id"],),
            ).fetchall()
            for card in cards:
                raw = canonical_json(dict(card))
                source_id = stable_id(PROVIDER_ID, "card", card["card_id"], digest(raw)[:16])
                connection.execute(
                    """insert into source_record values (?, ?, ?, ?, 'card', ?, ?, ?, ?, ?, null)
                    on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
                    (source_id, PROVIDER_ID, run_id, snapshot_id, card["card_id"], set_row["set_id"],
                     f"sqlite:cards/{card['card_id']}", digest(raw), raw),
                )
                design_id = stable_id(PROVIDER_ID, "design", card["card_id"])
                printing_id = stable_id(PROVIDER_ID, "printing", card["card_id"], "zh-cn", "CN")
                variant_id = stable_id(printing_id, "depiction-unspecified")
                connection.execute(
                    """insert into card_design values (?, 'other', ?, '[]', ?)
                    on conflict(id) do update set canonical_name=excluded.canonical_name""",
                    (design_id, card["name"], stable_id(PROVIDER_ID, card["card_id"])),
                )
                connection.execute(
                    """insert into card_printing values (?, ?, ?, ?, ?, ?, null, null, null, null, null,
                    '[]', null, '[]', '[]', '[]', 'provisional', ?)
                    on conflict(id) do update set source_record_id=excluded.source_record_id,raw_card_json=excluded.raw_card_json""",
                    (printing_id, design_id, release_id, source_id, card["local_id"], card["card_id"], raw),
                )
                connection.execute(
                    "insert or replace into card_variant values (?, ?, 'depiction-unspecified', null, null, null, null, 0, ?, 'unknown')",
                    (variant_id, printing_id, canonical_json({"provider_image_identity": card["card_id"]})),
                )
                connection.execute(
                    "insert or replace into card_localisation values (?, 'zh-cn', ?, null, '[]', 'provider_translation')",
                    (printing_id, card["name"]),
                )
                image_id = stable_id(variant_id, PROVIDER_ID, "display", digest(card["image_url"])[:16])
                connection.execute(
                    "insert or replace into card_image_candidate values (?, ?, ?, ?, 'display', ?, 'permission_pending', 'candidate')",
                    (image_id, variant_id, source_id, PROVIDER_ID, card["image_url"]),
                )
                connection.execute(
                    "insert or replace into provider_entity_mapping values (?, 'card', ?, 'card_printing', ?, 'direct_dataset_record', 'candidate', ?, '{}')",
                    (PROVIDER_ID, card["card_id"], printing_id, source_id),
                )
                connection.execute(
                    "insert or replace into marketplace_mapping values ('card_variant', ?, 'pikaqian', ?, ?, 'candidate')",
                    (variant_id, card["card_id"], source_id),
                )
                unresolved_id = stable_id(PROVIDER_ID, "official-local-name", card["card_id"])
                connection.execute(
                    "insert or replace into unresolved_item values (?, 'card_printing', ?, 'zh-cn', 'CN', 'missing_official_local_name', ?, ?, 'open', 0)",
                    (unresolved_id, printing_id,
                     "Community record supplies an English translation but not the official Simplified Chinese card name",
                     canonical_json({"provider_name": card["name"], "provider_card_id": card["card_id"]})),
                )
                counters["cards"] += 1
                counters["image_candidates"] += 1
            expected = int(set_row["card_count_official"] or 0)
            if expected != len(cards):
                unresolved_id = stable_id(PROVIDER_ID, "set-count", set_row["set_id"])
                connection.execute(
                    "insert or replace into unresolved_item values (?, 'set_release', ?, 'zh-cn', 'CN', 'official_count_shortfall', ?, ?, 'open', 0)",
                    (unresolved_id, release_id,
                     "Community set inventory differs from its stated official count",
                     canonical_json({"expected": expected, "present": len(cards), "difference": expected - len(cards)})),
                )
                counters["set_count_conflicts"] += 1
            counters["sets"] += 1
        connection.execute(
            "update import_run set status='completed',counters_json=?,checkpoint_json=?,completed_at=? where id=?",
            (canonical_json(dict(counters)), canonical_json({"complete": True, "source_commit": source_commit}),
             datetime.now(timezone.utc).isoformat(), run_id),
        )
        connection.commit()
        return dict(counters)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
        source.close()
