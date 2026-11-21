#!/usr/bin/env python3
"""Create a private exact-ID SHA-pinned VaultSync stage payload for TH/ID.

No PC, DB, image or public website changes. The live Workbench separately
verifies every canonical id, set, language, native name and printed number.
"""
from __future__ import annotations
import argparse,base64,gzip,hashlib,json,re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit
SOURCE_CLASS="OFFICIAL_TH_ID_ARCHIVED_EXACT_SOURCE_IMAGE_CROSSWALK_ONLY"
ITEM_CLASS="exact_missing_image_source_url_candidate"
ALLOWED={"th","id"}
CODE=re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
SHA=re.compile(r"^[0-9a-f]{64}$")
IDENT=re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
ID=re.compile(r"^[0-9]{1,10}$")

def accept(item):
    if not isinstance(item,dict) or item.get("classification")!=ITEM_CLASS:
        raise ValueError("not_archive_missing_image_candidate")
    lang=item.get("language")
    sid=item.get("set_id")
    cid=item.get("canonical_printing_id_archived")
    name=item.get("native_name")
    num=item.get("printed_number")
    detailid=item.get("source_detail_id")
    url=item.get("source_image_url_reference")
    proof=item.get("source_response_sha256")
    if (lang not in ALLOWED or not isinstance(sid,str)
        or not sid.startswith("tcgdex:asia:set:")):
        raise ValueError("invalid_language_or_set")
    code=sid.removeprefix("tcgdex:asia:set:")
    if not CODE.fullmatch(code) or not isinstance(cid,str) or not IDENT.fullmatch(cid):
        raise ValueError("unsafe_canonical_id_or_set")
    if not isinstance(detailid,str) or not ID.fullmatch(detailid):
        raise ValueError("bad_official_detail_id")
    if (not isinstance(name,str) or not name.strip() or len(name)>250
        or not isinstance(num,str) or not num.strip() or len(num)>80
        or not isinstance(proof,str) or not SHA.fullmatch(proof)):
        raise ValueError("missing_native_identity_or_source_sha")
    if (item.get("actual_live_vault_id_verified") or
        item.get("artwork_rights_verified") or item.get("artwork_downloaded") or
        item.get("safe_to_publish")):
        raise ValueError("rights_or_live_identity_claim_invalid")
    if not isinstance(url,str):
        raise ValueError("missing_image_reference")
    p=urlsplit(url)
    if (p.scheme!="https" or p.hostname!="asia.pokemon-card.com" or
        p.port not in (None,443) or p.username or p.password or p.query or p.fragment
        or not re.fullmatch("/"+lang+"/card-img/"+lang+r"[0-9]{8}\.png",p.path)):
        raise ValueError("unapproved_official_asia_image_url")
    return {"cid":cid,"lang":lang,"code":code,"name":name,
            "num":num,"url":url,"detailid":detailid,"sha":proof}

def build(source:Path,out:Path):
    if out.exists() or out.is_symlink():
        raise FileExistsError("stage_output_requires_new_directory")
    rec=json.loads((source/"receipt.json").read_text("utf8"))
    if (rec.get("classification")!=SOURCE_CLASS or
        not 1<=rec.get("exact_candidate_records",0)<=120 or
        rec.get("source_card_rows")!=120 or
        rec.get("live_vault_writes")!=0 or
        rec.get("artwork_binaries_downloaded")!=0 or
        rec.get("verified_live_id_and_image_missing")!=0 or
        rec.get("artwork_rights_verified") or rec.get("safe_for_publication")):
        raise ValueError("source_receipt_provenance_mismatch")
    raw=(source/"archived_exact_crosswalk.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest()!=rec["crosswalk_sha256"]:
        raise ValueError("crosswalk_source_sha256_mismatch")
    records=[json.loads(x) for x in raw.splitlines()]
    items=[accept(x) for x in records if x.get("classification")==ITEM_CLASS]
    if len(items)!=rec["exact_candidate_records"]:
        raise ValueError("source_candidate_count_drift")
    if not 1<=len(items)<=120 or len({x["cid"] for x in items})!=len(items):
        raise ValueError("duplicate_or_missing_canonical_identity")
    byset=defaultdict(list)
    for item in items:
        byset[(item["lang"],item["code"])].append(item["sha"])
    setproofs={k:hashlib.sha256("".join(sorted(v)).encode("ascii")).hexdigest()
               for k,v in byset.items()}
    sets=[{"language":lang,"provider_set_id":code,
           "source_response_sha256":setproofs[(lang,code)],
           "provenance_note":"sha256 of pinned official per-card response SHA evidence; not raw set response",
           "source_repo":"Official Pokemon Card Asia public card-detail HTML",
           "source_crosswalk_sha256":rec["crosswalk_sha256"]}
          for lang,code in sorted(byset)]
    cards=[{"canonical_printing_id":v["cid"],"language":v["lang"],
            "provider_set_id":v["code"],"status":"exact_unique",
            "provider_card_id":v["code"]+"-"+v["detailid"],
            "native_card_name":v["name"],
            "printed_collector_number":v["num"]}
           for v in items]
    images=[{"canonical_printing_id":v["cid"],"language":v["lang"],
             "provider_set_id":v["code"],"image_url":v["url"]}
            for v in items]
    doc={"schema_version":1,"source":"official_crosswalk",
         "sets":sets,"cards":cards,"images":images,
         "artwork_rights_verified":False,"image_binaries_downloaded":0}
    binary=gzip.compress(json.dumps(doc,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf8"),mtime=0)
    if not 0<len(binary)<=72000:
        raise ValueError("stage_payload_exceeds_one_bridge_chunk")
    bid="offidsv6s20261122_27"
    filename=bid+".gz"
    out.mkdir(parents=True)
    (out/filename).write_bytes(binary)
    result={
      "classification":"PRIVATE_OFFICIAL_TH_ID_VAULT_STAGE_READY_NOT_LIVE",
      "batch_id":bid,"gzip_file":filename,
      "gzip_sha256":hashlib.sha256(binary).hexdigest(),
      "compressed_bytes":len(binary),
      "set_count":len(sets),"card_candidates":len(cards),
      "image_url_candidates":len(images),
      "source_crosswalk_sha256":rec["crosswalk_sha256"],
      "right_to_reuse_artwork":False,
      "image_binaries_downloaded":0,
      "verified_new_canonical_cards":0,
      "live_vault_writes":0,
      "requires_exact_live_identity_check":True,
      "safe_for_public_artwork":False,
      "requires_worker_official_th_id_pattern":True,
    }
    (out/"receipt.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    return result

def selftest():
    row={"classification":ITEM_CLASS,"language":"th",
      "set_id":"tcgdex:asia:set:SV4K","canonical_printing_id_archived":"sample:th:1",
      "native_name":"เรดิบา","printed_number":"001","source_detail_id":"9635",
      "source_image_url_reference":"https://asia.pokemon-card.com/th/card-img/th00009635.png",
      "source_response_sha256":"b"*64,"actual_live_vault_id_verified":False,
      "artwork_rights_verified":False,"artwork_downloaded":False,"safe_to_publish":False}
    assert accept(row)["lang"]=="th"
    bad=dict(row,source_image_url_reference="https://example.com/th/card-img/th00009635.png")
    try:accept(bad)
    except ValueError:pass
    else:raise AssertionError("untrusted source host accepted")
    bad=dict(row,language="id")
    try:accept(bad)
    except ValueError:pass
    else:raise AssertionError("cross language URL accepted")
    print("PASS official TH/ID image source proof, native printed identity, rights guard")

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",type=Path)
    parser.add_argument("--out",type=Path)
    parser.add_argument("--self-test",action="store_true")
    a=parser.parse_args()
    if a.self_test:selftest()
    else:
        if not a.source or not a.out:parser.error("--source and --out required")
        result=build(a.source,a.out)
        print("OFFICIAL_TH_ID_SOURCE_PACK_PREPARED",json.dumps({
          "stage_only":True,"batch_id":result["batch_id"],
          "cards":result["card_candidates"],"urls":result["image_url_candidates"],
          "sha256":result["gzip_sha256"],
          "live_writes":0},sort_keys=True))
