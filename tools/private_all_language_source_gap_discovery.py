#!/usr/bin/env python3
"""Read-only candidate discovery for CardScanR's *unmatched* worldwide source sets.

This takes the seven current TCGdex set indexes and compares them to all eight
SHA256-verified CardScanR source packs. It then collects bounded set-detail/card
brief metadata ONLY for source set IDs not already present in the corresponding
CardScanR pack language. A provider-only set ID is NOT a confirmed missing card:
aliases, translations, reprints and placeholder set indexes are possible.

No image files, customer/ownership data, DB changes, public uploads or rights
assertions. The resulting JSONL is a private, SHA-verified review artifact.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import sqlite3
import tempfile
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote

from tools.preflight_multilingual_crossrelease import CANARY, MAX_GZIP, get, inspect_pack
from tools.preflight_legacy_korean_pack import pick
from tools.tcgdex_metadata_batch import API, LANGUAGES, fetch_json, SET_ID, CARD_ID
from tools.private_artwork_takedown_controls import _safe_image_base

# User preference order: Japanese, Traditional Chinese, Korean, English, Thai,
# Indonesian, then Simplified Chinese as a source-verification safety tail.
PRIORITY=("ja","zh-tw","ko","en","th","id","zh-cn")
MAX_INDEX_ENTRIES=2500
MAX_SET_CARDS=4000
MAX_SOURCE_REQUESTS=80
MAX_SETS_PER_BATCH=60
MIN_PACING_SECONDS=1.0
MAX_PACING_SECONDS=5.0


def _integer(value, maximum=10000):
    return value if type(value) is int and 0<=value<=maximum else None


def validate_index(lang: str, payload, *, quarantined=None) -> list[dict]:
    """Quarantine conflicting provider IDs instead of picking an arbitrary row.

    A live ZH-CN provider roster contained two entries named CSV1C with
    different titles and counts. Both must be excluded from collection until
    source reconciliation, while other set IDs remain usable.
    """
    if lang not in PRIORITY or not isinstance(payload,list) or len(payload)>MAX_INDEX_ENTRIES:
        raise ValueError("untrusted_provider_set_index")
    counts=Counter()
    for item in payload:
        if not isinstance(item,dict):
            raise ValueError("provider_index_nonobject")
        sid=item.get("id")
        if not isinstance(sid,str) or not SET_ID.fullmatch(sid):
            raise ValueError("invalid_provider_set_id")
        counts[sid]+=1
    if quarantined is None:
        quarantined=[]
    results=[]
    duplicate_noted=set()
    for offset,item in enumerate(payload):
        sid=item["id"]
        if counts[sid]>1:
            if sid not in duplicate_noted:
                quarantined.append({
                    "language":lang,"provider_set_id":sid,
                    "occurrences":counts[sid],
                    "status":"quarantined_conflicting_provider_set_id",
                })
                duplicate_noted.add(sid)
            continue
        count=item.get("cardCount")
        if count is not None and not isinstance(count,dict):
            raise ValueError("invalid_card_count")
        count=count or {}
        total=_integer(count.get("total"))
        official=_integer(count.get("official"))
        results.append({
            "language":lang, "provider_set_id":sid,
            "index_position":offset,
            "source_set_name":item.get("name") if isinstance(item.get("name"),str) else None,
            "index_card_count_total":total,
            "index_card_count_official":official,
            "index_has_logo_url":isinstance(item.get("logo"),str),
        })
    return results


def select_batches(index: dict[str,list[dict]], local_keys: set[tuple[str,str]],
                   *, batch_index:int, batch_size:int,
                   other_provider_suffix_keys=None) -> tuple[list[dict],dict]:
    if type(batch_index) is not int or batch_index<0 or batch_index>100:
        raise ValueError("invalid_batch_index")
    if type(batch_size) is not int or not 1<=batch_size<=MAX_SETS_PER_BATCH:
        raise ValueError("invalid_batch_size")
    suffix=other_provider_suffix_keys or set()
    selected=[]
    counts={}
    for lang in PRIORITY:
        rows=index.get(lang)
        if not isinstance(rows,list):
            raise ValueError("missing_provider_index_language")
        absent=[]
        for x in rows:
            key=(lang,x["provider_set_id"])
            if key in local_keys:
                continue
            candidate=dict(x)
            candidate["cross_provider_set_code_collision"]=(key in suffix)
            candidate["candidate_status"]=(
                "possible_cross_provider_alias" if key in suffix
                else "provider_set_id_not_in_pack")
            absent.append(candidate)
        # Prefer genuinely unrepresented set codes over likely cross-provider
        # aliases, then newest source index entries within each group.
        absent.sort(key=lambda r:(
            not r["cross_provider_set_code_collision"], r["index_position"]
        ),reverse=True)
        selected.extend(absent)
        counts[lang]={
            "source_index_sets":len(rows),
            "source_set_ids_without_same_provider_pack_match":len(absent),
            "possible_cross_provider_alias_codes":sum(
                x["cross_provider_set_code_collision"] for x in absent),
            "source_reported_card_positions":sum(
                x["index_card_count_total"] or 0 for x in absent),
        }
    start=batch_index*batch_size
    selected_page=selected[start:start+batch_size]
    alias_count=sum(x["cross_provider_set_code_collision"] for x in selected)
    return selected_page,{
        "total_source_unmatched_set_ids":len(selected),
        "possible_cross_provider_alias_codes":alias_count,
        "source_set_codes_without_any_pack_suffix_match":len(selected)-alias_count,
        "source_reported_card_positions_unmatched":sum(
            x["index_card_count_total"] or 0 for x in selected),
        "counts_by_language":counts,"batch_index":batch_index,
        "batch_size":batch_size,"total_batches":
            (len(selected)+batch_size-1)//batch_size,
        "selection_is_provisional_not_new_cards":True,
    }


def summarize_set(lang: str, sid: str, payload, raw: bytes) -> tuple[dict,list[dict]]:
    if not isinstance(payload,dict) or payload.get("id")!=sid:
        raise ValueError("set_detail_id_drift")
    cards=payload.get("cards")
    if not isinstance(cards,list) or len(cards)>MAX_SET_CARDS:
        raise ValueError("set_detail_cards_invalid_or_over_budget")
    source_sha=hashlib.sha256(raw).hexdigest()
    card_count=payload.get("cardCount")
    if card_count is not None and not isinstance(card_count,dict):
        raise ValueError("set_detail_card_count_invalid")
    card_count=card_count or {}
    seen=set()
    brief=[]
    art_count=0
    for card in cards:
        if not isinstance(card,dict):
            raise ValueError("source_card_not_object")
        cid=card.get("id")
        local=card.get("localId")
        name=card.get("name")
        if (not isinstance(cid,str) or not CARD_ID.fullmatch(cid)
                or not cid.startswith(sid+"-") or cid in seen):
            raise ValueError("duplicate_or_unexpected_source_card_id")
        seen.add(cid)
        if (local is not None and (not isinstance(local,str) or len(local)>80)
                or name is not None and (not isinstance(name,str) or len(name)>300)):
            raise ValueError("source_card_fields_over_budget")
        image=card.get("image")
        permitted=isinstance(image,str) and _safe_image_base(image)
        if permitted: art_count+=1
        brief.append({
            "provider":"tcgdex","language":lang,"provider_set_id":sid,
            "provider_card_id":cid,"provider_local_id":local,
            "native_card_name":name,
            "source_image_base_url":image if permitted else None,
            "source_image_ref_present":bool(permitted),
            "source_set_response_sha256":source_sha,
            "canonical_printing_id":None, # MUST reconcile before insert
            "exact_match_verified":False,
            "artwork_rights_verified":False,"image_binary_downloaded":False,
        })
    set_info={
        "provider":"tcgdex","language":lang,"provider_set_id":sid,
        "native_set_name":payload.get("name") if isinstance(payload.get("name"),str) else None,
        "release_date":payload.get("releaseDate"),
        "source_card_index_rows":len(cards),
        "provider_reported_card_total":_integer(card_count.get("total")),
        "provider_reported_card_official":_integer(card_count.get("official")),
        "image_base_references":art_count,
        "source_url":API+"/"+lang+"/sets/"+sid,
        "source_response_sha256":source_sha,
        "exact_pack_set_id_match":False,
        "candidate_for_alias_or_new_set_review":True,
        "new_CardScanR_cards_confirmed":0,
        "downloaded_image_files":0,
        "artwork_rights_verified":False,
    }
    return set_info,brief


def collect(*, batch_index:int, batch_size:int=40, pace:float=1.25,
            pack_fetch=get, provider_fetch=fetch_json):
    if type(pace) not in (int,float) or not MIN_PACING_SECONDS<=pace<=MAX_PACING_SECONDS:
        raise ValueError("unsafe_source_request_pacing")
    if batch_index<0 or batch_index>100 or not 1<=batch_size<=MAX_SETS_PER_BATCH:
        raise ValueError("invalid_batch_request")
    source_manifest=json.loads(pack_fetch(CANARY,2_000_000))
    local_keys=set()
    all_pack_suffix_keys=set()
    quarantined_source_set_ids=[]
    pack_evidence=[]
    indexes={}
    index_evidence=[]
    detail=[]
    briefs=[]
    failures=[]
    requests=0
    stopped_429=False
    with tempfile.TemporaryDirectory(prefix="cardscanr-provider-gap-") as td:
        for lang in PRIORITY:
            descriptor=pick(source_manifest,lang)
            compressed=pack_fetch(descriptor["compressedDatabaseUrl"],MAX_GZIP)
            file=Path(td)/(lang+".sqlite")
            inspect_pack(descriptor,compressed,file)
            uri="file:"+quote(file.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1"
            with closing(sqlite3.connect(uri,uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                keys=db.execute("SELECT DISTINCT language,set_id FROM cards").fetchall()
                for actual_lang,setid in keys:
                    if actual_lang!=lang:
                        raise ValueError("pack_card_language_drift")
                    if isinstance(setid,str):
                        code=setid.rsplit(":",1)[-1]
                        all_pack_suffix_keys.add((lang,code))
                        if setid.startswith("tcgdex:"):
                            local_keys.add((lang,code))
            pack_evidence.append({
                "language":lang,"sha256":descriptor["sha256"],
                "gzip_sha256":descriptor["gzipSha256"],
                "pack_card_count":descriptor["recordCount"],
                "distinct_pack_source_sets":len(keys),
            })
        for lang in PRIORITY:
            payload,raw=provider_fetch(API+"/"+lang+"/sets")
            if not isinstance(raw,bytes) or len(raw)>2_000_000:
                raise ValueError("index_source_response_too_large")
            indexes[lang]=validate_index(lang,payload,quarantined=quarantined_source_set_ids)
            index_evidence.append({
                "language":lang,"url":API+"/"+lang+"/sets",
                "sha256":hashlib.sha256(raw).hexdigest(),
                "set_count":len(indexes[lang]),
            })
        selected,plan=select_batches(
            indexes,local_keys,batch_index=batch_index,batch_size=batch_size,
            other_provider_suffix_keys=all_pack_suffix_keys)
        for target in selected:
            if requests>=MAX_SOURCE_REQUESTS:
                raise ValueError("source_request_budget_exceeded")
            if requests>0:
                time.sleep(pace)
            lang,sid=target["language"],target["provider_set_id"]
            requests+=1
            try:
                payload,raw=provider_fetch(API+"/"+lang+"/sets/"+sid)
                if not isinstance(raw,bytes) or len(raw)>2_000_000:
                    raise ValueError("set_source_response_too_large")
                item,cards=summarize_set(lang,sid,payload,raw)
            except (ValueError,KeyError,TypeError,URLError,HTTPError) as exc:
                failures.append({
                    "language":lang,"provider_set_id":sid,
                    "error_type":type(exc).__name__,
                    "http_status":getattr(exc,"code",None),
                })
                if isinstance(exc,HTTPError) and exc.code==429:
                    stopped_429=True
                    break
                continue
            item["index_card_count_total"]=target["index_card_count_total"]
            item["index_position"]=target["index_position"]
            item["candidate_status"]=target["candidate_status"]
            item["cross_provider_set_code_collision"]=target["cross_provider_set_code_collision"]
            detail.append(item)
            briefs.extend(cards)
    ids=[(x["language"],x["provider_card_id"]) for x in briefs]
    if len(set(ids))!=len(ids):
        raise ValueError("same_source_card_id_repeated")
    report={
        "classification":"PRIVATE_SOURCE_SET_GAP_EVIDENCE_ONLY",
        "generated_utc":datetime.now(timezone.utc).isoformat(),
        "packs_sha256_verified":pack_evidence,
        "provider_index_sha256_verified":index_evidence,
        "quarantined_provider_set_id_conflicts":quarantined_source_set_ids,
        **plan,
        "source_requests":requests,
        "source_sets_succeeded":len(detail),
        "source_sets_failed":len(failures),
        "failures":failures,"stopped_for_429":stopped_429,
        "card_briefs_captured":len(briefs),
        "source_image_url_bases_found":sum(x["source_image_ref_present"] for x in briefs),
        "verified_new_canonical_cards":0,
        "source_set_identities_not_reconciled":len(detail),
        "production_writes":0,"customer_data_read":False,
        "image_binaries_downloaded":0,"rights_granted":False,
        "safe_for_public_use":False,
        "provider_only_archive":True,
    }
    return detail,briefs,report


def save(output:Path,sets:list[dict],cards:list[dict],report:dict) -> None:
    if output.is_symlink() or output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FileExistsError("output_requires_fresh_empty_directory")
    output.mkdir(parents=True,exist_ok=True)
    for name,records in (("unmatched_source_sets.jsonl",sets),
                         ("unmatched_source_card_briefs.jsonl",cards)):
        data="".join(json.dumps(item,ensure_ascii=False,sort_keys=True)+"\n"
                     for item in records).encode("utf-8")
        (output/name).write_bytes(data)
        report[name]={"records":len(records),"sha256":hashlib.sha256(data).hexdigest(),
                      "bytes":len(data)}
    (output/"receipt.json").write_text(
        json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n",
        encoding="utf-8")


def main()->int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--batch-index",type=int,required=True)
    p.add_argument("--batch-size",type=int,default=40)
    p.add_argument("--pace",type=float,default=1.25)
    args=p.parse_args()
    result=collect(batch_index=args.batch_index,batch_size=args.batch_size,pace=args.pace)
    save(args.output,*result)
    summary=result[2]
    print(json.dumps({
        "result":"PRIVATE_GAP_DISCOVERY",
        "index_unmatched_source_set_ids":summary["total_source_unmatched_set_ids"],
        "potential_source_card_positions":summary["source_reported_card_positions_unmatched"],
        "detail_sets_fetched":summary["source_sets_succeeded"],
        "card_briefs_collected":summary["card_briefs_captured"],
        "source_image_refs_found":summary["source_image_url_bases_found"],
        "possible_alias_set_codes":summary["possible_cross_provider_alias_codes"],
        "quarantined_source_id_conflicts":len(summary["quarantined_provider_set_id_conflicts"]),
        "no_canonical_card_additions":True,"no_image_files":True,
        "batch_index":args.batch_index,"batch_size":args.batch_size,
        "total_batches":summary["total_batches"],"failed":summary["source_sets_failed"]
    },ensure_ascii=False,sort_keys=True))
    return 0 if summary["source_sets_succeeded"]>0 or summary["batch_index"]>=summary["total_batches"] else 2


if __name__=="__main__":
    raise SystemExit(main())
