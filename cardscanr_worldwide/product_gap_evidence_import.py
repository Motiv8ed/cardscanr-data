"""Import explicitly corroborated sealed-product gap evidence."""

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


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def import_evidence(database: Path, evidence_path: Path) -> dict[str, int]:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence.get("schema_version") != 1 or not evidence.get("records"):
        raise ValueError("Unsupported or empty product-gap evidence")
    provider = evidence["provider"]
    provider_id = provider["id"]
    snapshot_sha = file_sha256(evidence_path)
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    snapshot_id = stable_id(provider_id, "snapshot", snapshot_sha[:24])
    counters: Counter[str] = Counter()
    connection = connect(str(database))
    try:
        connection.execute(
            """insert into source_provider values (?, ?, 'community_corroboration', ?, 'metadata_only', ?, null, null)
               on conflict(id) do update set rights_status=excluded.rights_status,
                attribution_text=excluded.attribution_text""",
            (provider_id, provider["name"], provider["homepage"], provider["attribution"]),
        )
        connection.execute(
            "insert into import_run values (?, ?, 'running', ?, ?, '{}', '{}', ?, null, null)",
            (run_id, provider_id, str(evidence_path), snapshot_sha, now),
        )
        connection.execute(
            """insert into source_snapshot values (?, ?, ?, ?, ?, null, ?, ?, ?)
               on conflict(id) do update set import_run_id=excluded.import_run_id,fetched_at=excluded.fetched_at""",
            (snapshot_id, provider_id, run_id, str(evidence_path), snapshot_sha,
             evidence_path.stat().st_size, now, str(evidence_path.parent)),
        )
        related_ids: set[str] = set()
        for record in evidence["records"]:
            record_id = record["provider_record_id"]
            payload = canonical_json(record)
            source_id = stable_id(provider_id, "sealed_product", record_id, digest(payload)[:16])
            source_url = next(source["url"] for source in record["sources"] if source["role"] == "official_identity")
            connection.execute(
                """insert into source_record values (?, ?, ?, ?, 'sealed_product', ?, null, ?, ?, ?, null)
                   on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
                (source_id, provider_id, run_id, snapshot_id, record_id, source_url, digest(payload), payload),
            )
            product_id = stable_id(provider_id, "sealed", record_id)
            variant_id = stable_id(product_id, "en", "US", "standard")
            connection.execute(
                """insert into sealed_product values (?, ?, ?, ?, ?, ?, 'corroborated', ?)
                   on conflict(id) do update set source_record_id=excluded.source_record_id,
                    canonical_name=excluded.canonical_name,product_type=excluded.product_type,
                    verification_status='corroborated',raw_product_json=excluded.raw_product_json""",
                (product_id, provider_id, record_id, source_id, record["name"], record["product_type"], payload),
            )
            connection.execute(
                """insert into sealed_product_variant values (?, ?, 'en', 'US', ?, 'standard', ?, ?)
                   on conflict(id) do update set local_name=excluded.local_name,release_date=excluded.release_date,
                    attributes_json=excluded.attributes_json""",
                (variant_id, product_id, record["name"], record.get("release_date"), canonical_json({
                    "evidence_sources": record["sources"], "verification_basis": "multi-source_corroboration",
                })),
            )
            connection.execute("delete from product_content where sealed_product_variant_id=?", (variant_id,))
            for ordinal, content in enumerate(record.get("contents") or []):
                connection.execute(
                    "insert into product_content values (?, ?, ?, null, ?, ?, ?)",
                    (variant_id, ordinal, content["type"], content["name"], content.get("quantity", 1),
                     canonical_json({key: value for key, value in content.items()
                                     if key not in {"type", "name", "quantity"}})),
                )
                counters["product_contents"] += 1
            connection.execute(
                """insert or replace into provider_entity_mapping values (?, 'sealed_product', ?,
                   'sealed_product', ?, 'corroborated_official_identity', 'verified', ?, ?)""",
                (provider_id, record_id, product_id, source_id,
                 canonical_json({"related_archive_record_id": record["related_archive_record_id"]})),
            )
            related_ids.add(record["related_archive_record_id"])
            counters["products"] += 1
        for archive_id in related_ids:
            connection.execute(
                """update unresolved_item set status='resolved'
                   where entity_type='source_product' and entity_id=?
                     and issue_class='official_archive_collection_error'""", (archive_id,),
            )
        connection.execute(
            "update import_run set status='completed',counters_json=?,checkpoint_json=?,completed_at=? where id=?",
            (canonical_json(dict(counters)), canonical_json({"complete": True}), now, run_id),
        )
        connection.commit()
        return dict(counters)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
