#!/usr/bin/env python3
"""Private SHA-verified R2 pack gap inventory (no image files or live writes).

Archived pack coverage is NOT equivalent to the local Artwork Vault.
It is a source-finding map for improving missing card image URL references.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from tools.preflight_multilingual_crossrelease import (
    MAX_GZIP,
    get,
    inspect_pack,
)
from tools.preflight_legacy_korean_pack import ACTIVE,CANARY,pick
from tools.private_pack_completeness import inventory_pack

ACTIVE_PACKS=("core","en","ja")
ARCHIVE_PACKS=("ko","zh-tw","th","id","zh-cn")
ALLOWED=ACTIVE_PACKS+ARCHIVE_PACKS

def read_gap_distribution(db:sqlite3.Connection) -> tuple[list[dict],list[dict]]:
    """No card rows are included, only pack-level and set-level aggregate counts."""
    db.execute("PRAGMA query_only=ON")
    columns={str(c[1]) for c in db.execute("PRAGMA table_info(cards)")}
    required={"language","set_id","image_thumbnail_url","image_display_url"}
    if not required.issubset(columns):
        raise ValueError("pack_gap_columns_missing")
    predicate="""COALESCE(TRIM(image_thumbnail_url),'') = ''
               AND COALESCE(TRIM(image_display_url),'') = ''"""
    by_language=[]
    for language, count, no_url in db.execute(
        f"SELECT language,COUNT(*),SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END) "
        "FROM cards GROUP BY language ORDER BY 3 DESC, 2 DESC, 1"):
        by_language.append({
            "language":str(language),"records":int(count),
            "no_image_reference":int(no_url or 0),
            "has_image_reference":int(count)-int(no_url or 0),
        })
    top_sets=[]
    for language, sid, rows, lacking in db.execute(
        f"SELECT language,set_id,COUNT(*),SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END) "
        "FROM cards GROUP BY language,set_id "
        "ORDER BY 4 DESC,3 DESC,1,2 LIMIT 30"):
        if not isinstance(sid,str) or len(sid)>200 or not isinstance(language,str):
            raise ValueError("unbounded_set_name")
        top_sets.append({
            "language":language,"set_id":sid,
            "records":int(rows),"no_image_reference":int(lacking or 0),
        })
    return by_language,top_sets

def inspect_one(pack_id:str,output:Path):
    if pack_id not in ALLOWED:
        raise ValueError("pack_not_allowed")
    if output.exists() or output.is_symlink():
        raise FileExistsError("output_must_be_new")
    manifest_url=ACTIVE if pack_id in ACTIVE_PACKS else CANARY
    raw_manifest=get(manifest_url,2_000_000)
    manifest=json.loads(raw_manifest)
    descriptor=pick(manifest,pack_id)
    raw=get(descriptor["compressedDatabaseUrl"],MAX_GZIP)
    with tempfile.TemporaryDirectory(prefix="cardscanr-pack-gap-") as td:
        path=Path(td)/(pack_id+".sqlite")
        checks,_,_,_,_,_=inspect_pack(descriptor,raw,path)
        conn=sqlite3.connect(
            "file:"+quote(path.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1",
            uri=True
        )
        try:
            conn.execute("PRAGMA query_only=ON")
            from_source=inventory_pack(conn)
            langs,sets=read_gap_distribution(conn)
        finally:
            conn.close()
    total=sum(x["records"] for x in langs)
    missing=sum(x["no_image_reference"] for x in langs)
    if total!=descriptor["recordCount"] or total!=from_source["card_records"]:
        raise ValueError("provider_pack_card_count_drift")
    if missing!=from_source["cards_without_image_reference"]:
        raise ValueError("source_inventory_mismatch")
    receipt={
        "classification":"READ_ONLY_ARCHIVED_PACK_GAPS_NOT_LIVE_VAULT",
        "pack_id":pack_id,
        "pack_source": "active" if pack_id in ACTIVE_PACKS else "archived_canary",
        "generated_utc":datetime.now(timezone.utc).isoformat(),
        "source_pack_sha256":descriptor["sha256"],
        "source_pack_gzip_sha256":descriptor["gzipSha256"],
        "source_pack_integrity_check":checks["sqlite_quick_check"],
        "pack_record_count":total,
        "pack_records_missing_image_reference":missing,
        "pack_records_with_image_reference":total-missing,
        "pack_languages":langs,
        "highest_gap_sets":sets,
        "missing_metadata_field_counts":from_source["missing_metadata_field_counts"],
        "provider_reference_presence_not_a_rights_grant":True,
        "not_a_live_vault_card_inventory":True,
        "image_binaries_downloaded":0,
        "live_vault_writes":0,"production_writes":0,
        "new_canonical_card_records":0,
        "artwork_rights_cleared":False,
        "safe_to_publish_images":False,
    }
    output.mkdir(parents=True)
    (output/"receipt.json").write_text(
        json.dumps(receipt,sort_keys=True,ensure_ascii=False,indent=2)+"\n",
        encoding="utf8")
    return receipt

def self_test():
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE cards(language TEXT,set_id TEXT,image_thumbnail_url TEXT,image_display_url TEXT)")
        db.executemany("INSERT INTO cards VALUES(?,?,?,?)",[
            ("ja","setA",None,None),("ja","setA","https://example.com/1",None),
            ("ko","setB",None,"")])
        langs,sets=read_gap_distribution(db)
        assert sum(x["records"] for x in langs)==3
        assert sum(x["no_image_reference"] for x in langs)==2
        assert sets[0]["set_id"] in ("setA","setB")
    assert set(ALLOWED)=={"core","en","ja","ko","zh-tw","th","id","zh-cn"}
    print("PASS: aggregation only and fixed allowlisted packs")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--pack",choices=ALLOWED)
    ap.add_argument("--output",type=Path)
    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()
    if args.self_test:self_test();return
    if args.pack is None or args.output is None:
        ap.error("--pack and --output required")
    result=inspect_one(args.pack,args.output)
    print("VERIFIED_PACK_GAP_AUDIT",json.dumps({
        "pack":result["pack_id"],
        "records":result["pack_record_count"],
        "missing_image_url_in_snapshot":result["pack_records_missing_image_reference"],
        "per_language":result["pack_languages"],
        "is_live_vault":False,
        "new_cards":0,
        "writes":0,
    },ensure_ascii=False,sort_keys=True))
if __name__=="__main__":
    main()
