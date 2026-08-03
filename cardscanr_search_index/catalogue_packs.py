"""Versioned catalogue-pack builder for Cloudflare worldwide search indexes.

Splits a monolithic global catalogue SQLite into independently downloadable packs
so first-install clients never need a >512 MB monolith.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .global_builder import SCHEMA_VERSION

PACK_SCHEMA_VERSION = "2.2.0"
PACK_MANIFEST_SCHEMA_VERSION = "1.0.0"
MAX_PUBLIC_PACK_BYTES = 512 * 1024 * 1024
DEFAULT_AU_PACKS = ("core", "en", "sealed-products")

LANGUAGE_PACKS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("en", ("en",), "English card catalogue"),
    ("ja", ("ja",), "Japanese card catalogue"),
    ("ko", ("ko",), "Korean card catalogue"),
    ("zh-cn", ("zh-cn",), "Simplified Chinese card catalogue"),
    ("zh-tw", ("zh-tw",), "Traditional Chinese card catalogue"),
    ("th", ("th",), "Thai card catalogue"),
    ("id", ("id",), "Indonesian card catalogue"),
    (
        "intl-other",
        ("fr", "de", "it", "es", "pt", "es-mx", "pt-br", "nl", "ru", "pl"),
        "Other international-language card catalogues",
    ),
)

BASE_TABLES = (
    "meta",
    "sets",
    "cards",
    "card_aliases",
    "cards_fts",
    "sealed_products",
    "sealed_product_contents",
    "sealed_products_fts",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def gzip_file(source: Path, destination: Path, *, level: int = 9) -> dict[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    compressor = zlib.compressobj(level, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    written = 0
    with source.open("rb") as src, destination.open("wb") as dst:
        while True:
            chunk = src.read(8 * 1024 * 1024)
            if not chunk:
                break
            out = compressor.compress(chunk)
            if out:
                dst.write(out)
                written += len(out)
        out = compressor.flush()
        if out:
            dst.write(out)
            written += len(out)
    return {"rawBytes": source.stat().st_size, "gzipBytes": written}


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]


def _load_base_schema(source_db: Path) -> tuple[list[str], list[str]]:
    with _connect(source_db, read_only=True) as source:
        schema_sql = [
            str(row[0])
            for row in source.execute(
                f"""
                SELECT sql FROM sqlite_master
                WHERE type='table' AND name IN ({",".join("?" for _ in BASE_TABLES)})
                  AND sql IS NOT NULL
                ORDER BY name
                """,
                BASE_TABLES,
            )
        ]
        index_sql = [
            str(row[0])
            for row in source.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type='index' AND sql IS NOT NULL
                  AND tbl_name IN ('sets','cards','card_aliases','sealed_products','sealed_product_contents')
                ORDER BY name
                """
            )
        ]
    return schema_sql, index_sql


def _init_pack_db(tmp: Path, source_db: Path) -> sqlite3.Connection:
    tmp.unlink(missing_ok=True)
    schema_sql, index_sql = _load_base_schema(source_db)
    dest = _connect(tmp)
    dest.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY;")
    for sql in schema_sql:
        dest.execute(sql)
    for sql in index_sql:
        dest.execute(sql)
    dest.execute("ATTACH DATABASE ? AS src", (str(source_db),))
    return dest


def _set_meta(connection: sqlite3.Connection, pairs: dict[str, str]) -> None:
    connection.executemany(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        list(pairs.items()),
    )


def _rebuild_cards_fts(connection: sqlite3.Connection) -> int:
    connection.execute("DELETE FROM cards_fts")
    connection.execute(
        """
        INSERT INTO cards_fts(
          canonical_base_id, native_card_name, english_card_name, native_set_name,
          english_set_name, printed_collector_number, normalized_collector_number, set_code, aliases
        )
        SELECT
          canonical_base_id, native_card_name, english_card_name, native_set_name,
          english_set_name, printed_collector_number, normalized_collector_number, set_code, aliases
        FROM cards
        """
    )
    return int(connection.execute("SELECT COUNT(*) FROM cards_fts").fetchone()[0])


def _rebuild_products_fts(connection: sqlite3.Connection) -> int:
    connection.execute("DELETE FROM sealed_products_fts")
    connection.execute(
        """
        INSERT INTO sealed_products_fts(product_variant_id, local_name, canonical_name, product_type)
        SELECT product_variant_id, local_name, canonical_name, product_type
        FROM sealed_products
        """
    )
    return int(connection.execute("SELECT COUNT(*) FROM sealed_products_fts").fetchone()[0])


def _fts_bytes(connection: sqlite3.Connection, prefix: str) -> int:
    total = 0
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE ? ESCAPE '\\'",
        (prefix.replace("_", "\\_") + "%",),
    ):
        try:
            if name.endswith(("_content", "_data", "_docsize", "_idx")):
                cols = _table_columns(connection, name)
                if not cols:
                    continue
                expr = "+".join(f'COALESCE(LENGTH("{c}"),0)' for c in cols)
                total += int(connection.execute(f'SELECT COALESCE(SUM({expr}),0) FROM "{name}"').fetchone()[0])
        except sqlite3.Error:
            continue
    return total


@dataclass(frozen=True)
class PackBuildResult:
    pack_id: str
    kind: str
    description: str
    languages: list[str]
    sqlite_path: Path
    gzip_path: Path
    sha256: str
    gzip_sha256: str
    raw_bytes: int
    gzip_bytes: int
    installed_bytes: int
    temp_update_bytes: int
    record_count: int
    product_count: int
    fts_bytes_approx: int
    schema_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "packId": self.pack_id,
            "kind": self.kind,
            "description": self.description,
            "languages": self.languages,
            "schemaVersion": self.schema_version,
            "sqlitePath": str(self.sqlite_path),
            "gzipPath": str(self.gzip_path),
            "sha256": self.sha256,
            "gzipSha256": self.gzip_sha256,
            "rawSqliteBytes": self.raw_bytes,
            "compressedDownloadBytes": self.gzip_bytes,
            "installedBytes": self.installed_bytes,
            "temporaryUpdateSpaceBytes": self.temp_update_bytes,
            "recordCount": self.record_count,
            "productCount": self.product_count,
            "ftsBytesApprox": self.fts_bytes_approx,
            "withinPublicSizeLimit": self.raw_bytes <= MAX_PUBLIC_PACK_BYTES
            and self.gzip_bytes <= MAX_PUBLIC_PACK_BYTES,
        }


def _close_sqlite(connection: sqlite3.Connection | None) -> None:
    if connection is None:
        return
    try:
        connection.close()
    except sqlite3.Error:
        pass


def _finalize_pack(
    *,
    pack_id: str,
    kind: str,
    description: str,
    languages: Iterable[str],
    sqlite_path: Path,
    output_dir: Path,
) -> PackBuildResult:
    final_sqlite = output_dir / "sqlite" / f"{pack_id}.sqlite"
    final_sqlite.parent.mkdir(parents=True, exist_ok=True)
    final_sqlite.unlink(missing_ok=True)
    connection = _connect(sqlite_path)
    try:
        connection.execute("VACUUM INTO ?", (str(final_sqlite),))
    finally:
        _close_sqlite(connection)
    try:
        sqlite_path.unlink(missing_ok=True)
    except OSError:
        # Windows may briefly retain a handle; vacuumed output is authoritative.
        pass

    gzip_path = output_dir / "gzip" / f"{pack_id}.sqlite.gz"
    sizes = gzip_file(final_sqlite, gzip_path)
    raw_sha = sha256_file(final_sqlite)
    gzip_sha = sha256_file(gzip_path)
    with _connect(final_sqlite, read_only=True) as connection:
        cards = int(connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
        products = int(connection.execute("SELECT COUNT(*) FROM sealed_products").fetchone()[0])
        fts_bytes = _fts_bytes(connection, "cards_fts") + _fts_bytes(connection, "sealed_products_fts")
    installed = sizes["rawBytes"]
    temp_update = installed + sizes["gzipBytes"] + installed
    return PackBuildResult(
        pack_id=pack_id,
        kind=kind,
        description=description,
        languages=list(languages),
        sqlite_path=final_sqlite,
        gzip_path=gzip_path,
        sha256=raw_sha,
        gzip_sha256=gzip_sha,
        raw_bytes=sizes["rawBytes"],
        gzip_bytes=sizes["gzipBytes"],
        installed_bytes=installed,
        temp_update_bytes=temp_update,
        record_count=cards,
        product_count=products,
        fts_bytes_approx=fts_bytes,
        schema_version=SCHEMA_VERSION,
    )


def build_core_pack(source_db: Path, work_dir: Path, output_dir: Path) -> PackBuildResult:
    tmp = work_dir / "core.sqlite.tmp"
    dest = _init_pack_db(tmp, source_db)
    try:
        dest.execute("INSERT INTO sets SELECT * FROM src.sets")
        _set_meta(
            dest,
            {
                "schema_version": SCHEMA_VERSION,
                "searchIndexSchemaVersion": SCHEMA_VERSION,
                "packId": "core",
                "packKind": "core",
                "packSchemaVersion": PACK_SCHEMA_VERSION,
                "source": "catalogue_pack_builder",
                "recordCount": "0",
                "productRecordCount": "0",
                "productContentRecordCount": "0",
                "setCount": str(dest.execute("SELECT COUNT(*) FROM sets").fetchone()[0]),
            },
        )
        dest.commit()
        dest.execute("DETACH DATABASE src")
    finally:
        _close_sqlite(dest)
    return _finalize_pack(
        pack_id="core",
        kind="core",
        description="Core metadata and set index (no language card rows)",
        languages=[],
        sqlite_path=tmp,
        output_dir=output_dir,
    )


def build_language_pack(
    source_db: Path,
    work_dir: Path,
    output_dir: Path,
    *,
    pack_id: str,
    languages: Sequence[str],
    description: str,
) -> PackBuildResult:
    tmp = work_dir / f"{pack_id}.sqlite.tmp"
    placeholders = ",".join("?" for _ in languages)
    dest = _init_pack_db(tmp, source_db)
    try:
        dest.execute(
            f"INSERT INTO sets SELECT * FROM src.sets WHERE language IN ({placeholders})",
            tuple(languages),
        )
        dest.execute(
            f"INSERT INTO cards SELECT * FROM src.cards WHERE language IN ({placeholders})",
            tuple(languages),
        )
        dest.execute(
            f"""
            INSERT INTO card_aliases
            SELECT a.* FROM src.card_aliases a
            JOIN src.cards c ON c.canonical_base_id = a.canonical_base_id
            WHERE c.language IN ({placeholders})
            """,
            tuple(languages),
        )
        fts_count = _rebuild_cards_fts(dest)
        card_count = int(dest.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
        _set_meta(
            dest,
            {
                "schema_version": SCHEMA_VERSION,
                "searchIndexSchemaVersion": SCHEMA_VERSION,
                "packId": pack_id,
                "packKind": "language",
                "packSchemaVersion": PACK_SCHEMA_VERSION,
                "source": "catalogue_pack_builder",
                "languages": ",".join(languages),
                "recordCount": str(card_count),
                "productRecordCount": "0",
                "productContentRecordCount": "0",
                "ftsRecordCount": str(fts_count),
            },
        )
        dest.commit()
        dest.execute("DETACH DATABASE src")
    finally:
        _close_sqlite(dest)
    return _finalize_pack(
        pack_id=pack_id,
        kind="language",
        description=description,
        languages=languages,
        sqlite_path=tmp,
        output_dir=output_dir,
    )


def build_sealed_pack(source_db: Path, work_dir: Path, output_dir: Path) -> PackBuildResult:
    tmp = work_dir / "sealed-products.sqlite.tmp"
    dest = _init_pack_db(tmp, source_db)
    try:
        dest.execute("INSERT INTO sealed_products SELECT * FROM src.sealed_products")
        dest.execute("INSERT INTO sealed_product_contents SELECT * FROM src.sealed_product_contents")
        fts_count = _rebuild_products_fts(dest)
        product_count = int(dest.execute("SELECT COUNT(*) FROM sealed_products").fetchone()[0])
        content_count = int(dest.execute("SELECT COUNT(*) FROM sealed_product_contents").fetchone()[0])
        langs = [
            str(row[0] or "und")
            for row in dest.execute("SELECT DISTINCT language FROM sealed_products ORDER BY language")
        ]
        _set_meta(
            dest,
            {
                "schema_version": SCHEMA_VERSION,
                "searchIndexSchemaVersion": SCHEMA_VERSION,
                "packId": "sealed-products",
                "packKind": "sealed-products",
                "packSchemaVersion": PACK_SCHEMA_VERSION,
                "source": "catalogue_pack_builder",
                "recordCount": "0",
                "productRecordCount": str(product_count),
                "productContentRecordCount": str(content_count),
                "ftsRecordCount": str(fts_count),
                "languages": ",".join(langs),
            },
        )
        dest.commit()
        dest.execute("DETACH DATABASE src")
    finally:
        _close_sqlite(dest)
    return _finalize_pack(
        pack_id="sealed-products",
        kind="sealed-products",
        description="Sealed product catalogue and contents",
        languages=langs,
        sqlite_path=tmp,
        output_dir=output_dir,
    )


def build_all_packs(
    *,
    source_db: Path,
    output_dir: Path,
    public_base_url: str | None = None,
    catalogue_release_id: str | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = output_dir / "_work"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    results: list[PackBuildResult] = [
        build_core_pack(source_db, work_dir, output_dir),
    ]
    for pack_id, languages, description in LANGUAGE_PACKS:
        results.append(
            build_language_pack(
                source_db,
                work_dir,
                output_dir,
                pack_id=pack_id,
                languages=languages,
                description=description,
            )
        )
    results.append(build_sealed_pack(source_db, work_dir, output_dir))
    shutil.rmtree(work_dir, ignore_errors=True)

    release_id = catalogue_release_id or utc_now().replace(":", "").replace("-", "")
    base = (public_base_url or "").rstrip("/")
    packs_payload = []
    for result in results:
        object_key = (
            f"v2/catalog/pokemon/packs/{release_id}/{result.pack_id}/"
            f"{result.sha256}/{result.pack_id}.sqlite"
        )
        gzip_key = (
            f"v2/catalog/pokemon/packs/{release_id}/{result.pack_id}/"
            f"{result.sha256}/{result.pack_id}.sqlite.gz"
        )
        entry = result.as_dict()
        entry["objectKey"] = object_key
        entry["gzipObjectKey"] = gzip_key
        if base:
            entry["databaseUrl"] = f"{base}/{object_key}"
            entry["compressedDatabaseUrl"] = f"{base}/{gzip_key}"
            entry["compression"] = "gzip"
        packs_payload.append(entry)

    default_bytes = sum(
        p["compressedDownloadBytes"] for p in packs_payload if p["packId"] in DEFAULT_AU_PACKS
    )
    all_optional = sum(
        p["compressedDownloadBytes"] for p in packs_payload if p["packId"] not in DEFAULT_AU_PACKS
    )
    violations = [p["packId"] for p in packs_payload if not p["withinPublicSizeLimit"]]

    manifest = {
        "manifestSchemaVersion": PACK_MANIFEST_SCHEMA_VERSION,
        "catalogueSchemaVersion": "2.2.0",
        "packSchemaVersion": PACK_SCHEMA_VERSION,
        "searchIndexSchemaVersion": SCHEMA_VERSION,
        "generatedAt": utc_now(),
        "catalogueReleaseId": release_id,
        "sourceDatabaseSha256": sha256_file(source_db),
        "sourceDatabaseBytes": source_db.stat().st_size,
        "maxPublicObjectBytes": MAX_PUBLIC_PACK_BYTES,
        "defaultInstallPackIds": list(DEFAULT_AU_PACKS),
        "defaultInstallCompressedBytes": default_bytes,
        "optionalPackCompressedBytes": all_optional,
        "updatePolicy": "download_verified_immutable_pack_then_atomic_activate",
        "rollbackPolicy": "retain previous verified pack until replacement verifies",
        "deltaUpdates": {
            "status": "post_release",
            "notes": (
                "Initial release ships full immutable packs. Safe SQLite changeset / "
                "version-to-version patch bundles are deferred until pack baselines stabilize."
            ),
        },
        "packs": packs_payload,
        "sizeLimitViolations": violations,
        "classification": "PASS" if not violations else "FAIL_SIZE_LIMIT",
    }
    (output_dir / "catalogue.packs.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "PACK_SIZE_MATRIX.json").write_text(
        json.dumps(
            {
                "generatedAt": manifest["generatedAt"],
                "sourceDatabaseBytes": manifest["sourceDatabaseBytes"],
                "defaultInstallPackIds": manifest["defaultInstallPackIds"],
                "defaultInstallCompressedBytes": default_bytes,
                "packs": [
                    {
                        "packId": p["packId"],
                        "rawSqliteBytes": p["rawSqliteBytes"],
                        "compressedDownloadBytes": p["compressedDownloadBytes"],
                        "installedBytes": p["installedBytes"],
                        "temporaryUpdateSpaceBytes": p["temporaryUpdateSpaceBytes"],
                        "recordCount": p["recordCount"],
                        "productCount": p["productCount"],
                        "ftsBytesApprox": p["ftsBytesApprox"],
                        "languages": p["languages"],
                    }
                    for p in packs_payload
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest
