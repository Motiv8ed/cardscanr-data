#!/usr/bin/env python3
"""Read-only cross-release R2 catalogue compatibility preflight (never activates).

Downloads only eight explicitly allowlisted public SQLite packs to temporary
runner storage. Checks hashes, integrity, schemas, set references and canonical
ID collisions. It never changes active pointers, images or production databases.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.preflight_legacy_korean_pack import ACTIVE, CANARY, get, pick

ACTIVE_PACKS = ("core", "en", "ja")
ARCHIVE_PACKS = ("ko", "zh-tw", "th", "id", "zh-cn")
MAX_GZIP = 40_000_000
MAX_RAW = 300_000_000
EXPECTED_CARD_FIELDS = ("canonical_base_id", "canonical_printing_id", "set_id", "language")


def sha256_file(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_pack(pack: dict, zipped: bytes, destination: Path) -> tuple[dict, set[str], set[str], tuple]:
    required=("packId","rawSqliteBytes","recordCount","sha256","gzipSha256")
    if any(field not in pack for field in required):
        raise ValueError("incomplete_pack_descriptor")
    if len(zipped) > MAX_GZIP:
        raise ValueError("gzip_exceeds_budget")
    if hashlib.sha256(zipped).hexdigest() != pack["gzipSha256"]:
        raise ValueError("gzip_sha256_mismatch")
    size=0
    with gzip.GzipFile(fileobj=io.BytesIO(zipped)) as reader, destination.open("wb") as writer:
        while True:
            chunk=reader.read(1024*1024)
            if not chunk:break
            size+=len(chunk)
            if size > MAX_RAW:
                raise ValueError("decompressed_pack_exceeds_budget")
            writer.write(chunk)
    if size!=pack["rawSqliteBytes"] or sha256_file(destination)!=pack["sha256"]:
        raise ValueError("raw_pack_bytes_or_sha_mismatch")
    uri="file:"+quote(destination.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1"
    db=sqlite3.connect(uri,uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA quick_check").fetchone()[0]!="ok":
            raise ValueError("sqlite_quick_check_failed")
        columns={table:tuple(tuple(r[1:3]) for r in db.execute(
            f"PRAGMA table_info('{table}')")) for table in ("cards","sets","card_aliases")}
        card_column_names={row[0] for row in columns["cards"]}
        if not set(EXPECTED_CARD_FIELDS).issubset(card_column_names):
            raise ValueError("required_card_columns_missing")
        card_count=int(db.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
        set_count=int(db.execute("SELECT COUNT(*) FROM sets").fetchone()[0])
        fts_count=int(db.execute("SELECT COUNT(*) FROM cards_fts").fetchone()[0])
        orphan_count=int(db.execute(
            "SELECT COUNT(*) FROM cards c LEFT JOIN sets s ON c.set_id=s.set_id "
            "WHERE s.set_id IS NULL").fetchone()[0])
        if card_count!=pack["recordCount"]:
            raise ValueError("declared_record_count_mismatch")
        meta=dict(db.execute("SELECT key,value FROM meta"))
        base_ids={v[0] for v in db.execute("SELECT canonical_base_id FROM cards")}
        print_ids={v[0] for v in db.execute("SELECT canonical_printing_id FROM cards")}
        if len(base_ids)!=card_count or len(print_ids)!=card_count:
            raise ValueError("duplicate_canonical_ids_within_pack")
        if pack["packId"] in ARCHIVE_PACKS:
            wrong_lang=db.execute("SELECT COUNT(*) FROM cards WHERE language <> ?",
                                  (pack["packId"],)).fetchone()[0]
        else:
            wrong_lang=0
        permission_counts={str(k):int(v) for k,v in db.execute(
            "SELECT mirror_permission_status,COUNT(*) FROM cards "
            "GROUP BY mirror_permission_status ORDER BY COUNT(*) DESC LIMIT 20")}
        info={
            "pack_id":pack["packId"],"card_count":card_count,"set_count":set_count,
            "fts_rows":fts_count,"unresolved_set_references":orphan_count,
            "wrong_language_rows":wrong_lang,"gzip_bytes":len(zipped),
            "sqlite_bytes":size,"sha256_verified":True,"sqlite_quick_check":"ok",
            "search_schema_version":meta.get("schema_version"),
            "pack_schema_version":meta.get("packSchemaVersion"),
            "mirror_permission_status_counts":permission_counts,
        }
        signature=(columns["cards"],columns["sets"],columns["card_aliases"],
                   meta.get("schema_version"),meta.get("packSchemaVersion"))
        return info,base_ids,print_ids,signature
    finally:
        db.close()


def analyze(active: dict, archive: dict, fetch=get) -> dict:
    selected=[("active",pick(active,p)) for p in ACTIVE_PACKS]+[
        ("archive_canary",pick(archive,p)) for p in ARCHIVE_PACKS]
    results=[]
    base_owner={}
    print_owner={}
    collisions=[]
    schema_signatures=[]
    with tempfile.TemporaryDirectory(prefix="cardscanr-crossrelease-preflight-") as temp:
        root=Path(temp)
        for origin,pack in selected:
            url=pack.get("compressedDatabaseUrl")
            if not isinstance(url,str) or pack["packId"] not in ACTIVE_PACKS+ARCHIVE_PACKS:
                raise ValueError("disallowed_or_missing_pack")
            zipped=fetch(url,MAX_GZIP)
            info,bases,printings,sig=inspect_pack(
                pack,zipped,root/(pack["packId"]+".sqlite"))
            schema_signatures.append(sig)
            for type_name,keys,owners in (("base",bases,base_owner),
                                          ("printing",printings,print_owner)):
                for key in keys:
                    previous=owners.setdefault(key,pack["packId"])
                    if previous!=pack["packId"] and len(collisions)<20:
                        collisions.append({
                            "type":type_name,"id_sha256":hashlib.sha256(key.encode()).hexdigest(),
                            "first_pack":previous,"other_pack":pack["packId"]
                        })
            info["release_origin"]=origin
            results.append(info)
    fields_equal=all(sig==schema_signatures[0] for sig in schema_signatures)
    orphan_free=all(p["unresolved_set_references"]==0 for p in results)
    fts_complete=all(p["card_count"]==p["fts_rows"] for p in results)
    locales_clean=all(p["wrong_language_rows"]==0 for p in results)
    collision_free=not collisions
    structural_pass=fields_equal and orphan_free and fts_complete and locales_clean and collision_free
    # Deliberately NOT a publish-ready release: canonical data may be stale,
    # image rights and actual Flutter canary/rollback remain separate gates.
    return {
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "active_release_id":active.get("catalogueReleaseId"),
        "archive_release_id":archive.get("catalogueReleaseId"),
        "packs":results,
        "schema_fields_and_versions_equal":fields_equal,
        "all_local_set_references_present":orphan_free,
        "all_fts_card_counts_match":fts_complete,
        "language_pack_contents_match_codes":locales_clean,
        "canonical_id_collisions_found":len(collisions),
        "canonical_id_collision_samples":collisions,
        "structural_preconditions_passed":structural_pass,
        "source_database_shas_differ":active.get("sourceDatabaseSha256")!=archive.get("sourceDatabaseSha256"),
        "cross_release_data_freshness_verified":False,
        "customer_card_references_tested":False,
        "app_level_canary_tested":False,
        "artwork_rights_verified":False,
        "safe_to_publish_mixed_manifest":False,
        "database_packs_downloaded_for_private_verification_only":len(results),
        "image_files_downloaded":0,
        "production_writes":0,
        "r2_active_pointer_modified":False,
    }


def run(fetch=get) -> dict:
    active=json.loads(fetch(ACTIVE,2_000_000))
    archive=json.loads(fetch(CANARY,2_000_000))
    return analyze(active,archive,fetch)


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.output.is_symlink() or (
            args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir()))):
        parser.error("output must be a new empty directory")
    try:
        result=run()
    except (ValueError,OSError,sqlite3.Error,KeyError,EOFError,RuntimeError) as exc:
        parser.exit(2,f"Read-only cross-release preflight stopped: {type(exc).__name__}: {exc}\n")
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/"SAFE-Cross-Release-Pack-Preflight.json").write_text(
        json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps({"structural_preconditions_passed":result["structural_preconditions_passed"],
                      "packs_checked":len(result["packs"]),
                      "canonical_id_collisions_found":result["canonical_id_collisions_found"],
                      "safe_to_publish_mixed_manifest":False},sort_keys=True))
    return 0 if result["structural_preconditions_passed"] else 2


if __name__=="__main__":
    raise SystemExit(main())
