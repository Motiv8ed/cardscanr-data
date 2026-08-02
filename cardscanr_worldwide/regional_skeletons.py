"""Derive conservative regional printing rosters from exact TCGdex set evidence.

This module deliberately creates no localised card text and no image candidates.  A
row is eligible only when TCGdex names the target-language release and its declared
official count exactly equals the populated English sibling roster.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .schema import connect
from .tcgdex import canonical_json, stable_id

PROVIDER_ID = "cardscanr-regional-roster-derivations"
SOURCE_PROVIDER_ID = "tcgdex-cards-database"
ALGORITHM_VERSION = "1.0.0"
DEFAULT_LANGUAGES = ("de", "es", "it", "nl", "pl", "pt", "ru")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _eligible_releases(
    connection: sqlite3.Connection, languages: Iterable[str]
) -> list[sqlite3.Row]:
    requested = tuple(dict.fromkeys(languages))
    if not requested:
        return []
    placeholders = ",".join("?" for _ in requested)
    rows = connection.execute(
        f"""select target.*, target_source.raw_payload_json target_payload,
                          target_source.source_sha256 target_source_sha256,
                          english.id reference_release_id,
                          count(reference.id) reference_count
             from set_release target
             join source_record target_source on target_source.id=target.source_record_id
             join set_release english
               on english.card_set_id=target.card_set_id
              and english.language_code='en' and english.region_code='INTL'
             join card_printing reference on reference.set_release_id=english.id
            where target_source.provider_id=?
              and target.language_code in ({placeholders})
              and target.official_count is not null and target.official_count > 0
              and not exists (
                    select 1 from card_printing existing
                     where existing.set_release_id=target.id
              )
            group by target.id, english.id
           having count(reference.id)=target.official_count
            order by target.language_code, target.region_code, target.release_code, target.id""",
        (SOURCE_PROVIDER_ID, *requested),
    ).fetchall()
    eligible: list[sqlite3.Row] = []
    for row in rows:
        try:
            payload = json.loads(row["target_payload"] or "{}")
        except json.JSONDecodeError:
            continue
        names = payload.get("name") if isinstance(payload, dict) else None
        if isinstance(names, dict) and names.get(row["language_code"]):
            eligible.append(row)
    return eligible


def import_regional_skeletons(
    database: Path, languages: Iterable[str] = DEFAULT_LANGUAGES
) -> dict[str, int]:
    connection = connect(str(database))
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    counters: Counter[str] = Counter()
    try:
        releases = _eligible_releases(connection, languages)
        manifest = canonical_json([
            {
                "target_release_id": row["id"],
                "target_source_sha256": row["target_source_sha256"],
                "reference_release_id": row["reference_release_id"],
                "official_count": row["official_count"],
            }
            for row in releases
        ])
        manifest_sha = _sha256(manifest)
        snapshot_id = stable_id(PROVIDER_ID, "snapshot", manifest_sha[:24])
        connection.execute(
            """insert into source_provider values (?, ?, 'internal_derivation', ?, ?, ?, ?, ?)
               on conflict(id) do update set source_version=excluded.source_version""",
            (PROVIDER_ID, "CardScanR regional roster derivations", "generated://cardscanr/regional-rosters",
             "internal_audit", "Derived from exact localized TCGdex set evidence; no card text or images inferred",
             "https://github.com/tcgdex/cards-database", ALGORITHM_VERSION),
        )
        connection.execute(
            "insert into import_run values (?, ?, 'running', ?, ?, '{}', '{}', ?, null, null)",
            (run_id, PROVIDER_ID, str(database), manifest_sha, now),
        )
        connection.execute(
            """insert into source_snapshot values (?, ?, ?, ?, ?, ?, ?, ?, ?)
               on conflict(id) do update set import_run_id=excluded.import_run_id,
                 source_version=excluded.source_version, fetched_at=excluded.fetched_at""",
            (snapshot_id, PROVIDER_ID, run_id, "generated://cardscanr/regional-rosters/manifest.json",
             manifest_sha, ALGORITHM_VERSION, len(manifest.encode("utf-8")), now,
             "generated://cardscanr/regional-rosters/manifest.json"),
        )
        for target in releases:
            reference_rows = connection.execute(
                """select * from card_printing where set_release_id=?
                    order by local_printing_key,id""",
                (target["reference_release_id"],),
            ).fetchall()
            if len(reference_rows) != target["official_count"]:
                raise RuntimeError(f"reference roster changed during import: {target['id']}")
            for reference in reference_rows:
                evidence = {
                    "algorithm_version": ALGORITHM_VERSION,
                    "derivation": "exact_localized_set_plus_equal_count_english_roster",
                    "target_release_id": target["id"],
                    "target_set_source_sha256": target["target_source_sha256"],
                    "reference_release_id": target["reference_release_id"],
                    "reference_printing_id": reference["id"],
                    "reference_source_record_id": reference["source_record_id"],
                    "official_count": target["official_count"],
                    "limitations": [
                        "local card name and rules text not supplied",
                        "regional physical variant not classified",
                        "regional card image not supplied",
                        "copied structural metadata remains provisional",
                    ],
                }
                raw = canonical_json(evidence)
                provider_record_id = f"{target['id']}/{reference['local_printing_key']}"
                source_id = stable_id(PROVIDER_ID, "printing", provider_record_id, _sha256(raw)[:16])
                connection.execute(
                    """insert into source_record values (?, ?, ?, ?, 'derived_card_roster', ?, ?, ?, ?, ?, null)
                       on conflict(id) do update set import_run_id=excluded.import_run_id,
                         snapshot_id=excluded.snapshot_id""",
                    (source_id, PROVIDER_ID, run_id, snapshot_id, provider_record_id,
                     target["id"], f"derived:regional-roster/{provider_record_id}", _sha256(raw), raw),
                )
                printing_id = stable_id(PROVIDER_ID, target["id"], reference["local_printing_key"])
                connection.execute(
                    """insert into card_printing values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       'provisional', ?)
                       on conflict(id) do update set source_record_id=excluded.source_record_id,
                         verification_status=excluded.verification_status, raw_card_json=excluded.raw_card_json""",
                    (printing_id, reference["card_design_id"], target["id"], source_id,
                     reference["collector_number"], reference["local_printing_key"], reference["rarity"],
                     reference["illustrator"], reference["supertype"], reference["stage"], reference["hp"],
                     reference["types_json"], reference["regulation_mark"], reference["retreat_json"],
                     reference["weaknesses_json"], reference["resistances_json"], raw),
                )
                variant_id = stable_id(printing_id, "regional-variant-unclassified")
                connection.execute(
                    """insert or replace into card_variant values (?, ?, 'regional-variant-unclassified',
                       null, null, null, null, 0, ?, 'unknown')""",
                    (variant_id, printing_id, canonical_json({
                        "normalization_note": "Regional release is evidenced, but its physical finish/stamp variant is not classified"
                    })),
                )
                connection.execute(
                    """insert or replace into provider_entity_mapping values (?, 'derived_card_roster', ?,
                       'card_printing', ?, 'exact_set_and_equal_reference_roster', 'candidate', ?, ?)""",
                    (PROVIDER_ID, provider_record_id, printing_id, source_id, raw),
                )
                issues = (
                    ("regional_card_metadata_unverified", "Local card name, rules text, and region-specific metadata require an exact source"),
                    ("regional_variant_unclassified", "The regional printing is evidenced but its physical finish or stamp variant is unknown"),
                    ("regional_card_image_missing", "No exact image for this regional printing has been supplied"),
                )
                for issue_class, summary in issues:
                    unresolved_id = stable_id(PROVIDER_ID, issue_class, printing_id)
                    connection.execute(
                        """insert or replace into unresolved_item values (?, 'card_printing', ?, ?, ?, ?, ?, ?, 'open', 0)""",
                        (unresolved_id, printing_id, target["language_code"], target["region_code"],
                         issue_class, summary, raw),
                    )
                counters["printings"] += 1
                counters["variants"] += 1
                counters["unresolved_items"] += 3
            counters["releases"] += 1
            counters[f"language_{target['language_code']}"] += 1
        connection.execute(
            "update import_run set status='completed',counters_json=?,checkpoint_json=?,completed_at=? where id=?",
            (canonical_json(dict(counters)), canonical_json({"complete": True, "manifest_sha256": manifest_sha}),
             datetime.now(timezone.utc).isoformat(), run_id),
        )
        connection.commit()
        return dict(counters)
    except Exception as error:
        connection.rollback()
        connection.execute(
            "update import_run set status='failed',error_summary=?,completed_at=? where id=?",
            (f"{type(error).__name__}: {error}", datetime.now(timezone.utc).isoformat(), run_id),
        )
        connection.commit()
        raise
    finally:
        connection.close()

