#!/usr/bin/env python3
"""Source-pinned, live-identity-checked CHS single-variant private URL candidates.

No image downloads, public art permission, customer data or production writes.
No batch can be applied without separate Workbench source support, a fresh
read-only preflight, backup and PASS receipt.
"""
from __future__ import annotations
import argparse, base64, gzip, hashlib, json, re
from pathlib import Path
from urllib.request import Request, urlopen

SOURCE_COMMIT = "6fabdd3411c14d2d4c4ab84f4f24af79e1814ddc"
NATIVE_SOURCE_COMMIT = "7f03f38f4538f01d8ee03121aa91aad3b0ae1149"
CROSSWALK_SHA = "525ed14cf3318d971a19557e4a1c70b1d2309131832f16a64e6534c90cd19c03"
SCOPE = {
    "9": ("shining-synergy-gx-starter-deck", "CSM2DC",
          "3d706a01c1a1c7f9b2055240bbff4b9c0c05d43fdaa161698dc691d61110d233", 357, 366,
          "a3790505969905d2e9735eb89eb73379e5cc83afd0da72693f8c1521b284ec89"),
    "4": ("storming-emergence-gx-starter-deck", "CSM1DC",
          "5738e60d05bb432ab6b11979fd4cdca686382f3bb13d59805507fd6246aa1ef0", 336, 345,
          "088b3b5a29e14c3a9a154ae57a7a34e73256ecd00acf40b9915392c6ada2868e"),
    "29": ("dynamax-clash-v-starter-deck", "CS1DC",
           "6f4471a87d3508d0a01b06dd96669846ba703459b04620fd84b796c3a233e3d6", 222, 230,
           "ea037d499466267770a9d54d27748f6c8efddbd7669bccd42ff45934d0939248"),
}
BATCH_PREFIX = "ptcgchs1var20261123"
SAFE_PICTURE = re.compile(r"^[0-9a-f]{32}$")

def sha(b: bytes) -> str: return hashlib.sha256(b).hexdigest()

def number(s):
    x=str(s).split("/",1)[0].strip()
    return x.lstrip("0") or "0" if x.isdigit() else None

def validate_pair(live, candidate, source_card):
    if (live["canonical_printing_id"] != candidate["archived_canonical_printing_id"]
        or live["has_image_link"] is not False
        or live["native_card_name"] != candidate["archived_native_chinese_name"]
        or number(live["printed_collector_number"]) != number(candidate["printed_collector_number"])
        or candidate["independent_native_identity_corroborated"] is not True
        or candidate["classification"] != "NATIVE_NAME_AND_NUMBER_CORROBORATED_VARIANT_UNVERIFIED"):
        raise ValueError("live_native_or_number_crosswalk_mismatch")
    variants = source_card.get("variants")
    if not isinstance(variants,list) or len(variants)!=1:
        raise ValueError("multiple_or_missing_physical_variants")
    variant = variants[0]
    picture=variant.get("picture",{}).get("front",{}).get("image")
    if (not SAFE_PICTURE.fullmatch(str(picture))
        or picture != candidate["source_picture_id"]
        or not variant.get("id")
        or number(source_card["number"]["printed"]) != number(candidate["printed_collector_number"])):
        raise ValueError("source_variant_picture_or_number_mismatch")
    return variant["id"], picture

def source_set(slug,digest):
    url=("https://raw.githubusercontent.com/cardcorpus/pokemon-card-dataset/"
         +SOURCE_COMMIT+"/v1/sets/zh-hans/"+slug+".json")
    with urlopen(Request(url,headers={"User-Agent":"CardScanR-Source-Proof/1.0"}),timeout=45) as f:
        raw=f.read(2_000_001)
    if len(raw)>2_000_000 or sha(raw)!=digest: raise ValueError("immutable_source_sha_mismatch")
    return json.loads(raw)

def run(crosswalk,live_dir,out):
    if out.exists() or out.is_symlink(): raise FileExistsError("fresh_output_required")
    raw=Path(crosswalk).read_bytes()
    if sha(raw)!=CROSSWALK_SHA: raise ValueError("crosswalk_source_sha_drift")
    cross=[json.loads(x) for x in raw.splitlines()]
    if len(cross)!=5289: raise ValueError("crosswalk_count_drift")
    out.mkdir(parents=True)
    totals=[]
    for scope,(slug,set_code,digest,candidate_count,missing_count,live_sha) in SCOPE.items():
        path=Path(live_dir)/("ptcg_chs_set_"+scope+"_20261123.json")
        live_doc=json.loads(path.read_text("utf-8"))
        if (live_doc["set_scope"]!="ptcg-chs-datasets:set:"+scope
            or live_doc["snapshot_sha256"]!=live_sha
            or live_doc["missing_cards_in_snapshot"]!=missing_count
            or len(live_doc["rows"])!=missing_count
            or live_doc["import_rights_granted"] is not False):
            raise ValueError("live_snapshot_scope_count_or_sha_drift")
        live={r["canonical_printing_id"]:r for r in live_doc["rows"]}
        if len(live)!=missing_count:raise ValueError("duplicate_live_canonical")
        src=source_set(slug,digest)
        if src["code"].upper()!=set_code or src["language"]!="zh-Hans":raise ValueError("source_set_code_drift")
        bynum={str(int(c["number"]["numerator"])):c for c in src["cards"]}
        if len(bynum)!=len(src["cards"]):raise ValueError("ambiguous_source_numbers")
        eligible=[r for r in cross if r["archived_canonical_printing_id"].startswith(
            "ptcg-chs-datasets:printing:"+scope+"_")]
        if len(eligible)!=candidate_count:raise ValueError("candidate_scope_count_drift")
        cards=[];images=[];unique=set()
        for c in eligible:
            cid=c["archived_canonical_printing_id"]
            if cid not in live or cid in unique:raise ValueError("not_in_current_missing_live_snapshot")
            if c["normalized_source_set_code"]!=set_code or c["source_set_id"]!="zh-hans/"+slug:
                raise ValueError("crosswalk_source_set_drift")
            num=number(c["printed_collector_number"])
            if num is None or num not in bynum:raise ValueError("source_card_absent")
            variant,picture=validate_pair(live[cid],c,bynum[num])
            # Only an exact single declared physical source variant can pass.
            card_num=live[cid]["printed_collector_number"]
            card={
                "canonical_printing_id":cid,"language":"zh-cn",
                "provider_set_id":"ptcg-"+scope,"status":"exact_unique",
                "provider_card_id":set_code+"-"+str(card_num).zfill(3)+"-"+variant,
                "native_card_name":live[cid]["native_card_name"],
                "printed_collector_number":card_num,
                "source_picture_id":picture,"source_variant_id":variant,
                "source_variant_verified":True,"source_commit":SOURCE_COMMIT,
                "source_set_id":"zh-hans/"+slug,
            }
            cards.append(card)
            images.append({"canonical_printing_id":cid,"language":"zh-cn",
                "provider_set_id":"ptcg-"+scope,
                "image_url":"https://img.cardcorpus.com/s1/480/"+picture+".avif"})
            unique.add(cid)
        doc={"schema_version":1,"source":"cardcorpus_ptcg_single_variant_reference",
             "source_commit":SOURCE_COMMIT,
             "sets":[{"language":"zh-cn","provider_set_id":"ptcg-"+scope,
                      "source_set_id":"zh-hans/"+slug,
                      "source_response_sha256":digest,"source_commit":SOURCE_COMMIT}],
             "cards":cards,"images":images,
             "image_binaries_downloaded":0,"artwork_rights_verified":False}
        z=gzip.compress(json.dumps(doc,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode(),mtime=0)
        name=BATCH_PREFIX+"set"+scope
        (out/(name+".gz")).write_bytes(z)
        receipt={"classification":"PRIVATE_STAGED_METADATA_NOT_LIVE","batch_id":name,
                 "language":"zh-cn","source_set":slug,"pinned_source_sha256":digest,
                 "current_live_snapshot_sha256":live_sha,"source_printings":len(cards),
                 "gzip_sha256":sha(z),"gzip_size":len(z),"live_links_added":0,
                 "public_artwork_rights_granted":False,"artwork_files_downloaded":0,
                 "requires_source_gate_activation":True,
                 "requires_fresh_preflight_and_sqlite_backup":True}
        (out/(name+".json")).write_text(json.dumps(receipt,indent=2,sort_keys=True)+"\n")
        totals.append(receipt)
        print("PTCG_CHS_STAGE_CONTENT",json.dumps({"action":"artwork_vault_stage",
             "draft_id":name,"expected_sha256":sha(z),"chunk_index":0,
             "limit":1,"content":base64.b64encode(z).decode()},separators=(",",":")))
    (out/"receipt.json").write_text(json.dumps({"classification":"PRIVATE_PTCG_CHS_SINGLE_VARIANT_SOURCE_ONLY",
        "records":totals,"total_candidates":sum(x["source_printings"] for x in totals),
        "new_live_links":0,"public_artwork_rights_granted":False},indent=2)+"\n")
    return totals

def self_test():
    a={"canonical_printing_id":"cid","has_image_link":False,"native_card_name":"皮卡丘","printed_collector_number":"001"}
    b={"archived_canonical_printing_id":"cid","archived_native_chinese_name":"皮卡丘",
       "printed_collector_number":"001/342","independent_native_identity_corroborated":True,
       "classification":"NATIVE_NAME_AND_NUMBER_CORROBORATED_VARIANT_UNVERIFIED",
       "source_picture_id":"a"*32}
    c={"number":{"printed":"001/342"},"variants":[{"id":"non-holo","picture":{"front":{"image":"a"*32}}}]}
    assert validate_pair(a,b,c)==("non-holo","a"*32)
    try:validate_pair(a,b,dict(c,variants=c["variants"]+c["variants"]))
    except ValueError:pass
    else:raise AssertionError("ambiguous variant accepted")
    print("PASS exact live identity, unique physical variant and picture tests")

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--crosswalk",type=Path)
    ap.add_argument("--live-dir",type=Path)
    ap.add_argument("--out",type=Path)
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test:self_test()
    else:
        if not a.crosswalk or not a.live_dir or not a.out:ap.error("source input required")
        print("PTCG_CHS_915_SOURCE_PROOF",json.dumps({"batches":len(run(a.crosswalk,a.live_dir,a.out)),
              "live_imports":0}))
