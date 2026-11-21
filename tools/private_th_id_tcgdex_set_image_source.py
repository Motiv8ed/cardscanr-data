#!/usr/bin/env python3
"""Bounded Thai/Indonesian TCGdex set source-URL collector; never downloads images.

Only TCGdex-declared 'image' bases are accepted. The documented /low.webp
quality endpoint is derived ONLY when 'image' is explicitly present and its
path equals the returned card's own set and local printed number. Importability,
live ownership and image rights remain unverified until separately checked.
"""
from __future__ import annotations
import argparse, hashlib, json, re, time
from pathlib import Path
from urllib import request
from urllib.parse import urlsplit

BASE="https://api.tcgdex.net/v2"
HOST="assets.tcgdex.net"
SETS={"th":["SV7s","SV4K","SV3","SV2a"],
      "id":["SV4a","SV6s","SV7a","SV5a"]}
SETCODE=re.compile(r"^[A-Za-z0-9_-]{2,32}$")
CARDID=re.compile(r"^[A-Za-z0-9_-]{1,48}$")
NUMBER=re.compile(r"^[A-Za-z0-9-]{1,15}$")
USER_AGENT="CardScanR-Private-TCGdex-Card-URL-Research/1.0"


def safe_base(img,language,code,local):
    if not isinstance(img,str):
        return False
    try:
        u=urlsplit(img)
        p=u.path.split("/")
        return (u.scheme=="https" and u.hostname==HOST and u.port in (None,443)
                and not u.username and not u.password and not u.query and not u.fragment
                and len(p)==5 and p[0]=="" and p[1]==language
                and p[3]==code and p[4]==local
                and SETCODE.fullmatch(p[2]) is not None)
    except ValueError:
        return False


def set_url(lang,code):
    if lang not in SETS or code not in SETS[lang]:
        raise ValueError("set_not_allowlisted")
    return BASE+"/"+lang+"/sets/"+code


def fetch_json(lang,code):
    url=set_url(lang,code)
    req=request.Request(url,headers={"Accept":"application/json","User-Agent":USER_AGENT})
    with request.urlopen(req,timeout=35) as res:
        if res.status!=200 or "json" not in res.headers.get("Content-Type","").lower():
            raise ValueError("source_response_not_json_200")
        raw=res.read(1_900_001)
        if len(raw)>1_900_000:
            raise ValueError("set_response_over_limit")
        return json.loads(raw),hashlib.sha256(raw).hexdigest()


def parse_set(doc,lang,code,digest):
    if (not isinstance(doc,dict) or doc.get("id")!=code
            or not isinstance(doc.get("cards"),list)):
        raise ValueError("set_identity_missing_or_unexpected_schema")
    cards=doc["cards"]
    if not 1<=len(cards)<=750:
        raise ValueError("set_card_count_bad")
    rows=[]
    seen=set()
    for card in cards:
        cid=card.get("id")
        name=card.get("name")
        num=card.get("localId")
        if (not isinstance(cid,str) or not CARDID.fullmatch(cid)
            or not isinstance(num,str) or not NUMBER.fullmatch(num)
            or not isinstance(name,str) or not 1<=len(name)<=300
            or not cid.startswith(code+"-") or cid.split("-",1)[1]!=num
            or cid in seen):
            raise ValueError("returned_card_identity_bad_or_duplicate")
        seen.add(cid)
        base=card.get("image")
        accepted=safe_base(base,lang,code,num)
        rows.append({
            "classification":"TCGDEX_DECLARED_LANGUAGE_EXACT_SET_CARD_IMAGE_SOURCE_ONLY",
            "language":lang,"provider":"tcgdex","set_code":code,
            "provider_card_id":cid,"local_collector_number":num,
            "source_native_name":name,
            "source_image_base":base if accepted else None,
            "source_low_webp_url":base+"/low.webp" if accepted else None,
            "source_image_declared":accepted,
            "source_response_sha256":digest,
            "current_live_identity_verified":False,
            "commercial_artwork_rights_verified":False,
            "image_binaries_downloaded":0,
            "current_live_vault_links_added":0
        })
    return rows


def collect(pairs,out,pace=0.7):
    if out.exists() or out.is_symlink():
        raise FileExistsError("output_must_be_new")
    if not 0.5<=pace<=5 or not 1<=len(pairs)<=8 or len(set(pairs))!=len(pairs):
        raise ValueError("invalid_bulk_request_bounds")
    out.mkdir(parents=True)
    allrows=[];sources=[];errors=[]
    for pos,(lang,code) in enumerate(pairs):
        if pos:time.sleep(pace)
        try:
            response,sha=fetch_json(lang,code)
            rows=parse_set(response,lang,code,sha)
            allrows.extend(rows)
            sources.append({"language":lang,"set_code":code,
                "source_url":set_url(lang,code),
                "source_response_sha256":sha,
                "cards":len(rows),
                "provider_declared_image_urls":sum(r["source_image_declared"] for r in rows)})
        except Exception as exc:
            errors.append({"language":lang,"set_code":code,
                           "error":type(exc).__name__+":"+str(exc)[:140]})
    ordered=sorted(allrows,key=lambda r:(r["language"],r["set_code"],r["provider_card_id"]))
    text="".join(json.dumps(r,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n"
                 for r in ordered)
    data=text.encode("utf8")
    (out/"provider_card_sources.jsonl").write_bytes(data)
    receipt={
        "classification":"PRIVATE_TH_ID_TCGDEX_REAL_SET_CARD_IMAGE_URL_REFS_ONLY",
        "source_pages_attempted":len(pairs),"source_pages_succeeded":len(sources),
        "source_pages_failed":len(errors),
        "source_records":len(ordered),
        "provider_declared_language_image_bases":sum(r["source_image_declared"] for r in ordered),
        "source_cards_with_no_declared_image":sum(not r["source_image_declared"] for r in ordered),
        "source_url_manifest_sha256":hashlib.sha256(data).hexdigest(),
        "sources":sources,"errors":errors,
        "actual_live_new_links":0,"actual_live_new_canonical_cards":0,
        "images_downloaded":0,"artwork_rights_verified":False,
        "public_artwork_enabled":False,
        "further_live_name_set_number_and_variant_matching_required":True
    }
    (out/"receipt.json").write_text(json.dumps(receipt,sort_keys=True,
                        ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    return receipt


def self_test():
    good="https://assets.tcgdex.net/th/SV/SV7s/001"
    assert safe_base(good,"th","SV7s","001")
    assert not safe_base(good,"id","SV7s","001")
    assert not safe_base(good,"th","SV7s","002")
    assert not safe_base(good.replace(HOST,"attacker.example"),"th","SV7s","001")
    assert not safe_base(good+"?query=1","th","SV7s","001")
    good_id="https://assets.tcgdex.net/id/SV/SV6s/001"
    assert safe_base(good_id,"id","SV6s","001")
    sample={"id":"SV7s","cards":[{"id":"SV7s-001","localId":"001",
                   "name":"เรดิบา","image":good}]}
    hits=parse_set(sample,"th","SV7s","a"*64)
    assert len(hits)==1 and hits[0]["source_low_webp_url"]==good+"/low.webp"
    assert hits[0]["image_binaries_downloaded"]==0
    print("PASS TCGdex actual declared image only, strict language/set/number, no binaries")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--out",type=Path)
    p.add_argument("--self-test",action="store_true")
    p.add_argument("--pilot",action="store_true")
    a=p.parse_args()
    if a.self_test:self_test()
    else:
        if not a.out:p.error("--out required")
        pairs=[("th","SV7s"),("id","SV4a")] if a.pilot else [
            (l,s) for l,codes in SETS.items() for s in codes]
        r=collect(pairs,a.out)
        print("TH_ID_REAL_IMAGE_URL_SOURCE_RECEIPT",json.dumps({
             k:r[k] for k in ("source_pages_attempted","source_pages_succeeded",
             "source_pages_failed","source_records",
             "provider_declared_language_image_bases",
             "source_cards_with_no_declared_image","actual_live_new_links")
        },sort_keys=True))
