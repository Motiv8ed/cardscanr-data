#!/usr/bin/env python3
"""29-set private image-reference acquisition with exact canonical printing joins.

Targets from the 19-Nov verified 158803-row gap audit and archived 808-entry
TCGdex roster. Fetches 4 SHA-pinned immutable language packs and up to 29
TCGdex set JSON payloads only, never image binary data, customer records,
unlicensed third-party APIs, or public manifests.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sqlite3
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from tools.preflight_multilingual_crossrelease import CANARY, MAX_GZIP, get, inspect_pack
from tools.preflight_legacy_korean_pack import pick
from tools.tcgdex_metadata_batch import API, fetch_json
from tools.private_image_identity_crosswalk import crosswalk_rows
from tools.private_artwork_takedown_controls import _safe_image_base

# (language, provider-set-id, verified pack image URL gap count)
# All unique exact set-language IDs from PR70/PR71; no CN alias substitutions.
TARGETS = (
    ("zh-tw","SV8a",237),("id","SV3s",227),
    ("id","SV2a",207),("zh-tw","SV2a",207),
    ("id","SV5s",194),("zh-tw","S4a",190),
    ("id","SV9s",185),("th","SV9s",185),
    ("id","SV4s",184),("th","SV8s",182),
    ("zh-tw","S8b",181),("th","S12a",172),
    ("zh-tw","SC1a",172),("id","SVDs",171),
    ("th","SVDs",171),("zh-tw","SC1b",171),
    ("zh-tw","SCC",171),("id","SV6s",167),
    ("id","SV7s",166),("zh-tw","SC1D",164),
    ("zh-tw","SCD",159),("th","SV4a",150),
    ("th","SV3",138),("th","SV6",101),
    ("th","S12",98),("th","SV5M",97),
    ("ko","SV4K",87),("id","SV1S",78),
    ("ko","SV5K",24),
)
EXPECTED_GAP = 4636
LANGUAGES = ("zh-tw","id","th","ko")
COLS = ("canonical_base_id","canonical_printing_id","set_id","language",
        "printed_collector_number","native_card_name","image_thumbnail_url","image_display_url")
MAX_SOURCE_RESPONSE = 2_000_000


def validate_targets(targets: tuple = TARGETS) -> None:
    if not targets or len(targets) > 40 or len(set((a,b) for a,b,_ in targets)) != len(targets):
        raise ValueError("invalid_target_count_or_duplicate")
    if any(lang not in LANGUAGES or not isinstance(sid,str) or
           not sid.replace("-","").replace("_","").isalnum() or
           not isinstance(gap,int) or gap < 1 for lang,sid,gap in targets):
        raise ValueError("bad_target_language_set_or_gap")


def set_brief(payload: dict, lang: str, sid: str) -> dict:
    if not isinstance(payload,dict) or payload.get("id") != sid or not isinstance(payload.get("cards"),list):
        raise ValueError("invalid_provider_set_identity_or_cards")
    ids=[r.get("id") for r in payload["cards"] if isinstance(r,dict)]
    if len(ids) != len(payload["cards"]) or len(ids) != len(set(ids)) or any(not isinstance(i,str) for i in ids):
        raise ValueError("duplicate_or_malformed_provider_cards")
    counts=payload.get("cardCount")
    return {
        "language":lang,"provider_set_id":sid,
        "native_set_name":payload.get("name") if isinstance(payload.get("name"),str) else None,
        "release_date":payload.get("releaseDate") if isinstance(payload.get("releaseDate"),str) else None,
        "official_card_count":counts.get("official") if isinstance(counts,dict) else None,
        "total_card_count":counts.get("total") if isinstance(counts,dict) else None,
        "source_set_card_index_rows":len(ids),
        "source_logo_url_reference":payload.get("logo") if isinstance(payload.get("logo"),str) else None,
        "source_symbol_url_reference":payload.get("symbol") if isinstance(payload.get("symbol"),str) else None,
        "set_details_only":True,"rights_cleared":False,
    }


def run(*, fetch=get, source_fetch=fetch_json, pace_seconds:float=0.75, targets:tuple=TARGETS) -> tuple[list[dict],dict]:
    validate_targets(targets)
    if not 0 <= pace_seconds <= 3:
        raise ValueError("invalid_request_pace")
    manifest=json.loads(fetch(CANARY,2_000_000))
    results: list[dict]=[]
    set_evidence: list[dict]=[]
    error_evidence: list[dict]=[]
    request_count=0
    stopped_for_throttle=False
    validated_packs=[]
    with tempfile.TemporaryDirectory(prefix="cardscanr-29sets-") as td:
        for lang in LANGUAGES:
            group=[(sid,gap) for language,sid,gap in targets if language==lang]
            if not group:
                continue
            desc=pick(manifest,lang)
            compressed=fetch(desc["compressedDatabaseUrl"],MAX_GZIP)
            destination=Path(td)/(lang+".sqlite")
            inspect_pack(desc,compressed,destination)
            validated_packs.append({"language":lang,"sha256":desc["sha256"]})
            uri="file:"+quote(destination.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1"
            with sqlite3.connect(uri,uri=True) as db:
                db.execute("PRAGMA query_only=ON")
                for sid,expected_gap in group:
                    canonical_set="tcgdex:asia:set:"+sid
                    pack_rows=[dict(zip(COLS,row)) for row in db.execute(
                        "SELECT "+",".join(COLS)+" FROM cards WHERE set_id=? AND language=?",
                        (canonical_set,lang),
                    )]
                    actual_gap=sum(not(r["image_thumbnail_url"] or r["image_display_url"]) for r in pack_rows)
                    if not pack_rows or actual_gap != expected_gap:
                        raise ValueError("audited_pack_gap_changed:"+lang+"/"+sid)
                    url=API+"/"+lang+"/sets/"+sid
                    if request_count and pace_seconds:
                        time.sleep(pace_seconds)
                    request_count+=1
                    try:
                        payload,body=source_fetch(url)
                        if not isinstance(body,bytes) or len(body)>MAX_SOURCE_RESPONSE:
                            raise ValueError("source_json_response_oversized")
                        brief=set_brief(payload,lang,sid)
                        matches,match_report=crosswalk_rows(pack_rows,payload["cards"],
                                                          language=lang,source_set_id=sid)
                    except (OSError, ValueError, TypeError, KeyError, HTTPError, URLError) as exc:
                        kind=type(exc).__name__
                        error_evidence.append({"language":lang,"source_set_id":sid,"error_type":kind,
                                               "http_status":getattr(exc,"code",None)})
                        if isinstance(exc,HTTPError) and exc.code==429:
                            stopped_for_throttle=True
                            break
                        continue
                    sha=hashlib.sha256(body).hexdigest()
                    for row in matches:
                        if not row["candidate_for_missing_pack_image_ref"]:
                            continue
                        base=row["source_image_base_url"]
                        if not _safe_image_base(base):
                            row["candidate_for_missing_pack_image_ref"]=False
                            row["source_image_validation"]="invalid_asset_url"
                        else:
                            row["source_image_validation"]="allowlisted_url_reference"
                            row["provider_low_webp_url"]=base+"/low.webp"
                    candidates=sum(bool(r["candidate_for_missing_pack_image_ref"]) for r in matches)
                    set_evidence.append({
                        **brief,
                        "source_url":url,"source_sha256":sha,
                        "pack_sha256_verified":True,
                        "verified_pack_gap":expected_gap,
                        "exact_unique_matches":match_report["status_counts"].get("exact_unique",0),
                        "ambiguous_matches":match_report["status_counts"].get("ambiguous",0),
                        "unmatched":match_report["status_counts"].get("unmatched",0),
                        "missing_image_with_valid_source_url":candidates,
                        "source_image_references_present":match_report["source_cards_with_image_url"],
                        "card_artwork_files_downloaded":0,
                    })
                    for row in matches:
                        row["source_set_sha256"]=sha
                        row["source_url"]=url
                    results.extend(matches)
                if stopped_for_throttle:
                    break
    unique_printings=[r["canonical_printing_id"] for r in results if r["status"]=="exact_unique"]
    if len(unique_printings)!=len(set(unique_printings)):
        raise ValueError("duplicate_canonical_printing_across_sets")
    counts=Counter(x["status"] for x in results)
    ready=[r for r in results if r["candidate_for_missing_pack_image_ref"]]
    report={
        "classification":"PRIVATE_29_SET_IMAGE_REFERENCE_CROSSWALK",
        "generated_utc":datetime.now(timezone.utc).isoformat(),
        "source_target_count":len(targets),
        "audited_target_gap":sum(gap for _,_,gap in targets),
        "source_set_requests":request_count,
        "source_request_cap":len(targets),
        "source_sets_succeeded":len(set_evidence),
        "source_sets_failed":len(error_evidence),
        "throttle_stop":stopped_for_throttle,
        "card_rows_reconciled":len(results),
        "exact_unique_printing_matches":len(unique_printings),
        "status_counts":dict(sorted(counts.items())),
        "valid_missing_image_ref_candidates":len(ready),
        "per_language":{l:{
            "source_sets":sum(x["language"]==l for x in set_evidence),
            "exact_unique":sum(r["language"]==l and r["status"]=="exact_unique" for r in results),
            "new_image_ref_candidates":sum(r["language"]==l for r in ready),
        } for l in LANGUAGES},
        "set_evidence":set_evidence,
        "source_failures":error_evidence,
        "sha_verified_language_packs":validated_packs,
        "image_binaries_downloaded":0,
        "production_writes":0,
        "customer_ownership_read":False,
        "canonical_id_changes":False,
        "artwork_rights_cleared":False,
        "safe_to_publish":False,
    }
    if request_count>len(targets):
        raise AssertionError("request_budget_exceeded")
    return results,report


def save(out:Path,rows:list[dict],report:dict)->dict:
    if out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
        raise FileExistsError("output_must_be_new_or_empty")
    out.mkdir(parents=True,exist_ok=True)
    for name,records in (
        ("all_exact_and_unmatched_rows.jsonl",rows),
        ("valid_image_candidates.jsonl",[r for r in rows if r["candidate_for_missing_pack_image_ref"]]),
    ):
        payload="".join(json.dumps(x,ensure_ascii=False,sort_keys=True)+"\n" for x in records).encode()
        (out/name).write_bytes(payload)
        report[name]={"records":len(records),"bytes":len(payload),
                      "sha256":hashlib.sha256(payload).hexdigest()}
    (out/"receipt.json").write_text(
        json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8",
    )
    return report


def main()->int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",required=True,type=Path)
    p.add_argument("--pace-seconds",type=float,default=0.75)
    args=p.parse_args()
    rows,report=run(pace_seconds=args.pace_seconds)
    save(args.output,rows,report)
    print(json.dumps({
        "status":"COMPLETE" if report["source_sets_succeeded"]==report["source_target_count"] else "PARTIAL",
        "source_sets_succeeded":report["source_sets_succeeded"],
        "exact_unique":report["exact_unique_printing_matches"],
        "card_rows":report["card_rows_reconciled"],
        "image_ref_candidates":report["valid_missing_image_ref_candidates"],
        "by_language":report["per_language"],
        "failed":report["source_failures"],
        "no_artwork_binary_download":True,"safe_to_publish":False,
    },sort_keys=True))
    return 0 if report["source_sets_succeeded"] else 2


if __name__=="__main__":
    raise SystemExit(main())
