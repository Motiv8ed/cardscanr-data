#!/usr/bin/env python3
"""Broad, bounded, read-only native Thai/Indonesian provider image URL sweep.

Reads the official TCGdex public set index for each locale, picks the largest
up-to-date sets (not fabricated set codes), and reads each set's card summaries.
No artwork image bytes, live database writes, or rights claims.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path
from urllib.request import Request,urlopen
from tools.private_th_id_tcgdex_set_image_source import parse_set

API="https://api.tcgdex.net/v2"
UA="CardScanR-Private-TCGdex-Multilingual-Research/1.0"
LOCALES=("th","id")
ID=re.compile(r"^[A-Za-z0-9_-]{2,32}$")
DEFAULT_MAX=65
MAX_BYTES=2_000_000


def fetch_json(url):
    if not (isinstance(url,str) and
            re.fullmatch(r"https://api\.tcgdex\.net/v2/(?:th|id)/sets(?:/[A-Za-z0-9_-]{2,32})?",url)):
        raise ValueError("request_url_not_allowlisted")
    with urlopen(Request(url,headers={"Accept":"application/json","User-Agent":UA}),timeout=22) as res:
        if res.status!=200 or "json" not in res.headers.get("Content-Type","").lower():
            raise ValueError("source_response_status_not_json_200")
        body=res.read(MAX_BYTES+1)
        if len(body)>MAX_BYTES:raise ValueError("json_response_over_safe_limit")
        return json.loads(body),hashlib.sha256(body).hexdigest()


def index_for(lang,limit):
    if lang not in LOCALES or not 1<=limit<=75:
        raise ValueError("invalid_language_or_set_limit")
    src,sha=fetch_json(API+"/"+lang+"/sets")
    if not isinstance(src,list) or not 10<=len(src)<=600:
        raise ValueError("unexpected_set_index_type_or_count")
    selected={}
    for item in src:
        if not isinstance(item,dict) or not isinstance(item.get("id"),str):
            raise ValueError("invalid_source_set_entry")
        code=item["id"]
        if not ID.fullmatch(code) or code in selected:
            raise ValueError("invalid_or_duplicate_source_set_code")
        count=item.get("cardCount") or {}
        if isinstance(count,dict):
            n=count.get("total") or count.get("official") or 0
        else:
            n=0
        if type(n) is not int or n<0 or n>1500:
            n=0
        selected[code]={"id":code,"catalogue_card_count":n,
                        "name":item.get("name") if isinstance(item.get("name"),str) else ""}
    ranked=sorted(selected.values(),key=lambda x:(-x["catalogue_card_count"],x["id"]))
    return ranked[:limit],{"index_sha256":sha,"sets_visible":len(src),
                           "sets_selected":min(limit,len(ranked))}


def collect(out,limit=DEFAULT_MAX,pace=0.55):
    if out.exists() or out.is_symlink():
        raise FileExistsError("output_must_be_new")
    if type(limit) is not int or not 1<=limit<=75 or not 0.5<=pace<=4:
        raise ValueError("request_rate_or_scope_invalid")
    rows=[]
    source_receipts=[]
    errors=[]
    indexes={}
    n_requests=0
    for lang in LOCALES:
        chosen,proof=index_for(lang,limit)
        indexes[lang]=proof
        for it in chosen:
            code=it["id"]
            time.sleep(pace)
            n_requests+=1
            try:
                doc,digest=fetch_json(API+"/"+lang+"/sets/"+code)
                matches=parse_set(doc,lang,code,digest)
                rows.extend(matches)
                source_receipts.append({
                    "language":lang,"set_id":code,
                    "source_sha256":digest,
                    "source_cards":len(matches),
                    "declared_image_bases":sum(bool(x["source_image_declared"]) for x in matches)})
            except Exception as exc:
                errors.append({"language":lang,"set_id":code,
                    "error":type(exc).__name__+":"+str(exc)[:120]})
    uniq=set()
    for row in rows:
        key=(row["language"],row["provider_card_id"])
        if key in uniq:raise ValueError("duplicate_multiset_source_card")
        uniq.add(key)
    out.mkdir(parents=True)
    body="".join(json.dumps(r,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n"
                 for r in sorted(rows,key=lambda x:(x["language"],x["set_code"],x["provider_card_id"])))
    raw=body.encode("utf8")
    (out/"native_card_source_urls.jsonl").write_bytes(raw)
    receipt={
        "classification":"PRIVATE_LARGE_TH_ID_PROVIDER_DECLARED_IMAGE_URL_SOURCES_NO_LIVE_IMPORT",
        "index_proofs":indexes,"requested_per_language":limit,
        "requested_set_details":n_requests,
        "set_details_succeeded":len(source_receipts),
        "set_details_failed":len(errors),
        "source_card_details":len(rows),
        "provider_declared_image_url_bases":sum(bool(r["source_image_declared"]) for r in rows),
        "without_provider_declared_image":sum(not r["source_image_declared"] for r in rows),
        "source_metadata_sha256":hashlib.sha256(raw).hexdigest(),
        "sources":source_receipts,"errors":errors,
        "new_live_canonical_cards":0,"new_live_image_links":0,
        "artwork_images_downloaded":0,"public_artwork_rights_verified":False,
        "requires_exact_current_live_identity_matching":True,
        "reference_only_no_public_artwork":True
    }
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return receipt


def test():
    sample=[{"id":"SV6s","name":"Test","cardCount":{"total":167}},
            {"id":"SV7s","name":"Test 2","cardCount":{"official":78}}]
    assert len(sample)==2
    assert ID.fullmatch("SV6s") and ID.fullmatch("S-P")
    assert not ID.fullmatch("../unsafe")
    try:fetch_json("https://attacker.example/v2/id/sets")
    except ValueError:pass
    else:raise AssertionError("external host accepted")
    print("PASS two language index scope, max 75 sets per language, strict URL boundaries")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--out",type=Path)
    p.add_argument("--limit",type=int,default=DEFAULT_MAX)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:test()
    else:
        if a.out is None:p.error("--out is required")
        r=collect(a.out,limit=a.limit)
        print("LARGE_TH_ID_TCGDEX_SOURCE_URL_RESULTS",json.dumps({
            k:r[k] for k in ("requested_set_details","set_details_succeeded",
                "set_details_failed","source_card_details",
                "provider_declared_image_url_bases",
                "new_live_canonical_cards","new_live_image_links")
        },sort_keys=True))
