#!/usr/bin/env python3
"""Live byte/integrity compatibility canary for old KO and active core R2 packs.

Never writes production objects or serves images. Downloads two small, public
catalogue packs to disposable local storage and reports read-only SQLite checks.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

ACTIVE = "https://assets.cardscanr.com/v2/catalog/pokemon/packs/active/catalogue.packs.manifest.json"
CANARY = "https://assets.cardscanr.com/v2/catalog/pokemon/packs/canary4-packs-20260803/catalogue.packs.manifest.json"
HOSTS = {"assets.cardscanr.com", "pub-258b8de1c4964f538a8cb08022761430.r2.dev"}
MAX_MANIFEST = 2 * 1024 * 1024
MAX_GZIP = 15 * 1024 * 1024
MAX_RAW = 128 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("redirect_denied")


def get(url: str, max_bytes: int) -> bytes:
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in HOSTS or parts.username or
            parts.password or parts.port not in (None, 443)):
        raise ValueError("unapproved_url")
    req = Request(url, headers={"User-Agent": "CardScanR-ReadOnly-Korean-PackPreflight/1.0"})
    with build_opener(NoRedirect()).open(req, timeout=25) as response:
        if response.status != 200:
            raise ValueError("http_not_200")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("download_exceeds_cap")
    return body


def pick(manifest: dict, pack_id: str) -> dict:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("packs"), list):
        raise ValueError("invalid_pack_manifest")
    matches = [p for p in manifest["packs"]
               if isinstance(p, dict) and p.get("packId") == pack_id]
    if len(matches) != 1:
        raise ValueError("required_pack_not_unique_or_missing")
    return matches[0]


def unpack(pack: dict, gzip_bytes: bytes, target: Path) -> dict:
    expected_gz = pack.get("gzipSha256")
    expected_raw = pack.get("sha256")
    expected_bytes = pack.get("rawSqliteBytes")
    if not all(isinstance(v, str) and len(v) == 64 for v in (expected_gz, expected_raw)):
        raise ValueError("pack_missing_sha256")
    if hashlib.sha256(gzip_bytes).hexdigest() != expected_gz:
        raise ValueError("gzip_sha256_mismatch")
    with gzip.GzipFile(fileobj=__import__("io").BytesIO(gzip_bytes)) as stream, target.open("wb") as out:
        total = 0
        while chunk := stream.read(64 * 1024):
            total += len(chunk)
            if total > MAX_RAW:
                raise ValueError("inflated_sqlite_exceeds_cap")
            out.write(chunk)
    if len(gzip_bytes) > MAX_GZIP or total != expected_bytes:
        raise ValueError("raw_pack_size_mismatch")
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected_raw:
        raise ValueError("raw_sha256_mismatch")
    db = sqlite3.connect("file:" + quote(target.resolve().as_posix(),safe="/:") + "?mode=ro&immutable=1", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        check = db.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise ValueError("sqlite_quick_check_failed")
        meta = dict(db.execute("SELECT key,value FROM meta"))
        cards = db.execute("SELECT count(*) FROM cards").fetchone()[0]
        sets = db.execute("SELECT count(*) FROM sets").fetchone()[0]
        fields = {}
        for table in ("cards", "sets"):
            fields[table] = [r[1:3] for r in db.execute(f"PRAGMA table_info('{table}')")]
        if cards != pack.get("recordCount"):
            raise ValueError("declared_card_count_mismatch")
        return {
            "pack_id": pack["packId"], "cards": cards, "sets": sets,
            "raw_sqlite_bytes": total, "compressed_bytes": len(gzip_bytes),
            "sha256_verified": True, "sqlite_quick_check": check,
            "schema_version": meta.get("schema_version"),
            "pack_schema_version": meta.get("packSchemaVersion"),
            "field_signatures": fields,
        }
    finally:
        db.close()


def run(fetch=get) -> dict:
    active = json.loads(fetch(ACTIVE, MAX_MANIFEST))
    canary = json.loads(fetch(CANARY, MAX_MANIFEST))
    active_core = pick(active, "core")
    old_ko = pick(canary, "ko")
    if any(not isinstance(p.get("compressedDatabaseUrl"), str) for p in (active_core, old_ko)):
        raise ValueError("missing_download_url")
    with tempfile.TemporaryDirectory(prefix="cardscanr-ko-preflight-") as tmp:
        root = Path(tmp)
        results = []
        for pack in (active_core, old_ko):
            compressed = fetch(pack["compressedDatabaseUrl"], MAX_GZIP)
            results.append(unpack(pack, compressed, root / (pack["packId"] + ".sqlite")))
    core, ko = results
    schema_equal = core["field_signatures"] == ko["field_signatures"]
    pack_version_equal = core["pack_schema_version"] == ko["pack_schema_version"]
    search_version_equal = core["schema_version"] == ko["schema_version"]
    # These are necessary, not sufficient, for a real mixed-generation release.
    compatibility_preconditions = schema_equal and pack_version_equal and search_version_equal
    for result in results:
        result.pop("field_signatures", None)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "active_release_id": active.get("catalogueReleaseId"),
        "old_release_id": canary.get("catalogueReleaseId"),
        "packs": results,
        "schema_fields_equal": schema_equal,
        "pack_schema_version_equal": pack_version_equal,
        "search_schema_version_equal": search_version_equal,
        "structural_preconditions_passed": compatibility_preconditions,
        "cross_release_content_and_canonical_id_compatibility_verified": False,
        "artwork_rights_verified": False,
        "safe_to_change_production_pointer": False,
        "production_writes": 0,
        "image_downloads": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.is_symlink() or (args.output.exists() and
                                 (not args.output.is_dir() or any(args.output.iterdir()))):
        parser.error("output must be a new empty folder")
    try:
        result = run()
    except (ValueError, OSError, sqlite3.Error, KeyError, json.JSONDecodeError) as e:
        parser.exit(2, f"read-only preflight stopped: {type(e).__name__}: {e}\n")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "SAFE-Korean-Pack-Preflight.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in (
        "active_release_id", "old_release_id", "structural_preconditions_passed",
        "safe_to_change_production_pointer")}))
    return 0 if result["structural_preconditions_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
