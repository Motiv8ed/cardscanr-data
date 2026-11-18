#!/usr/bin/env python3
"""Inspect *why* source provider IDs did not confirm in a TCGdex reconciliation.

Reads only 480 matched printing/source identifiers from local SQLite. Does not
copy the catalogue, write to SQLite, normalize aliases or confirm card variants.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

MAX_ROWS = 2000
REQUIRED_KEYS = ("source_language", "source_card_id", "source_set_id",
                 "local_id", "existing_printing_id")


def checked_rows(report: Path, jsonl: Path) -> list[dict]:
    manifest = json.loads(report.read_text(encoding="utf-8-sig"))
    data = jsonl.read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest.get("result_sha256"):
        raise ValueError("reconciliation result SHA256 mismatch")
    rows = [json.loads(line) for line in data.splitlines()]
    if (len(rows) != manifest.get("input_rows") or
            len(rows) == 0 or len(rows) > MAX_ROWS):
        raise ValueError("reconciliation count invalid or too large")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str)
                                            or not row[k] for k in REQUIRED_KEYS):
            raise ValueError("invalid reconciliation row")
        if row.get("canonical_write_approved") is not False:
            raise ValueError("unexpected canonical write approval")
        key = (row["source_language"], row["source_card_id"])
        if key in seen:
            raise ValueError("duplicate provider card identity")
        seen.add(key)
    return rows


def open_readonly(path: Path) -> sqlite3.Connection:
    path = path.resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise ValueError("database must be a real file")
    wal = path.with_name(path.name + "-wal")
    if wal.is_file() and wal.stat().st_size:
        raise ValueError("active SQLite WAL; use checkpointed snapshot")
    db = sqlite3.connect("file:" + quote(path.as_posix(), safe="/:") +
                         "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    required = {"card_printing", "source_record", "card_set", "set_release"}
    existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required.issubset(existing):
        db.close()
        raise ValueError("not a worldwide staging schema")
    return db


SQL = """
SELECT cp.id, src.provider_id, src.provider_record_id,
       src.record_type, src.source_path,
       sr.release_code, sr.language_code, cp.collector_number,
       cs.provider_id AS set_provider_id
FROM card_printing cp
JOIN source_record src ON src.id=cp.source_record_id
JOIN set_release sr ON sr.id=cp.set_release_id
JOIN card_set cs ON cs.id=sr.card_set_id
WHERE cp.id=?
"""


def classify(source_id: str, api_id: str) -> str:
    if source_id == api_id:
        return "provider_id_exact"
    if source_id.endswith("/" + api_id):
        return "provider_path_suffix_candidate_not_confirmed"
    return "provider_id_format_or_identity_unresolved"


def audit(rows: list[dict], db: sqlite3.Connection) -> dict:
    counts = collections.Counter()
    by_language: dict[str, collections.Counter] = {}
    samples: dict[str, list[dict]] = {}
    for row in rows:
        lang = row["source_language"]
        existing = db.execute(SQL, (row["existing_printing_id"],)).fetchone()
        if existing is None:
            category = "printing_no_longer_found_in_snapshot"
        elif (existing["release_code"] != row["source_set_id"] or
              existing["language_code"] != lang or
              existing["collector_number"] != row["local_id"] or
              existing["provider_id"] != "tcgdex-cards-database" or
              existing["set_provider_id"] != "tcgdex-cards-database"):
            category = "source_release_or_number_changed"
        else:
            category = classify(str(existing["provider_record_id"]), row["source_card_id"])
        counts[category] += 1
        by_language.setdefault(lang, collections.Counter())[category] += 1
        if len(samples.setdefault(lang, [])) < 2 and existing is not None:
            samples[lang].append({
                "expected_provider_card_id": row["source_card_id"],
                "stored_provider_record_id": str(existing["provider_record_id"])[:160],
                "stored_record_type": str(existing["record_type"])[:80],
                "source_path_filename": Path(str(existing["source_path"])).name[:120],
                "category": category,
            })
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows_checked": len(rows), "classifications": dict(sorted(counts.items())),
        "by_language": {k: dict(sorted(v.items())) for k, v in sorted(by_language.items())},
        "provider_id_samples": samples,
        "source_id_match_is_not_variant_confirmation": True,
        "aliases_automatically_approved": 0,
        "database_writes": 0,
        "production_publication": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--reconciliation", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.is_symlink() or (args.output.exists() and
                                  (not args.output.is_dir() or any(args.output.iterdir()))):
        parser.error("output must be a new empty folder")
    try:
        rows = checked_rows(args.report, args.reconciliation)
        db = open_readonly(args.database)
        try:
            result = audit(rows, db)
        finally:
            db.close()
    except (ValueError, FileNotFoundError, sqlite3.Error, OSError, UnicodeError) as exc:
        parser.exit(2, f"source audit stopped without DB changes: {type(exc).__name__}: {exc}\n")
    args.output.mkdir(parents=True, exist_ok=True)
    out = args.output / "SAFE-Provider-ID-Audit.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(json.dumps({"rows_checked": result["rows_checked"],
                      "classifications": result["classifications"],
                      "output": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
