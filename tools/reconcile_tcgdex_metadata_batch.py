#!/usr/bin/env python3
"""Read-only reconciliation of a hashed TCGdex metadata discovery ZIP with CardScanR staging.

Never imports data, changes canonical IDs, infers card variants, or publishes artwork.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

MAX_ZIP_BYTES = 35_000_000
MAX_ROWS = 100_000
LANGUAGES = frozenset({"ja", "zh-tw", "ko", "en", "th", "id", "zh-cn"})
PROVIDER_ID = "tcgdex-cards-database"
REQUIRED_TABLES = frozenset({"card_set", "set_release", "card_printing", "card_variant", "source_record"})


def read_candidates(archive: Path) -> tuple[list[dict], dict]:
    if not archive.is_file() or archive.is_symlink():
        raise ValueError("archive must be a real ZIP file")
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        if sorted(names) != ["card_candidates.jsonl", "manifest.json"]:
            raise ValueError("unexpected archive members")
        if any(i.file_size > MAX_ZIP_BYTES for i in z.infolist()):
            raise ValueError("archive member exceeds size limit")
        if sum(i.file_size for i in z.infolist()) > MAX_ZIP_BYTES:
            raise ValueError("archive too large")
        manifest = json.loads(z.read("manifest.json"))
        raw = z.read("card_candidates.jsonl")
    if manifest.get("provider") != "tcgdex" or manifest.get("app_release_allowed") is not False:
        raise ValueError("unsupported or release-enabled manifest")
    if hashlib.sha256(raw).hexdigest() != manifest.get("candidate_sha256"):
        raise ValueError("candidate SHA256 mismatch")
    if len(raw) != manifest.get("candidate_bytes"):
        raise ValueError("candidate size mismatch")
    lines = raw.splitlines()
    if len(lines) != manifest.get("candidate_cards") or len(lines) > MAX_ROWS:
        raise ValueError("candidate count invalid")
    rows, keys = [], set()
    for lineno, line in enumerate(lines, 1):
        row = json.loads(line)
        if not isinstance(row, dict) or row.get("source_provider") != "tcgdex":
            raise ValueError(f"row {lineno}: invalid provider")
        lang, sid, cid, local = (row.get(k) for k in
                                  ("source_language", "source_set_id", "source_card_id", "local_id"))
        if lang not in LANGUAGES or not all(isinstance(v, str) and v and len(v) <= 120
                                             for v in (sid, cid, local)):
            raise ValueError(f"row {lineno}: invalid provider identity")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", cid) or not re.fullmatch(r"[A-Za-z0-9_.-]+", sid):
            raise ValueError(f"row {lineno}: unexpected identity format")
        if not re.fullmatch(r"[0-9a-f]{64}", str(row.get("source_response_sha256", ""))):
            raise ValueError(f"row {lineno}: invalid response SHA")
        key = (lang, cid)
        if key in keys:
            raise ValueError(f"row {lineno}: duplicate provider card identity")
        keys.add(key)
        if row.get("artwork_downloaded") is not False or row.get("production_published") is not False:
            raise ValueError(f"row {lineno}: unsafe publication marker")
        rows.append(row)
    return rows, manifest


def connect_staging(path: Path) -> sqlite3.Connection:
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("staging path must be a file")
    if path.with_name(path.name + "-wal").exists() and path.with_name(path.name + "-wal").stat().st_size:
        raise ValueError("live WAL detected; use a checkpointed copy of staging, not the active database")
    # immutable=1 disallows any journal/SHM creation and prevents accidental writes.
    uri = "file:" + quote(path.as_posix(), safe="/:") + "?mode=ro&immutable=1"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not REQUIRED_TABLES.issubset(existing):
        db.close()
        raise ValueError("not a CardScanR worldwide staging database")
    return db


SQL = """
SELECT DISTINCT cp.id AS printing_id, sr.region_code AS region, sr.language_code AS lang,
       sr.release_code AS set_code, cp.collector_number AS collector,
       src.provider_id AS card_source, src.provider_record_id AS card_source_id
FROM card_printing cp
JOIN set_release sr ON sr.id=cp.set_release_id
JOIN card_set cs ON cs.id=sr.card_set_id
JOIN source_record src ON src.id=cp.source_record_id
WHERE cs.provider_id=? AND sr.language_code=? AND sr.release_code=?
  AND cp.collector_number=? AND src.provider_id=?
ORDER BY cp.id
LIMIT 4
"""


def classify(row: dict, db: sqlite3.Connection | None) -> dict:
    output = {
        "source_provider": "tcgdex",
        "source_language": row["source_language"],
        "source_set_id": row["source_set_id"],
        "source_card_id": row["source_card_id"],
        "local_id": row["local_id"],
        "source_response_sha256": row["source_response_sha256"],
        "canonical_write_approved": False,
        "variant_identity_verified": False,
        "artwork_publication_approved": False,
    }
    if db is None:
        output["status"] = "requires_staging_snapshot"
        return output
    matches = db.execute(SQL, (PROVIDER_ID, row["source_language"], row["source_set_id"],
                               row["local_id"], PROVIDER_ID)).fetchall()
    if len(matches) == 0:
        output["status"] = "not_found_by_exact_provider_set_language_number"
    elif len(matches) > 1:
        output["status"] = "ambiguous_multiple_printings"
        output["matching_printing_count"] = len(matches)
    else:
        found = matches[0]
        variant_count = db.execute("SELECT count(*) FROM card_variant WHERE card_printing_id=?",
                                   (found["printing_id"],)).fetchone()[0]
        source_id = str(found["card_source_id"])
        card_id = row["source_card_id"]
        source_id_matches = (source_id == card_id or source_id.endswith("/" + card_id))
        output.update({
            "status": ("represented_exact_set_language_number" if source_id_matches
                       else "set_language_number_match_provider_card_id_unconfirmed"),
            "existing_printing_id": found["printing_id"],
            "existing_region": found["region"],
            "existing_variant_count": variant_count,
            "source_card_id_provenance_confirmed": source_id_matches,
            "needs_variant_review": True,
        })
    return output


def reconcile(archive: Path, database: Path | None, output: Path) -> dict:
    if output.is_symlink() or (output.exists() and
                              (not output.is_dir() or any(output.iterdir()))):
        raise FileExistsError("use a new empty output directory")
    rows, manifest = read_candidates(archive)
    db = connect_staging(database) if database is not None else None
    try:
        results = [classify(row, db) for row in rows]
        counts = collections.Counter(r["status"] for r in results)
        provider_id_confirmed = sum(r.get("source_card_id_provenance_confirmed") is True for r in results)
        provider_id_unconfirmed = sum(r.get("source_card_id_provenance_confirmed") is False for r in results)
        multiple_variants = sum(r.get("existing_variant_count", 0) > 1 for r in results)
        by_language: dict[str, dict[str, int]] = {}
        for r in results:
            lan = r["source_language"]
            if lan not in by_language:
                by_language[lan] = {}
            status = r["status"]
            by_language[lan][status] = by_language[lan].get(status, 0) + 1
        for language in manifest["languages_requested"]:
            by_language.setdefault(language, {})
        report = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "input_sha256": manifest["candidate_sha256"],
            "input_rows": len(rows),
            "staging_snapshot_provided": db is not None,
            "results": dict(sorted(counts.items())),
            "provider_card_id_confirmed_matches": provider_id_confirmed,
            "provider_card_id_unconfirmed_matches": provider_id_unconfirmed,
            "existing_printings_with_multiple_variants": multiple_variants,
            "all_variant_identities_still_unverified": True,
            "set_number_match_alone_is_not_provider_id_confirmation": True,
            "by_language": by_language,
            "missing_from_staging_is_not_confirmed_new_printing": True,
            "variants_automatically_merged": 0,
            "canonical_database_writes": 0,
            "image_files_downloaded": 0,
            "app_publication_allowed": False,
        }
        serialized = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                             for row in results).encode("utf-8")
        report["result_sha256"] = hashlib.sha256(serialized).hexdigest()
        output.mkdir(parents=True, exist_ok=True)
        (output / "reconciliation.jsonl").write_bytes(serialized)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                                       sort_keys=True) + "\n", encoding="utf-8")
        return report
    finally:
        if db is not None:
            db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path, help="unmodified TCGdex GitHub Actions artifact ZIP")
    parser.add_argument("--database", type=Path, help="checkpointed, read-only worldwide staging SQLite copy")
    parser.add_argument("--output", required=True, type=Path, help="new empty report directory")
    args = parser.parse_args()
    try:
        report = reconcile(args.archive, args.database, args.output)
    except (ValueError, FileNotFoundError, FileExistsError, sqlite3.Error, OSError, zipfile.BadZipFile) as err:
        parser.exit(2, f"reconciliation stopped: {type(err).__name__}: {err}\n")
    print(json.dumps({"input_rows": report["input_rows"], "results": report["results"],
                      "by_language": report["by_language"], "canonical_database_writes": 0,
                      "report_sha256": report["result_sha256"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
