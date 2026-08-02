"""Resumable acquisition and technical validation for card-image candidates."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from .product_image_validation import (
    CHECKPOINT_SCHEMA as ASSET_CHECKPOINT_SCHEMA,
    VALIDATOR_VERSION,
    acquire,
    checkpoint_counts,
)
from .schema import connect
from .tcgdex import canonical_json, stable_id

VALIDATOR = "cardscanr-card-image-technical"
CHECKPOINT_SCHEMA = ASSET_CHECKPOINT_SCHEMA.replace(
    "variant_id text not null", "variant_id text not null"
)


def register_candidates(database: Path, checkpoint: Path) -> dict[str, int]:
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    staging = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    progress = sqlite3.connect(checkpoint)
    try:
        progress.executescript(CHECKPOINT_SCHEMA)
        rows = staging.execute(
            "select id,card_variant_id,provider_id,source_url from card_image_candidate order by id"
        ).fetchall()
        with progress:
            for candidate_id, variant_id, provider_id, source_url in rows:
                progress.execute("insert or ignore into assets(source_url) values (?)", (source_url,))
                progress.execute("insert or replace into candidates values (?,?,?,?)",
                                 (candidate_id, variant_id, provider_id, source_url))
        return {"candidates": len(rows), "distinct_urls": progress.execute("select count(*) from assets").fetchone()[0]}
    finally:
        staging.close()
        progress.close()


def apply_results(database: Path, checkpoint: Path) -> dict[str, int]:
    progress = sqlite3.connect(f"file:{checkpoint.resolve()}?mode=ro", uri=True)
    progress.row_factory = sqlite3.Row
    staging = connect(str(database))
    staging.row_factory = sqlite3.Row
    counters: Counter[str] = Counter()
    try:
        rows = progress.execute(
            """select c.*,a.status,a.attempted_at,a.http_status,a.content_type,a.byte_size,a.sha256,
                      a.cache_path,a.result_json,a.error
                 from candidates c join assets a on a.source_url=c.source_url
                where a.status!='pending' order by c.candidate_id"""
        ).fetchall()
        with staging:
            for row in rows:
                status = row["status"]
                outcome = {"pass": "acquired", "not_found": "not_found", "fail": "invalid",
                           "retryable_error": "retryable_error"}[status]
                mapped = staging.execute(
                    """select exists(select 1 from card_image_candidate cic
                         join source_record sr on sr.id=cic.source_record_id
                         join provider_entity_mapping pem on pem.provider_id=sr.provider_id
                          and pem.provider_record_id=sr.provider_record_id
                          and pem.entity_type='card_variant' and pem.entity_id=cic.card_variant_id
                        where cic.id=?)""", (row["candidate_id"],),
                ).fetchone()[0]
                technical = json.loads(row["result_json"] or "{}")
                evidence = {
                    "http_status": row["http_status"], "content_type": row["content_type"],
                    "cache_path": row["cache_path"], "technical": technical, "error": row["error"],
                    "provider_identity_mapping": bool(mapped), "rights_decision": "preserved_from_candidate",
                }
                attempt_id = stable_id("card-image-attempt", row["candidate_id"], VALIDATOR)
                staging.execute(
                    """insert into image_acquisition_attempt values (?,?,?,?,?,?,?,?,?)
                       on conflict(id) do update set attempted_at=excluded.attempted_at,http_status=excluded.http_status,
                        outcome=excluded.outcome,evidence_json=excluded.evidence_json""",
                    (attempt_id, "card_variant", row["variant_id"], row["provider_id"], row["source_url"],
                     row["attempted_at"], row["http_status"], outcome, canonical_json(evidence)),
                )
                validation_status = "pass" if status == "pass" and mapped else (
                    "warning" if status in ("pass", "retryable_error") else "fail"
                )
                validation_id = stable_id("card-image-validation", row["candidate_id"], VALIDATOR)
                staging.execute(
                    """insert into image_validation_result values (?,?,null,?,?,?,?,?)
                       on conflict(id) do update set status=excluded.status,checks_json=excluded.checks_json,
                        checked_at=excluded.checked_at""",
                    (validation_id, row["candidate_id"], VALIDATOR, VALIDATOR_VERSION, validation_status,
                     canonical_json({
                         "http_availability": {"status": "pass" if status == "pass" else validation_status,
                                               "http_status": row["http_status"]},
                         "decode_dimensions_and_hashes": {"status": "pass" if status == "pass" else validation_status,
                                                          **evidence},
                         "identity_match": {"status": "pass" if mapped else "warning",
                                            "provider_entity_mapping": bool(mapped)},
                         "watermark_or_seller_background": {"status": "not_applicable",
                                                              "basis": "official provider asset"},
                     }), row["attempted_at"]),
                )
                if validation_status == "pass":
                    staging.execute("update card_image_candidate set validation_status='verified' where id=?",
                                    (row["candidate_id"],))
                    staging.execute(
                        """update unresolved_item set status='resolved'
                           where entity_type='card_variant' and entity_id=? and issue_class='missing_card_image'""",
                        (row["variant_id"],),
                    )
                elif status in ("not_found", "fail"):
                    staging.execute("update card_image_candidate set validation_status='invalid' where id=?",
                                    (row["candidate_id"],))
                    unresolved = staging.execute(
                        """select id,evidence_json from unresolved_item where entity_type='card_variant'
                           and entity_id=? and issue_class='missing_card_image'""", (row["variant_id"],),
                    ).fetchall()
                    for item in unresolved:
                        existing = json.loads(item["evidence_json"] or "{}")
                        existing["official_image_validation"] = evidence
                        staging.execute(
                            "update unresolved_item set status='needs_review',evidence_json=? where id=?",
                            (canonical_json(existing), item["id"]),
                        )
                counters[status] += 1
                if not mapped:
                    counters["identity_mapping_warnings"] += 1
        counters["applied"] = len(rows)
        return dict(counters)
    finally:
        staging.close()
        progress.close()


__all__ = ["acquire", "apply_results", "checkpoint_counts", "register_candidates"]

