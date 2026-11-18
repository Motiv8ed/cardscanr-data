#!/usr/bin/env python3
"""Build a *private review-only* core with exactly the set IDs missing from a mixed release.

No Cloudflare/R2 uploads, no app/Supabase writes, no card/artwork downloads.
The original active core stays untouched. Test output is a disposable, hashable
candidate for a future beta canary, NOT a production-ready catalogue manifest.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.preflight_multilingual_crossrelease import (
    ACTIVE_PACKS,ARCHIVE_PACKS,ACTIVE,CANARY,MAX_GZIP,get,inspect_pack,sha256_file
)
from tools.preflight_legacy_korean_pack import pick

SET_COLUMNS=("set_id","language","region","set_name","normalized_set_name","release_date")


def read_core_rows(path:Path) -> dict[str,tuple]:
    uri="file:"+quote(path.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1"
    db=sqlite3.connect(uri,uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        actual=tuple(r[1] for r in db.execute("PRAGMA table_info(sets)"))
        if actual!=SET_COLUMNS:
            raise ValueError("unexpected_core_set_schema")
        return {str(r[0]):tuple(r) for r in db.execute("SELECT * FROM sets")}
    finally:
        db.close()


def missing_ids(current_sets:set[str], card_set_refs:dict[str,collections.Counter]) -> set[str]:
    return {sid for refs in card_set_refs.values() for sid in refs if sid not in current_sets}


def make_review_core(active_path:Path, legacy_path:Path, needed:set[str],
                     output:Path) -> dict:
    original=read_core_rows(active_path)
    old=read_core_rows(legacy_path)
    if not needed or len(needed)>500:
        raise ValueError("expected_1_to_500_missing_sets")
    if needed&set(original):
        raise ValueError("attempted_overwrite_of_existing_set")
    if not needed.issubset(old):
        raise ValueError("legacy_core_cannot_resolve_all_sets")
    if output.exists():
        raise FileExistsError("review_core_target_already_exists")
    shutil.copy2(active_path,output)
    db=sqlite3.connect(output)
    try:
        with db:
            for sid in sorted(needed):
                db.execute("INSERT INTO sets VALUES (?,?,?,?,?,?)",old[sid])
            db.execute("UPDATE meta SET value=? WHERE key='setCount'",
                       (str(len(original)+len(needed)),))
        if db.execute("PRAGMA quick_check").fetchone()[0]!="ok":
            raise ValueError("candidate_sqlite_quick_check_failed")
        card_rows=db.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        if card_rows!=0:
            raise ValueError("candidate_core_must_not_contain_cards")
    finally:
        db.close()
    updated=read_core_rows(output)
    if len(updated)!=len(original)+len(needed):
        raise ValueError("candidate_card_set_count_mismatch")
    if any(updated[k]!=v for k,v in original.items()):
        raise ValueError("candidate_changed_original_core_set")
    if any(updated[k]!=old[k] for k in needed):
        raise ValueError("candidate_new_set_payload_differs")
    return {
        "sets_before":len(original),"sets_added":len(needed),
        "sets_after":len(updated),"card_rows":0,
        "original_set_rows_preserved":True,
        "additional_set_rows_exact_from_verified_legacy":True,
        "sqlite_sha256":sha256_file(output),"sqlite_bytes":output.stat().st_size,
    }


def audit_and_build(output:Path,fetch=get) -> dict:
    current=json.loads(fetch(ACTIVE,2_000_000))
    legacy=json.loads(fetch(CANARY,2_000_000))
    selected=[("current",p,pick(current,p)) for p in ACTIVE_PACKS]
    selected += [("archived",p,pick(legacy,p)) for p in ARCHIVE_PACKS]
    selected += [("old-core","core",pick(legacy,"core"))]
    set_union=set()
    card_refs={}
    original_core=legacy_core=None
    pack_evidence=[]
    with tempfile.TemporaryDirectory(prefix="cardscanr-review-core-") as workspace:
        temp=Path(workspace)
        for origin,name,desc in selected:
            compressed=fetch(desc["compressedDatabaseUrl"],MAX_GZIP)
            dest=temp/(origin+"-"+name+".sqlite")
            fact,_,_,_,sets,refs=inspect_pack(desc,compressed,dest)
            pack_evidence.append({
                "origin":origin,"pack_id":name,"cards":fact["card_count"],
                "sqlite_sha256":desc["sha256"],
                "sqlite_integrity_verified":True,
            })
            if origin=="old-core":
                legacy_core=dest
            else:
                set_union.update(sets)
                card_refs[name]=refs
                if origin=="current" and name=="core":
                    original_core=dest
        if original_core is None or legacy_core is None:
            raise ValueError("required_cores_missing")
        required=missing_ids(set_union,card_refs)
        old_sets=set(read_core_rows(legacy_core))
        if not required or not required.issubset(old_sets):
            raise ValueError("unresolved_set_ids_not_all_in_legacy_core")
        before=sum(n for refs in card_refs.values()
                   for sid,n in refs.items() if sid not in set_union)
        output.mkdir(parents=True,exist_ok=True)
        review=output/"review-core.sqlite"
        core_result=make_review_core(original_core,legacy_core,required,review)
        all_set_ids=set_union|required
        after=sum(n for refs in card_refs.values()
                  for sid,n in refs.items() if sid not in all_set_ids)
        if after:
            raise ValueError("candidate_core_did_not_resolve_all_references")
        compressed_path=output/"review-core.sqlite.gz"
        with review.open("rb") as inp,gzip.open(compressed_path,"wb",compresslevel=9) as out:
            shutil.copyfileobj(inp,out,1024*1024)
        report={
            "generated_at_utc":datetime.now(timezone.utc).isoformat(),
            "current_release_id":current.get("catalogueReleaseId"),
            "archived_release_id":legacy.get("catalogueReleaseId"),
            "candidate_core":{**core_result,
                "gzip_sha256":sha256_file(compressed_path),
                "gzip_bytes":compressed_path.stat().st_size,
                "review_only":True,
                "provenance":"exact_missing_set_id_rows_from_archived_core"},
            "unresolved_card_set_references_before":before,
            "unresolved_card_set_references_after":after,
            "pack_receipts":pack_evidence,
            "untouched_existing_pack_count":len(selected)-2,
            "canonical_printing_ids_changed":0,
            "card_image_files_downloaded":0,
            "artwork_redistribution_rights_cleared":False,
            "app_canary_passed":False,
            "safe_to_publish":False,
            "r2_writes":0,"supabase_writes":0,
        }
        (output/"SAFE-68-Set-Review-Core.json").write_text(
            json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        return report


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.output.is_symlink() or (args.output.exists() and (
            not args.output.is_dir() or any(args.output.iterdir()))):
        parser.error("output must be a new empty directory")
    try:
        report=audit_and_build(args.output)
    except (ValueError,FileExistsError,OSError,sqlite3.Error,KeyError,EOFError) as exc:
        parser.exit(2,f"Review-only core builder stopped: {type(exc).__name__}: {exc}\n")
    print(json.dumps({"sets_added":report["candidate_core"]["sets_added"],
                      "unresolved_after":report["unresolved_card_set_references_after"],
                      "safe_to_publish":False,
                      "core_sha256":report["candidate_core"]["sqlite_sha256"]},sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
