from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .builder import _content_fingerprint, sha256_file
from .catalogue_reader import collect_catalogue_snapshot
from .constants import (
    DATABASE_BASENAME,
    DEFAULT_CATALOGUE_ROOT,
    MANIFEST_BASENAME,
    PREVIOUS_DATABASE_BASENAME,
    SEARCH_OUTPUT_DIR,
    SHA256_BASENAME,
)
from .search import SearchRequest, connect_readonly, lookup_exact_identity, search_cards


REQUIRED_TABLES = ("meta", "sets", "cards", "card_aliases")
REQUIRED_INDEXES = (
    "idx_cards_language",
    "idx_cards_set_collector",
    "idx_cards_name",
    "idx_cards_localized_name",
    "idx_cards_set_name",
    "idx_cards_set_name_canon",
    "idx_cards_set_name_localized",
    "idx_cards_physical_printing",
    "idx_cards_base_reference",
    "idx_aliases_normalized",
)
REQUIRED_PHYSICAL_COLUMNS = (
    "physical_printing_id",
    "identity_model_version",
    "base_card_reference",
    "printing_class",
    "product_family",
    "variant_signature",
)


@dataclass(frozen=True)
class VerifyResult:
    passed: bool
    issues: list[str]
    total_rows: int
    per_language_counts: dict[str, int]
    duplicate_canonical_ids: int
    duplicate_physical_printing_ids: int
    missing_physical_printing_ids: int
    manifest_sha256_matches: bool
    deterministic_rebuild_matches: bool
    rollback_available: bool
    fts_healthy: bool


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def verify_search_index(
    *,
    output_dir: Path = SEARCH_OUTPUT_DIR,
    catalogue_root: Path = DEFAULT_CATALOGUE_ROOT,
    expected_fingerprint: str | None = None,
) -> VerifyResult:
    issues: list[str] = []
    db_path = output_dir / DATABASE_BASENAME
    manifest_path = output_dir / MANIFEST_BASENAME
    sha256_path = output_dir / SHA256_BASENAME
    previous_db = output_dir / PREVIOUS_DATABASE_BASENAME

    if not db_path.exists():
        issues.append("database_missing")
    if not manifest_path.exists():
        issues.append("manifest_missing")
    if not sha256_path.exists():
        issues.append("sha256_sidecar_missing")

    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    manifest_sha256_matches = False
    if db_path.exists() and manifest.get("sha256"):
        actual = sha256_file(db_path)
        sidecar = sha256_path.read_text(encoding="utf-8").strip() if sha256_path.exists() else ""
        manifest_sha256_matches = actual == manifest["sha256"] == sidecar
        if not manifest_sha256_matches:
            issues.append("sha256_mismatch")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    for table in REQUIRED_TABLES:
        if not _table_exists(conn, table):
            issues.append(f"missing_table:{table}")
    if not _table_exists(conn, "cards_fts"):
        issues.append("missing_fts_table")
    for index in REQUIRED_INDEXES:
        if not _index_exists(conn, index):
            issues.append(f"missing_index:{index}")

    card_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(cards)")}
    for column in REQUIRED_PHYSICAL_COLUMNS:
        if column not in card_columns:
            issues.append(f"missing_physical_column:{column}")

    fts_healthy = False
    try:
        conn.execute("SELECT COUNT(*) FROM cards_fts").fetchone()
        fts_healthy = True
    except sqlite3.DatabaseError:
        issues.append("fts_unhealthy")

    total_rows = int(conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
    per_language = {
        str(row["language"]): int(row["count"])
        for row in conn.execute("SELECT language, COUNT(*) AS count FROM cards GROUP BY language")
    }
    duplicate_canonical_ids = int(
        conn.execute(
            "SELECT COUNT(*) FROM (SELECT canonical_base_id FROM cards GROUP BY canonical_base_id HAVING COUNT(*) > 1)"
        ).fetchone()[0]
    )
    if duplicate_canonical_ids:
        issues.append("duplicate_canonical_base_id")

    duplicate_physical_printing_ids = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT physical_printing_id FROM cards
              WHERE physical_printing_id IS NOT NULL AND trim(physical_printing_id) <> ''
              GROUP BY physical_printing_id HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
    )
    if duplicate_physical_printing_ids:
        issues.append("duplicate_physical_printing_id")

    missing_physical_printing_ids = int(
        conn.execute(
            "SELECT COUNT(*) FROM cards WHERE physical_printing_id IS NULL OR trim(physical_printing_id) = ''"
        ).fetchone()[0]
    )
    if missing_physical_printing_ids:
        issues.append(f"missing_physical_printing_id:{missing_physical_printing_ids}")

    snapshot = collect_catalogue_snapshot(catalogue_root)
    if total_rows != snapshot.total_cards:
        issues.append(f"row_count_mismatch expected={snapshot.total_cards} actual={total_rows}")
    for language, expected in snapshot.per_language_counts.items():
        if per_language.get(language, 0) != expected:
            issues.append(f"language_count_mismatch:{language}")

    fingerprint = _content_fingerprint(db_path)
    deterministic_rebuild_matches = expected_fingerprint is None or fingerprint == expected_fingerprint
    if expected_fingerprint and fingerprint != expected_fingerprint:
        issues.append("deterministic_rebuild_mismatch")

    rollback_available = previous_db.exists() or manifest.get("previousSha256") is not None

    # Query contract smoke checks without remote providers.
    readonly = connect_readonly(str(db_path))
    if not search_cards(readonly, SearchRequest(query_text="charizard", language="en", limit=5)):
        issues.append("smoke_search_failed")
    if lookup_exact_identity(readonly, language="en", set_id="base1", collector_number="4") is None:
        issues.append("smoke_exact_lookup_failed")
    readonly.close()
    conn.close()

    return VerifyResult(
        passed=not issues,
        issues=issues,
        total_rows=total_rows,
        per_language_counts=per_language,
        duplicate_canonical_ids=duplicate_canonical_ids,
        duplicate_physical_printing_ids=duplicate_physical_printing_ids,
        missing_physical_printing_ids=missing_physical_printing_ids,
        manifest_sha256_matches=manifest_sha256_matches,
        deterministic_rebuild_matches=deterministic_rebuild_matches,
        rollback_available=rollback_available,
        fts_healthy=fts_healthy,
    )
