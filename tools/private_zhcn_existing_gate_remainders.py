#!/usr/bin/env python3
"""Exact, read-only legacy CardCorpus/PikaQian Chinese source matching.

Uses existing VaultSync cardcorpus_variant_reference contract. Source URLs
are private references with unverified copyright. Never downloads artwork.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.request import urlopen, Request

SOURCE_COMMIT="6fabdd3411c14d2d4c4ab84f4f24af79e1814ddc"
# (slug, provider set code, SHA256 of the pinned raw JSON, exact missing count,
#  SHA256 of authenticated read-only live missing-printing snapshot, accepted)
SETS={
"cn-csm1cc":("storming-emergence-abundant","CSM1cC","df8380a35507ccfb173f5858a92001d125cadbc96c7d230b3d223c06ed671a5e",60,"41795ce9dbb8f782dbc676a54d9531c07f428ba694c9aa8a8edea5c946c72df5",52),
"cn-csm2dc":("shining-synergy-gx-starter-deck","CSM2DC","3d706a01c1a1c7f9b2055240bbff4b9c0c05d43fdaa161698dc691d61110d233",29,"59f11d9c0781c5bee6a19b442e010d4753edbc6adbbe47d00b69bac18c66a8a7",20),
"cn-csm1dc":("storming-emergence-gx-starter-deck","CSM1DC","5738e60d05bb432ab6b11979fd4cdca686382f3bb13d59805507fd6246aa1ef0",25,"81d7305436548e88f77f443de2a4205ee11416e61ef4e5ccd70101bbbe31734d",24),
"cn-csm1ac":("storming-emergence-radiant","CSM1aC","277d4b9292b3479a0c191ba5d655e295d915b43e8d1b03c41fd0c01fa4383107",4,"d7685dd39245874414bf5c2fa9d716520fe467ef7e0bb363ae8a1112278f1682",3),
"cn-csm1bc":("storming-emergence-verdant","CSM1bC","28e6a5b7abfbb4f49a71ced3fcc743076874553ae8e836de5eff31c967e22cb3",3,"368fd9809d739eb62cf862b58134b1366523ecb5640b3e404bc31bc5002e5d66",3),
}
ALLOWED_VARIANTS=frozenset(("holo","non-holo","reverse-holo","master-ball","poke-ball"))
BATCH_PREFIX="zhcnlegacy20261123"
SHA_RE=re.compile(r"^[a-f0-9]{64}$")
PIC_RE=re.compile(r"^[a-f0-9]{32}$")

def digest(raw):return hashlib.sha256(raw).hexdigest()

def norm(v):
    raw=unicodedata.normalize("NFKC",str(v or ""))
    raw=re.sub(r"[\u2010-\u2015\u2212-]","-",raw)
    raw=re.sub(r"[^\w\s]+"," ",raw,flags=re.UNICODE)
    return " ".join(raw.casefold().split())

def number(s):
    s=str(s or "").split("/",1)[0].strip()
    return s.lstrip("0") or "0" if s.isdecimal() else None

def choose(live,src):
    """Returns one verified source picture/variant or a quarantine reason."""
    if not src:return None,"source_number_absent"
    if number(live.get("printed_collector_number")) != number(src.get("number",{}).get("short")):
        return None,"source_collector_drift"
    if not norm(live.get("native_card_name")) or norm(live["native_card_name"])!=norm(src.get("name")):
        return None,"name_drift"
    variants=src.get("variants")
    if not isinstance(variants,list) or len(variants)!=1:
        return None,"ambiguous_source_variants"
    v=variants[0]
    variant=v.get("id")
    pic=v.get("picture",{}).get("front",{}).get("image")
    if variant not in ALLOWED_VARIANTS or not isinstance(pic,str) or not PIC_RE.fullmatch(pic):
        return None,"unverified_source_picture_or_finish"
    # Never assign a normal source card to an explicit reverse/stamped live variant.
    low=str(live["native_card_name"]).lower()
    if ("reverse holo" in low and variant!="reverse-holo") or ("stamped" in low):
        return None,"declared_variant_mismatch"
    return (variant,pic),None

def fetch_set(slug,expected):
    url=("https://raw.githubusercontent.com/cardcorpus/pokemon-card-dataset/"
        +SOURCE_COMMIT+"/v1/sets/zh-hans/"+slug+".json")
    with urlopen(Request(url,headers={"User-Agent":"CardScanR-private-source-proof/1.0"}),timeout=40) as res:
        raw=res.read(3_000_001)
    if len(raw)>3_000_000 or digest(raw)!=expected:
        raise ValueError("source_response_sha256_drift")
    doc=json.loads(raw)
    if doc.get("language")!="zh-Hans" or not isinstance(doc.get("cards"),list):
        raise ValueError("chinese_source_shape_drift")
    return doc

def run(live_dir,out):
    if out.exists() or out.is_symlink():raise FileExistsError("output_already_exists")
    out.mkdir(parents=True)
    audit=[]
    for code,(slug,prefix,sha,expected,live_sha,expected_matches) in SETS.items():
        if not SHA_RE.fullmatch(sha+"" if len(sha)==64 else ""):
            raise ValueError("invalid_static_source_sha")
        source=fetch_set(slug,sha)
        if source.get("code","").upper()!=prefix.upper():
            raise ValueError("cardcorpus_set_code_drift")
        live=json.loads((live_dir/("pikaqian_"+code+"_20261123.json")).read_text("utf-8"))
        if (live.get("classification")!="AUTHENTICATED_UNLINKED_PIKAQIAN_SET_READ_ONLY"
                or live.get("set_id")!="pokemon-tcg-kb-pikaqian:set:"+code
                or live.get("snapshot_sha256")!=live_sha
                or live.get("missing_count")!=expected
                or live.get("rights_granted") is not False
                or len(live.get("rows",[]))!=expected):
            raise ValueError("current_live_snapshot_contract_drift")
        src_by_number={}
        for s in source["cards"]:
            number_key=number(s.get("number",{}).get("short"))
            if number_key is None or number_key in src_by_number:
                raise ValueError("ambiguous_source_card_numbers")
            src_by_number[number_key]=s
        cards,images=[],[]
        quarantined=Counter()
        seen=set()
        for item in live["rows"]:
            cid=item["canonical_printing_id"]
            if (cid in seen or item.get("has_image_link") is not False or item.get("language")!="zh-cn"
                or item.get("set_id")!="pokemon-tcg-kb-pikaqian:set:"+code
                or not cid.startswith("pokemon-tcg-kb-pikaqian:printing:"+code+"-")
                or not cid.endswith(":zh-cn:CN")):
                raise ValueError("duplicate_or_wrong_live_identity")
            seen.add(cid)
            num=number(item.get("printed_collector_number"))
            chosen,reason=choose(item,src_by_number.get(num))
            if reason:
                quarantined[reason]+=1
                continue
            variant,picture=chosen
            exact_number=str(item["printed_collector_number"])
            if not re.fullmatch(r"[0-9]{3}",exact_number):
                raise ValueError("live_source_number_not_3_digit")
            cards.append({
                "canonical_printing_id":cid,"language":"zh-cn","provider_set_id":code,
                "status":"exact_unique","provider_card_id":prefix+"-"+exact_number+"-"+variant,
                "native_card_name":item["native_card_name"],
                "printed_collector_number":exact_number,
                "source_picture_id":picture,"source_variant_id":variant,
                "source_variant_verified":True,
                "source_commit":SOURCE_COMMIT,"source_set_id":"zh-hans/"+slug,
            })
            images.append({
                "canonical_printing_id":cid,"language":"zh-cn","provider_set_id":code,
                "image_url":"https://img.cardcorpus.com/s1/480/"+picture+".avif",
            })
        if len(cards)!=expected_matches or len(quarantined)+len(cards)!=expected:
            raise ValueError(f"exact_match_count_drift:{code}:got={len(cards)},expected={expected_matches},quarantine={dict(quarantined)}")
        payload={"schema_version":1,"source":"cardcorpus_variant_reference",
            "source_commit":SOURCE_COMMIT,"image_binaries_downloaded":0,
            "artwork_rights_verified":False,
            "sets":[{"language":"zh-cn","provider_set_id":code,
                     "source_set_id":"zh-hans/"+slug,
                     "source_response_sha256":sha,"source_commit":SOURCE_COMMIT}],
            "cards":cards,"images":images}
        raw=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()
        packed=gzip.compress(raw,mtime=0)
        batch=BATCH_PREFIX+code.replace("-","")
        (out/(batch+".gz")).write_bytes(packed)
        receipt={"batch_id":batch,"set":code,"source_sha256":sha,
                 "current_live_missing_snapshot_sha256":live_sha,
                 "source_exact_single_variant":len(cards),"quarantine_reasons":dict(quarantined),
                 "gzip_sha256":digest(packed),"gzip_size":len(packed),
                 "new_live_links":0,"artwork_files_downloaded":0,
                 "public_rights_granted":False}
        (out/(batch+".json")).write_text(json.dumps(receipt,sort_keys=True,indent=2)+"\n")
        audit.append(receipt)
        print("LEGACY_CHS_STAGE_CONTENT",json.dumps({
            "action":"artwork_vault_stage","draft_id":batch,
            "expected_sha256":receipt["gzip_sha256"],"chunk_index":0,"limit":1,
            "content":base64.b64encode(packed).decode()},separators=(",",":")))
    result={"classification":"EXISTING_VAULT_SOURCE_GATED_PRIVATE_URL_ONLY",
        "batches":audit,"source_match_candidates":sum(x["source_exact_single_variant"] for x in audit),
        "quarantined":sum(sum(x["quarantine_reasons"].values()) for x in audit),
        "new_live_links":0,"rights_granted":False}
    (out/"receipt.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    return result

def self_test():
    a={"native_card_name":"Virizion GX","printed_collector_number":"017"}
    b={"name":"Virizion-GX","number":{"short":"017"},"variants":[{
        "id":"non-holo","picture":{"front":{"image":"a"*32}}}]}
    assert choose(a,b)==(("non-holo","a"*32),None)
    assert choose(a,dict(b,variants=b["variants"]*2))[1]=="ambiguous_source_variants"
    assert choose(dict(a,native_card_name="Virizion GX Reverse Holo"),b)[1]=="name_drift"
    assert choose(dict(a,printed_collector_number="018"),b)[1]=="source_collector_drift"
    assert choose(a,dict(b,variants=[{"id":"unknown","picture":{"front":{"image":"a"*32}}}]))[1]=="unverified_source_picture_or_finish"
    print("PASS old-source one-variant exact live matching and quarantine tests")

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--self-test",action="store_true")
    p.add_argument("--live-dir",type=Path)
    p.add_argument("--out",type=Path)
    args=p.parse_args()
    if args.self_test:self_test()
    else:
        if not args.live_dir or not args.out:p.error("live_dir and out required")
        print("LEGACY_CHS_EXACT_SOURCE_PROOF",json.dumps(run(args.live_dir,args.out),sort_keys=True))
