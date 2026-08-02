"""Classify same-collector groups without collapsing provider-distinct printings."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json, stable_id


def _provider_number_conflict(provider_id: str, provider_record_id: str, collector_number: str) -> bool:
    if provider_id != "pokemontcg-data":
        return False
    last = provider_record_id.rsplit("-", 1)[-1].split("_", 1)[0]
    if not last.isdigit() or not collector_number.isdigit():
        return False
    return int(last) != int(collector_number)


def classify_collisions(database: Path) -> dict[str, int]:
    connection = connect(str(database))
    counters: Counter[str] = Counter()
    try:
        groups = connection.execute(
            """select set_release_id,collector_number,count(*) rows
                 from card_printing group by set_release_id,collector_number having count(*)>1
                 order by set_release_id,collector_number"""
        ).fetchall()
        for group in groups:
            release = connection.execute(
                "select language_code,region_code,local_name from set_release where id=?",
                (group["set_release_id"],),
            ).fetchone()
            rows = connection.execute(
                """select cp.id,cp.local_printing_key,cd.canonical_name,src.provider_id,
                          src.provider_record_id,src.source_sha256,src.raw_payload_json,
                          group_concat(distinct cic.source_url) image_urls
                     from card_printing cp join card_design cd on cd.id=cp.card_design_id
                     join source_record src on src.id=cp.source_record_id
                     left join card_variant cv on cv.card_printing_id=cp.id
                     left join card_image_candidate cic on cic.card_variant_id=cv.id
                    where cp.set_release_id=? and cp.collector_number=?
                    group by cp.id order by cp.id""",
                (group["set_release_id"], group["collector_number"]),
            ).fetchall()
            evidence_rows = []
            provider_number_conflicts = []
            provider_ids = set()
            source_hashes = set()
            image_urls = set()
            for row in rows:
                provider_identity = (row["provider_id"], row["provider_record_id"])
                provider_ids.add(provider_identity)
                source_hashes.add(row["source_sha256"])
                images = sorted(filter(None, (row["image_urls"] or "").split(",")))
                image_urls.update(images)
                conflict = _provider_number_conflict(
                    row["provider_id"], row["provider_record_id"], group["collector_number"],
                )
                if conflict:
                    provider_number_conflicts.append(row["id"])
                raw = json.loads(row["raw_payload_json"] or "{}")
                evidence_rows.append({
                    "printing_id": row["id"], "local_printing_key": row["local_printing_key"],
                    "canonical_name": row["canonical_name"], "provider_id": row["provider_id"],
                    "provider_record_id": row["provider_record_id"], "source_sha256": row["source_sha256"],
                    "image_urls": images, "source_image_hash": raw.get("hash") if isinstance(raw, dict) else None,
                    "provider_number_conflict": conflict,
                })
            all_source_distinct = len(provider_ids) == len(rows) and len(source_hashes) == len(rows)
            images_distinct = len(image_urls) >= len(rows)
            if provider_number_conflicts:
                status = "needs_review"
                classification = "provider_id_collector_number_conflict"
                summary = "Provider record identity conflicts with its reported collector number"
                counters["needs_review"] += 1
            elif all_source_distinct:
                status = "classified_nonblocking"
                classification = "provider_distinct_printings_same_reported_collector"
                if images_distinct:
                    classification = "provider_and_image_distinct_printings_same_reported_collector"
                summary = "Same collector number is intentionally preserved across distinct provider records"
                counters["classified_nonblocking"] += 1
            else:
                status = "needs_review"
                classification = "insufficient_distinguishing_evidence"
                summary = "Same-collector records lack complete provider-distinguishing evidence"
                counters["needs_review"] += 1
            entity_id = f"{group['set_release_id']}|{group['collector_number']}"
            unresolved_id = stable_id("collector-collision", entity_id)
            evidence = canonical_json({
                "classification": classification, "set_release_id": group["set_release_id"],
                "set_name": release["local_name"], "collector_number": group["collector_number"],
                "printing_count": len(rows), "all_source_records_distinct": all_source_distinct,
                "all_images_distinct": images_distinct, "rows": evidence_rows,
            })
            connection.execute(
                """insert into unresolved_item values (?, 'collector_collision_group', ?, ?, ?,
                   'collector_number_collision', ?, ?, ?, 0)
                   on conflict(id) do update set summary=excluded.summary,evidence_json=excluded.evidence_json,
                    status=excluded.status""",
                (unresolved_id, entity_id, release["language_code"], release["region_code"],
                 summary, evidence, status),
            )
            counters["groups"] += 1
            counters["printing_rows"] += len(rows)
        connection.commit()
        return dict(counters)
    finally:
        connection.close()

