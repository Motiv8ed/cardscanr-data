"""Apply narrowly scoped, evidence-backed normalization corrections."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json, stable_id

PROVIDER_ID = "cardscanr-catalogue-corrections"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_corrections(database: Path, registry: Path) -> dict[str, int]:
    payload = json.loads(registry.read_text(encoding="utf-8"))
    registry_sha = file_sha256(registry)
    connection = connect(str(database))
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    snapshot_id = stable_id(PROVIDER_ID, "snapshot", registry_sha[:24])
    counters: Counter[str] = Counter()
    try:
        connection.execute(
            """insert into source_provider values (?, 'CardScanR evidence-backed catalogue corrections',
               'internal', 'generated://cardscanr/catalogue-corrections', 'approved_for_mirror',
               'CardScanR normalization audit', null, ?)
               on conflict(id) do update set source_version=excluded.source_version""",
            (PROVIDER_ID, str(payload.get("schema_version", 1))),
        )
        connection.execute(
            "insert into import_run values (?, ?, 'running', ?, ?, '{}', '{}', ?, null, null)",
            (run_id, PROVIDER_ID, str(registry), registry_sha, now),
        )
        connection.execute(
            """insert into source_snapshot values (?, ?, ?, ?, ?, ?, ?, ?, ?)
               on conflict(id) do update set import_run_id=excluded.import_run_id,fetched_at=excluded.fetched_at""",
            (snapshot_id, PROVIDER_ID, run_id, str(registry), registry_sha,
             str(payload.get("schema_version", 1)), registry.stat().st_size, now, str(registry)),
        )
        for correction in payload["corrections"]:
            if correction["entity_type"] != "card_printing" or correction["field"] != "collector_number":
                raise ValueError(f"Unsupported correction type: {correction['id']}")
            row = connection.execute(
                "select id,set_release_id,collector_number,local_printing_key from card_printing where id=?",
                (correction["entity_id"],),
            ).fetchone()
            if not row:
                raise RuntimeError(f"Correction entity not found: {correction['entity_id']}")
            current = row["collector_number"]
            if current == correction["corrected_value"]:
                counters["already_applied"] += 1
            elif current != correction["expected_value"]:
                raise RuntimeError(
                    f"Correction precondition failed for {correction['id']}: {current!r}"
                )
            else:
                connection.execute(
                    "update card_printing set collector_number=?,verification_status='corroborated' where id=?",
                    (correction["corrected_value"], row["id"]),
                )
                counters["applied"] += 1
            raw = canonical_json(correction)
            source_id = stable_id(PROVIDER_ID, correction["id"], hashlib.sha256(raw.encode()).hexdigest()[:16])
            connection.execute(
                """insert into source_record values (?, ?, ?, ?, 'normalization_correction', ?, ?, ?, ?, ?, null)
                   on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
                (source_id, PROVIDER_ID, run_id, snapshot_id, correction["id"], correction["entity_id"],
                 str(registry), hashlib.sha256(raw.encode()).hexdigest(), raw),
            )
            connection.execute(
                """insert or replace into provider_entity_mapping values (?, 'normalization_correction', ?,
                   'card_printing', ?, 'evidence_backed_field_correction', 'verified', ?, ?)""",
                (PROVIDER_ID, correction["id"], correction["entity_id"], source_id, raw),
            )
            old_group = f"{row['set_release_id']}|{correction['expected_value']}"
            connection.execute(
                """update unresolved_item set status='resolved',summary=?
                    where entity_type='collector_collision_group' and entity_id=?
                      and issue_class='collector_number_collision'""",
                (f"Resolved by correction {correction['id']}", old_group),
            )
        connection.execute(
            "update import_run set status='completed',counters_json=?,checkpoint_json=?,completed_at=? where id=?",
            (canonical_json(dict(counters)), canonical_json({"complete": True, "registry_sha256": registry_sha}),
             datetime.now(timezone.utc).isoformat(), run_id),
        )
        connection.commit()
        return dict(counters)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

