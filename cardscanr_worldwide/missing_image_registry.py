"""Import a preserved CardScanR missing-image registry into worldwide staging."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import connect
from .tcgdex import canonical_json, digest, stable_id

PROVIDER_ID = "cardscanr-missing-image-registry"
INVENTORY_SUFFIX = "UNRESOLVED_2907_INVENTORY.json"
REGIONS = {"Japan": "JP", "English/international": "INTL"}


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_inventory(package_path: Path) -> tuple[str, dict[str, Any]]:
    with zipfile.ZipFile(package_path) as archive:
        members = [name for name in archive.namelist() if name.endswith(INVENTORY_SUFFIX)]
        if not members:
            raise ValueError(f"Expected at least one {INVENTORY_SUFFIX}; found none")
        blobs = {name: archive.read(name) for name in members}
        hashes = {hashlib.sha256(blob).hexdigest() for blob in blobs.values()}
        if len(hashes) != 1:
            raise ValueError(f"Conflicting copies of {INVENTORY_SUFFIX} in package")
        member = next((name for name in members if "/source/" in name), sorted(members)[0])
        payload = json.loads(blobs[member])
    if payload.get("identity_count") != len(payload.get("records") or []):
        raise ValueError("Missing-image registry identity count does not match its records")
    return member, payload


def _candidate_matches(connection: sqlite3.Connection, record: dict[str, Any]) -> list[sqlite3.Row]:
    urls = [
        attempt.get("url") for attempt in record.get("previously_attempted_sources") or []
        if attempt.get("url")
    ]
    if not urls:
        return []
    placeholders = ",".join("?" for _ in urls)
    return list(connection.execute(
        f"select id,card_variant_id,provider_id,source_url from card_image_candidate "
        f"where source_url in ({placeholders}) order by source_url",
        urls,
    ))


def import_registry(database: Path, package_path: Path) -> dict[str, int]:
    member, inventory = load_inventory(package_path)
    package_sha = file_sha256(package_path)
    run_id = str(uuid.uuid4())
    snapshot_id = stable_id(PROVIDER_ID, "snapshot", package_sha[:24])
    now = datetime.now(timezone.utc).isoformat()
    counters: Counter[str] = Counter()
    connection = connect(str(database))
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """insert into source_provider values (?, 'CardScanR preserved missing-image registry',
            'first_party_audit', null, 'self_controlled', 'CardScanR', null, null)
            on conflict(id) do update set rights_status=excluded.rights_status""",
            (PROVIDER_ID,),
        )
        connection.execute(
            "insert into import_run values (?, ?, 'running', ?, ?, '{}', '{}', ?, null, null)",
            (run_id, PROVIDER_ID, str(package_path), package_sha, now),
        )
        connection.execute(
            """insert into source_snapshot values (?, ?, ?, ?, ?, null, ?, ?, ?)
            on conflict(id) do update set import_run_id=excluded.import_run_id,fetched_at=excluded.fetched_at""",
            (snapshot_id, PROVIDER_ID, run_id, str(package_path), package_sha,
             package_path.stat().st_size, now, member),
        )
        for record in inventory["records"]:
            matches = _candidate_matches(connection, record)
            variant_ids = sorted({row["card_variant_id"] for row in matches})
            if len(variant_ids) != 1:
                raise RuntimeError(
                    f"{record['canonical_identity']} maps to {len(variant_ids)} card variants via recorded URLs"
                )
            attempted_urls = {
                attempt.get("url") for attempt in record.get("previously_attempted_sources") or []
                if attempt.get("url")
            }
            matched_urls = {row["source_url"] for row in matches}
            if attempted_urls != matched_urls:
                raise RuntimeError(
                    f"{record['canonical_identity']} has unmapped attempted image URLs"
                )
            variant_id = variant_ids[0]
            raw = canonical_json(record)
            source_id = stable_id(PROVIDER_ID, record["canonical_identity"], digest(raw)[:16])
            connection.execute(
                """insert into source_record values (?, ?, ?, ?, 'missing_image_record', ?, null, ?, ?, ?, null)
                on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
                (source_id, PROVIDER_ID, run_id, snapshot_id, record["canonical_identity"],
                 f"{member}#{record['canonical_identity']}", digest(raw), raw),
            )
            connection.execute(
                """insert or replace into provider_entity_mapping values
                (?, 'missing_image_record', ?, 'card_variant', ?, 'recorded_url_exact', 'verified', ?, ?)""",
                (PROVIDER_ID, record["canonical_identity"], variant_id, source_id,
                 canonical_json({"candidate_ids": [row["id"] for row in matches]})),
            )
            for match in matches:
                connection.execute(
                    "update card_image_candidate set validation_status='missing' where id=?",
                    (match["id"],),
                )
                counters["candidate_urls_marked_missing"] += 1
            issue_class = (
                "card_image_identity_review"
                if record.get("phase9_category") == "manual_review"
                else "missing_card_image"
            )
            status = "needs_review" if issue_class == "card_image_identity_review" else "open"
            unresolved_id = stable_id(PROVIDER_ID, issue_class, variant_id)
            evidence = {
                "source_record_id": source_id,
                "canonical_identity": record["canonical_identity"],
                "canonical_card_id": record.get("canonical_card_id"),
                "failure_reason": record.get("failure_reason"),
                "previously_attempted_sources": record.get("previously_attempted_sources") or [],
                "required_replacement_evidence": record.get("required_replacement_evidence") or [],
                "collision_risks": record.get("collision_risks") or [],
                "provenance_stream": record.get("provenance_stream"),
                "package_sha256": package_sha,
                "package_member": member,
            }
            connection.execute(
                """insert into unresolved_item values (?, 'card_variant', ?, ?, ?, ?, ?, ?, ?, 0)
                on conflict(entity_type,entity_id,language_code,issue_class) do update set
                summary=excluded.summary,evidence_json=excluded.evidence_json,status=excluded.status,
                externally_unavoidable=0""",
                (unresolved_id, variant_id, record.get("language"), REGIONS.get(record.get("region")),
                 issue_class, record.get("failure_reason") or "Card image unavailable",
                 canonical_json(evidence), status),
            )
            counters["records"] += 1
            counters[issue_class] += 1
        connection.execute(
            "update import_run set status='completed',counters_json=?,checkpoint_json=?,completed_at=? where id=?",
            (canonical_json(dict(counters)), canonical_json({
                "complete": True, "package_member": member, "schema_version": inventory.get("schema_version"),
            }), datetime.now(timezone.utc).isoformat(), run_id),
        )
        connection.commit()
        return dict(counters)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
