#!/usr/bin/env python3
"""Build 406 exact, missing Simplified Chinese private image URL candidates.

Uses only already-enabled CardCorpus variant source contract and immutable
authenticated live missing-identity snapshots. No image bytes, public rights,
canonical-card creation or production writes. Explicit finish identity only.
"""
from __future__ import annotations
from collections import Counter
import argparse, base64, gzip, hashlib, json, re, unicodedata
from pathlib import Path
from urllib.request import Request, urlopen

COMMIT="6fabdd3411c14d2d4c4ab84f4f24af79e1814ddc"
# source set, provider code, pinned raw JSON SHA256, live missing count,
# live read-only full snapshot SHA, expected verified rows, batch id
SPECS={
 "cn-promo-sv-p":("sv-chinese-promos","SV-P",
  "15b619ebfe06c4484901c48960824c4a495403389c77f9a8b150f0f39a0a85e2",
  227,"8ebfa1314c3c494e102bbf0c81d36855f88d35c47b0e7b408207fd5a2be2af7d",225,"zhcnw40623svpromo"),
 "cn-cs4dac":("start-deck-100","CS4DaC",
  "f5c2e3c7e2bc2dc84039a364c2a248d17833e7a6102129c81fc05101ac03c433",
  266,"2603f918430dffe752b04ea56d57eafd6d9b4125687233f0fbcf18ee34a3ddd5",131,"zhcnw40623cs4dac"),
 "cn-csve2c":("battle-party-shining-dream","CSVE2C",
  "ca2b767787b1885264db6063a0c801ebf57ef98cc53360857d82898d199df9db",
  63,"0c56d8b622cf4f0742f800f31222da106df09f232cf8feecf19438fed00de436",31,"zhcnw40623csve2c"),
 "cn-csve1c":("battle-party-dream-together","CSVE1C",
  "46d04f89f8f2f10335531ee4334db8405ef291c4b632fbff0940b3cda097bbc8",
  40,"194d5df3330dc9611d567cf20c91f8e8f5267559f75a416acf4f6fc1e86332ed",19,"zhcnw40623csve1c"),
}
EXACT_PIC=re.compile(r"[a-f0-9]{32}\Z")
NUMBER=re.compile(r"[0-9]{3}\Z")
SUFFIXES=((" Master Ball Holo","master-ball"),(" Pokeball Holo","poke-ball"))
VARIANTS=frozenset(("non-holo","poke-ball","master-ball"))

def sha(raw):return hashlib.sha256(raw).hexdigest()

def norm(s):
    s=unicodedata.normalize("NFKC",str(s or ""))
    s=re.sub(r"[\u2010-\u2015\u2212-]","-",s)
    s=re.sub(r"[^\w\s]+"," ",s,flags=re.UNICODE)
    return " ".join(s.casefold().split())

def printed_num(s):
    s=str(s or "").strip().split("/",1)[0]
    return (s.lstrip("0") or "0") if s.isdecimal() else ""

def choose(live,source,set_code):
    native=live["native_card_name"]
    raw=native
    desired="non-holo"
    if set_code=="cn-promo-sv-p":
        for suffix,variant in SUFFIXES:
            if raw.lower().endswith(suffix.lower()):
                raw=raw[:-len(suffix)]
                desired=variant
                break
    if raw.lower().endswith(" reverse holo") or raw.lower().endswith(" holo"):
        return None,"ambiguous_explicit_finish"
    if not source or norm(raw)!=norm(source.get("name")):
        return None,"source_name_or_number_drift"
    options=source.get("variants")
    if not isinstance(options,list):
        return None,"source_variants_missing"
    candidates=[v for v in options if v.get("id")==desired]
    if len(candidates)!=1 or desired not in VARIANTS:
        return None,"source_finish_unavailable"
    pic=candidates[0].get("picture",{}).get("front",{}).get("image")
    if not isinstance(pic,str) or not EXACT_PIC.fullmatch(pic):
        return None,"source_picture_unverified"
    return (desired,pic),None

def source_file(slug,digest):
    url=("https://raw.githubusercontent.com/cardcorpus/pokemon-card-dataset/"
         +COMMIT+"/v1/sets/zh-hans/"+slug+".json")
    with urlopen(Request(url,headers={"User-Agent":"CardScanR-source-proof/1.0"}),timeout=50) as r:
        raw=r.read(4_000_001)
    if len(raw)>4_000_000 or sha(raw)!=digest:
        raise ValueError("immutable_cardcorpus_response_digest_drift")
    j=json.loads(raw)
    if j.get("language")!="zh-Hans" or not isinstance(j.get("cards"),list):
        raise ValueError("source_set_schema_drift")
    return j

def run(live_dir:Path,out:Path):
    if out.exists() or out.is_symlink():raise FileExistsError("new_output_directory_required")
    out.mkdir(parents=True)
    all_receipts=[]
    for code,(slug,prefix,digest,missing,live_sha,expected,batch) in SPECS.items():
        live=json.loads((live_dir/("pikaqian_"+code+"_406_20261123.json")).read_text("utf8"))
        if (live.get("classification")!="LIVE_MISSING_CHS_SET_IDENTITY_READ_ONLY"
            or live.get("source_set")!="pokemon-tcg-kb-pikaqian:set:"+code
            or live.get("snapshot_sha256")!=live_sha
            or live.get("missing_cards_in_snapshot")!=missing
            or live.get("rights_granted") is not False
            or len(live.get("rows",[]))!=missing):
            raise ValueError("live_full_missing_snapshot_hash_or_count_drift")
        source=source_file(slug,digest)
        if source.get("code","").upper()!=prefix.upper():
            raise ValueError("source_provider_set_code_drift")
        by_number={}
        for card in source["cards"]:
            n=printed_num(card.get("number",{}).get("short"))
            if not n:continue # some source promo IDs are nonnumeric / stamped
            if n in by_number:raise ValueError("duplicated_source_printed_number")
            by_number[n]=card
        cards=[];images=[];quarantine=Counter();seen=set()
        for row in live["rows"]:
            cid=row.get("canonical_printing_id")
            num=row.get("printed_collector_number")
            if (not isinstance(cid,str) or cid in seen
                    or row.get("language")!="zh-cn"
                    or row.get("set_id")!="pokemon-tcg-kb-pikaqian:set:"+code
                    or row.get("has_image_link") is not False
                    or not cid.startswith("pokemon-tcg-kb-pikaqian:printing:"+code+"-")
                    or not cid.endswith(":zh-cn:CN")
                    or not isinstance(num,str) or not NUMBER.fullmatch(num)):
                raise ValueError("live_canonical_identity_or_number_invalid")
            seen.add(cid)
            src=by_number.get(printed_num(num))
            matched,reason=choose(row,src,code)
            if reason:
                quarantine[reason]+=1
                continue
            variant,picture=matched
            cards.append({
                "canonical_printing_id":cid,"language":"zh-cn",
                "provider_set_id":code,"status":"exact_unique",
                "provider_card_id":prefix+"-"+num+"-"+variant,
                "native_card_name":row["native_card_name"],
                "printed_collector_number":num,
                "source_picture_id":picture,"source_variant_id":variant,
                "source_variant_verified":True,"source_commit":COMMIT,
                "source_set_id":"zh-hans/"+slug,
            })
            images.append({
                "canonical_printing_id":cid,"language":"zh-cn",
                "provider_set_id":code,
                "image_url":"https://img.cardcorpus.com/s1/480/"+picture+".avif",
            })
        if len(cards)!=expected or sum(quarantine.values())+len(cards)!=missing:
            raise ValueError("exact_verified_card_count_drift:"+code+":got="+str(len(cards))+
                             ":expected="+str(expected)+":rejected="+repr(dict(quarantine)))
        payload={"schema_version":1,"source":"cardcorpus_variant_reference",
                 "source_commit":COMMIT,
                 "image_binaries_downloaded":0,"artwork_rights_verified":False,
                 "sets":[{"language":"zh-cn","provider_set_id":code,
                          "source_set_id":"zh-hans/"+slug,
                          "source_response_sha256":digest,"source_commit":COMMIT}],
                 "cards":cards,"images":images}
        z=gzip.compress(json.dumps(payload,ensure_ascii=False,sort_keys=True,
                                   separators=(",",":")).encode("utf-8"),mtime=0)
        receipt={"batch_id":batch,"source_set":code,
                 "source_verified_cards":len(cards),"quarantined":dict(quarantine),
                 "full_live_missing":missing,"live_snapshot_sha256":live_sha,
                 "source_response_sha256":digest,
                 "gzip_sha256":sha(z),"gzip_size":len(z),
                 "artwork_files_downloaded":0,"public_artwork_rights_granted":False,
                 "new_live_links":0}
        (out/(batch+".gz")).write_bytes(z)
        (out/(batch+".json")).write_text(json.dumps(receipt,sort_keys=True,indent=2)+"\n")
        all_receipts.append(receipt)
        print("CHS_406_STAGE_CONTENT",json.dumps({
            "action":"artwork_vault_stage","draft_id":batch,
            "expected_sha256":sha(z),"chunk_index":0,
            "limit":1,"content":base64.b64encode(z).decode("ascii"),
        },separators=(",",":")))
    summary={"classification":"PRIVATE_EXISTING_WORKBENCH_CHS_406_EXACT_SOURCE_PROOF",
        "total_verified_source_candidates":sum(r["source_verified_cards"] for r in all_receipts),
        "total_quarantined":sum(sum(r["quarantined"].values()) for r in all_receipts),
        "batches":all_receipts,"new_live_links":0,"rights_granted":False}
    (out/"receipt.json").write_text(json.dumps(summary,indent=2,sort_keys=True)+"\n")
    return summary

def self_test():
    b={"name":"Pinsir","variants":[
         {"id":"non-holo","picture":{"front":{"image":"a"*32}}},
         {"id":"poke-ball","picture":{"front":{"image":"b"*32}}},
         {"id":"master-ball","picture":{"front":{"image":"c"*32}}}]}
    assert choose({"native_card_name":"Pinsir"},b,"cn-promo-sv-p")==(("non-holo","a"*32),None)
    assert choose({"native_card_name":"Pinsir Pokeball Holo"},b,"cn-promo-sv-p")==(("poke-ball","b"*32),None)
    assert choose({"native_card_name":"Pinsir Master Ball Holo"},b,"cn-promo-sv-p")==(("master-ball","c"*32),None)
    assert choose({"native_card_name":"Pinsir Reverse Holo"},b,"cn-promo-sv-p")[0] is None
    assert choose({"native_card_name":"Yanma Reverse Holo"},b,"cn-cs4dac")[0] is None
    assert choose({"native_card_name":"Pinsor"},b,"cn-promo-sv-p")[0] is None
    print("PASS exact printed finish crosswalk + fail-closed reverse/stamp/name")

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--self-test",action="store_true")
    p.add_argument("--live-dir",type=Path)
    p.add_argument("--out",type=Path)
    a=p.parse_args()
    if a.self_test:self_test()
    else:
        if not a.live_dir or not a.out:p.error("live-dir and out required")
        r=run(a.live_dir,a.out)
        print("CHS_406_SOURCE_ONLY_PASS",json.dumps({
            "candidate_count":r["total_verified_source_candidates"],
            "quarantined":r["total_quarantined"],"new_live_links":0}))
