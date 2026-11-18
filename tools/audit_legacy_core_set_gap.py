#!/usr/bin/env python3
"""Identify missing set metadata recoverable from the archived August core pack.

Read-only, offline-proof analysis of nine public R2 SQLite packs. Does NOT build
or publish a mixed-release manifest, overwrite current sets or download artwork.
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from tools.preflight_multilingual_crossrelease import (
    ACTIVE_PACKS, ARCHIVE_PACKS, MAX_GZIP, ACTIVE, CANARY, get, inspect_pack
)
from tools.preflight_legacy_korean_pack import pick


def missing_set_analysis(
    pack_refs:dict[str,collections.Counter], current_sets:set[str], legacy_core_sets:set[str]
) -> dict:
    before={}; after={}; necessary=set()
    joined=current_sets|legacy_core_sets
    for pack,refs in sorted(pack_refs.items()):
        before_missing={sid:n for sid,n in refs.items() if sid not in current_sets}
        after_missing={sid:n for sid,n in refs.items() if sid not in joined}
        before[pack]={
            "rows":sum(before_missing.values()),"unique_missing_set_ids":len(before_missing),
            "sample_ids":sorted(before_missing)[:5],
        }
        after[pack]={
            "rows":sum(after_missing.values()),"unique_missing_set_ids":len(after_missing),
            "sample_ids":sorted(after_missing)[:5],
        }
        necessary.update(sid for sid in before_missing if sid in legacy_core_sets)
    return {
        "missing_rows_before_legacy_core":sum(v["rows"] for v in before.values()),
        "missing_rows_after_legacy_core":sum(v["rows"] for v in after.values()),
        "sets_recoverable_from_legacy_core":len(necessary),
        "by_pack_before":before,"by_pack_after":after,
        "sets_requiring_another_source":sorted(set(
            sid for refs in pack_refs.values() for sid in refs if sid not in joined)),
    }


def read_set_data(path:Path) -> dict[str,tuple]:
    db=sqlite3.connect("file:"+quote(path.resolve().as_posix(),safe="/:")+
                       "?mode=ro&immutable=1",uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        return {row[0]:tuple(row[1:]) for row in db.execute(
            "SELECT set_id,language,region,set_name,normalized_set_name,release_date FROM sets")}
    finally:
        db.close()


def audit(active:dict,archive:dict,fetch=get) -> dict:
    packs=[("current",p,pick(active,p)) for p in ACTIVE_PACKS]
    packs += [("canary",p,pick(archive,p)) for p in ARCHIVE_PACKS]
    packs += [("legacy_core_only","core",pick(archive,"core"))]
    pack_refs={}
    pack_set_ids=set()
    legacy_core_sets=set()
    pack_infos=[]
    current_core_data={}
    legacy_core_data={}
    with tempfile.TemporaryDirectory(prefix="cardscanr-legacy-core-audit-") as temp:
        for origin,name,descriptor in packs:
            compressed=fetch(descriptor["compressedDatabaseUrl"],MAX_GZIP)
            path=Path(temp)/(origin+"_"+name+".sqlite")
            info,_,_,_,set_ids,refs=inspect_pack(descriptor,compressed,path)
            info["origin"]=origin
            pack_infos.append(info)
            if origin=="legacy_core_only":
                legacy_core_sets=set(set_ids)
                legacy_core_data=read_set_data(path)
            else:
                pack_set_ids.update(set_ids)
                pack_refs[name]=refs
                if origin=="current" and name=="core":
                    current_core_data=read_set_data(path)
    common=set(current_core_data)&set(legacy_core_data)
    conflicts=[sid for sid in common if current_core_data[sid]!=legacy_core_data[sid]]
    analysis=missing_set_analysis(pack_refs,pack_set_ids,legacy_core_sets)
    # Exact set references are necessary but not sufficient: app-level
    # compatibility and image rights remain separately blocked.
    return {
        "generated_at_utc":datetime.now(timezone.utc).isoformat(),
        "active_release_id":active.get("catalogueReleaseId"),
        "archived_release_id":archive.get("catalogueReleaseId"),
        "packs_checked":len(packs),"pack_receipts":pack_infos,
        "current_core_set_ids":len(current_core_data),
        "archived_core_set_ids":len(legacy_core_data),
        "archived_core_set_ids_absent_from_current_core":len(
            set(legacy_core_data)-set(current_core_data)),
        "shared_core_set_ids_with_differing_metadata":len(conflicts),
        "shared_core_conflict_samples":sorted(conflicts)[:10],
        "cross_pack":analysis,
        "recovered_sets_are_reuse_candidates_only":True,
        "canonical_ids_rewritten":0,
        "app_level_canary_passed":False,
        "image_rights_cleared":False,
        "ready_to_publish":False,
        "r2_writes":0,"supabase_writes":0,"artwork_downloads":0,
    }


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.output.is_symlink() or (args.output.exists() and (
        not args.output.is_dir() or any(args.output.iterdir()))):
        parser.error("output must be a new empty directory")
    try:
        report=audit(json.loads(get(ACTIVE,2_000_000)),json.loads(get(CANARY,2_000_000)))
    except (ValueError,KeyError,sqlite3.Error,OSError,EOFError,RuntimeError) as exc:
        parser.exit(2,f"Legacy core audit safely stopped: {type(exc).__name__}: {exc}\n")
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/"SAFE-Legacy-Core-Set-Gap-Analysis.json").write_text(
        json.dumps(report,indent=2,ensure_ascii=False,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps({"packs_checked":report["packs_checked"],
        "missing_rows_before_legacy_core":report["cross_pack"]["missing_rows_before_legacy_core"],
        "missing_rows_after_legacy_core":report["cross_pack"]["missing_rows_after_legacy_core"],
        "sets_recoverable_from_legacy_core":report["cross_pack"]["sets_recoverable_from_legacy_core"],
        "ready_to_publish":False},sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
