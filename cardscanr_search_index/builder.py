from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalogue_reader import CardRecord, collect_catalogue_snapshot, iter_catalogue_cards, iter_set_records
from .constants import (
    CATALOGUE_SCHEMA_VERSION,
    DATABASE_BASENAME,
    DEFAULT_CATALOGUE_ROOT,
    GENERATOR_VERSION,
    MANIFEST_BASENAME,
    MINIMUM_COMPATIBLE_APP_VERSION,
    MINIMUM_COMPATIBLE_APP_VERSION_STATUS,
    PREVIOUS_DATABASE_BASENAME,
    SEARCH_INDEX_SCHEMA_VERSION,
    SEARCH_OUTPUT_DIR,
    SHA256_BASENAME,
    SUPPORTED_LANGUAGES,
)


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE sets (
  set_id TEXT NOT NULL,
  language TEXT NOT NULL,
  name TEXT NOT NULL,
  normalized_set_name TEXT NOT NULL,
  total INTEGER,
  printed_total INTEGER,
  release_date TEXT,
  ptcgo_code TEXT,
  series TEXT,
  PRIMARY KEY (language, set_id)
);

CREATE TABLE cards (
  id INTEGER PRIMARY KEY,
  schema_version TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  canonical_base_id TEXT NOT NULL UNIQUE,
  canonical_english_name TEXT,
  localized_name TEXT,
  normalized_canonical_name TEXT NOT NULL,
  normalized_localized_name TEXT NOT NULL,
  language TEXT NOT NULL,
  set_id TEXT NOT NULL,
  set_name TEXT NOT NULL,
  normalized_set_name TEXT NOT NULL,
  provider_set_codes_json TEXT NOT NULL,
  collector_number TEXT NOT NULL,
  normalized_collector_number TEXT NOT NULL,
  local_number TEXT,
  set_total INTEGER,
  rarity TEXT,
  thumbnail_url TEXT,
  large_image_url TEXT,
  image_source TEXT,
  image_cached INTEGER NOT NULL,
  provider_ids_json TEXT NOT NULL,
  promotion_provider_set_id TEXT,
  release_date TEXT,
  set_release_date TEXT,
  set_ptcgo_code TEXT,
  search_aliases_json TEXT NOT NULL
);

CREATE TABLE card_aliases (
  canonical_base_id TEXT NOT NULL,
  alias TEXT NOT NULL,
  normalized_alias TEXT NOT NULL,
  alias_kind TEXT NOT NULL,
  PRIMARY KEY (canonical_base_id, normalized_alias, alias_kind),
  FOREIGN KEY (canonical_base_id) REFERENCES cards(canonical_base_id)
);

CREATE VIRTUAL TABLE cards_fts USING fts5(
  canonical_base_id UNINDEXED,
  search_text,
  tokenize='unicode61 remove_diacritics 0'
);

CREATE INDEX idx_cards_language ON cards(language);
CREATE INDEX idx_cards_set_collector ON cards(language, set_id, normalized_collector_number);
CREATE INDEX idx_cards_name ON cards(language, normalized_canonical_name);
CREATE INDEX idx_cards_localized_name ON cards(language, normalized_localized_name);
CREATE INDEX idx_cards_set_name ON cards(language, normalized_set_name);
CREATE INDEX idx_cards_set_name_canon ON cards(language, normalized_set_name, normalized_canonical_name);
CREATE INDEX idx_cards_set_name_localized ON cards(language, normalized_set_name, normalized_localized_name);
CREATE INDEX idx_aliases_normalized ON card_aliases(normalized_alias);
"""


@dataclass(frozen=True)
class BuildResult:
    database_path: Path
    manifest_path: Path
    sha256_path: Path
    build_time_seconds: float
    total_cards: int
    per_language_counts: dict[str, int]
    database_bytes: int
    sha256: str
    content_fingerprint: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def git_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "unknown"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _insert_aliases(conn: sqlite3.Connection, record: CardRecord) -> None:
    rows = []
    for alias in record.search_aliases:
        rows.append((record.canonical_base_id, alias, alias.casefold(), "catalogue"))
    conn.executemany(
        "INSERT OR IGNORE INTO card_aliases (canonical_base_id, alias, normalized_alias, alias_kind) VALUES (?, ?, ?, ?)",
        rows,
    )


def _insert_card(conn: sqlite3.Connection, record: CardRecord) -> None:
    conn.execute(
        """
        INSERT INTO cards (
          schema_version, generated_at, canonical_base_id, canonical_english_name, localized_name,
          normalized_canonical_name, normalized_localized_name, language, set_id, set_name,
          normalized_set_name, provider_set_codes_json, collector_number, normalized_collector_number,
          local_number, set_total, rarity, thumbnail_url, large_image_url, image_source, image_cached,
          provider_ids_json, promotion_provider_set_id, release_date, set_release_date, set_ptcgo_code,
          search_aliases_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.schema_version,
            record.generated_at,
            record.canonical_base_id,
            record.canonical_english_name,
            record.localized_name,
            record.normalized_canonical_name,
            record.normalized_localized_name,
            record.language,
            record.set_id,
            record.set_name,
            record.normalized_set_name,
            json.dumps(record.provider_set_codes, ensure_ascii=False),
            record.collector_number,
            record.normalized_collector_number,
            record.local_number,
            record.set_total,
            record.rarity,
            record.thumbnail_url,
            record.large_image_url,
            record.image_source,
            1 if record.image_cached else 0,
            record.provider_ids_json,
            record.promotion_provider_set_id,
            record.release_date,
            record.set_release_date,
            record.set_ptcgo_code,
            json.dumps(record.search_aliases, ensure_ascii=False),
        ),
    )
    search_text = " ".join(
        [
            record.canonical_english_name or "",
            record.localized_name or "",
            record.normalized_canonical_name,
            record.normalized_localized_name,
            record.set_name,
            record.normalized_set_name,
            record.collector_number,
            record.normalized_collector_number,
            " ".join(record.provider_set_codes),
            " ".join(record.search_aliases),
        ]
    ).strip()
    conn.execute(
        "INSERT INTO cards_fts (canonical_base_id, search_text) VALUES (?, ?)",
        (record.canonical_base_id, search_text),
    )
    _insert_aliases(conn, record)


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if not sidecar.exists():
            continue
        try:
            sidecar.unlink()
        except OSError:
            pass


def _publish_database(temp_db: Path, published_db: Path, previous_db: Path) -> None:
    if published_db.exists():
        if previous_db.exists():
            try:
                previous_db.unlink()
            except OSError:
                pass
        shutil.copy2(published_db, previous_db)
    shutil.copy2(temp_db, published_db)
    _remove_sqlite_sidecars(published_db)
    _remove_sqlite_sidecars(temp_db)
    try:
        temp_db.unlink()
    except OSError:
        pass


def build_search_index(
    *,
    catalogue_root: Path = DEFAULT_CATALOGUE_ROOT,
    output_dir: Path = SEARCH_OUTPUT_DIR,
    root: Path | None = None,
) -> BuildResult:
    root = root or catalogue_root.parents[1]
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_db = output_dir / f"{DATABASE_BASENAME}.tmp"
    if temp_db.exists():
        temp_db.unlink()

    started = time.perf_counter()
    snapshot = collect_catalogue_snapshot(catalogue_root)
    conn = _connect(temp_db)
    conn.executescript(SCHEMA_SQL)
    conn.executemany(
        """
        INSERT INTO sets (set_id, language, name, normalized_set_name, total, printed_total, release_date, ptcgo_code, series)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item.set_id,
                item.language,
                item.name,
                item.normalized_set_name,
                item.total,
                item.printed_total,
                item.release_date,
                item.ptcgo_code,
                item.series,
            )
            for language in SUPPORTED_LANGUAGES
            for item in iter_set_records(catalogue_root, language=language)
        ],
    )
    meta = {
        "schema_version": SEARCH_INDEX_SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "generator_version": GENERATOR_VERSION,
        "git_commit": git_commit(root),
        "catalogue_schema_version": CATALOGUE_SCHEMA_VERSION,
        "supported_languages": ",".join(SUPPORTED_LANGUAGES),
        "total_cards": str(snapshot.total_cards),
    }
    conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", list(meta.items()))

    batch: list[CardRecord] = []
    for record in iter_catalogue_cards(catalogue_root):
        batch.append(record)
        if len(batch) >= 1000:
            with conn:
                for item in batch:
                    _insert_card(conn, item)
            batch.clear()
    if batch:
        with conn:
            for item in batch:
                _insert_card(conn, item)
    conn.execute("INSERT INTO cards_fts(cards_fts) VALUES('optimize')")
    conn.commit()
    conn.close()

    published_db = output_dir / DATABASE_BASENAME
    previous_db = output_dir / PREVIOUS_DATABASE_BASENAME
    previous_sha256 = sha256_file(published_db) if published_db.exists() else None
    _publish_database(temp_db, published_db, previous_db)
    sha256 = sha256_file(published_db)
    content_fingerprint = _content_fingerprint(published_db)

    manifest_path = output_dir / MANIFEST_BASENAME
    previous_manifest = _load_previous_manifest(manifest_path)
    manifest = {
        "catalogueSchemaVersion": CATALOGUE_SCHEMA_VERSION,
        "searchIndexSchemaVersion": SEARCH_INDEX_SCHEMA_VERSION,
        "generatedAt": utc_now_iso(),
        "generatorVersion": GENERATOR_VERSION,
        "gitCommit": git_commit(root),
        "databaseFilename": DATABASE_BASENAME,
        "databaseUrl": f"/v1/catalog/pokemon/search/{DATABASE_BASENAME}",
        "sha256": sha256,
        "byteSize": published_db.stat().st_size,
        "contentFingerprint": content_fingerprint,
        "supportedLanguages": list(SUPPORTED_LANGUAGES),
        "totalCardCount": snapshot.total_cards,
        "perLanguageCounts": snapshot.per_language_counts,
        "minimumCompatibleAppVersion": MINIMUM_COMPATIBLE_APP_VERSION,
        "minimumCompatibleAppVersionStatus": MINIMUM_COMPATIBLE_APP_VERSION_STATUS,
        "previousDatabaseUrl": previous_manifest.get("databaseUrl") if previous_manifest else None,
        "previousSha256": previous_manifest.get("sha256") if previous_manifest else previous_sha256,
        "sourceCatalogueHashes": snapshot.source_hashes,
        "updatePolicy": "atomic_replace_with_manifest_and_sha256_sidecar",
        "rollbackPolicy": "restore catalog_search_v1.previous.sqlite and prior manifest metadata",
    }
    manifest_tmp = manifest_path.with_suffix(".json.tmp")
    manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(manifest_tmp, manifest_path)

    sha256_path = output_dir / SHA256_BASENAME
    sha256_tmp = sha256_path.with_suffix(".tmp")
    sha256_tmp.write_text(sha256 + "\n", encoding="utf-8")
    os.replace(sha256_tmp, sha256_path)

    elapsed = time.perf_counter() - started
    return BuildResult(
        database_path=published_db,
        manifest_path=manifest_path,
        sha256_path=sha256_path,
        build_time_seconds=elapsed,
        total_cards=snapshot.total_cards,
        per_language_counts=snapshot.per_language_counts,
        database_bytes=published_db.stat().st_size,
        sha256=sha256,
        content_fingerprint=content_fingerprint,
    )


def _load_previous_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _content_fingerprint(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT canonical_base_id, language, set_id, collector_number, normalized_canonical_name
        FROM cards
        ORDER BY canonical_base_id
        """
    ).fetchall()
    conn.close()
    digest = hashlib.sha256()
    for row in rows:
        digest.update("|".join(str(value or "") for value in row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
