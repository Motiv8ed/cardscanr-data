#!/usr/bin/env python3
"""Prepare bounded multi-batch exact official TH/ID URL source-only VaultSync packs.

Only previously verified archived exact card identity candidates; actual PC
printing and rights always independently revalidated by the Workbench.
"""
from __future__ import annotations
import argparse,gzip,hashlib,json
from collections import defaultdict
from pathlib import Path
from tools.private_official_asia_th_id_vault_stage import accept

CLASSIFICATION="PRIVATE_TH_ID_OFFICIAL_PAGES_6_9_ARCHIVE_CROSSWALK"
LIMIT=40

def build(source:Path,out:Path):
    if out.exists() or out.is_symlink():raise FileExistsError("stage_output_must_be_new")
    r=json.loads((source/"receipt.json").read_text("utf8"))
    if (r.get("classification")!=CLASSIFICATION or
        r.get("source_records")!=160 or
        r.get("official_source_pages")!=[6,7,8,9] or
        r.get("live_vault_writes")!=0 or r.get("image_binaries_downloaded")!=0 or
        r.get("artwork_rights_verified") or r.get("safe_for_public_artwork")):
        raise ValueError("unverified_paged_source_receipt")
    raw=(source/"exact_source_url_candidates.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest()!=r.get("crosswalk_sha256"):
        raise ValueError("source_url_crosswalk_sha256_changed")
    rows=[accept(json.loads(line)) for line in raw.splitlines()]
    if len(rows)!=r.get("candidate_records") or len(rows)<100 or len(rows)>160:
        raise ValueError("low_or_inconsistent_exact_candidate_count")
    if len({x["cid"] for x in rows})!=len(rows):
        raise ValueError("canonical_identity_repeated_across_source_pages")
    rows.sort(key=lambda x:(x["lang"],x["code"],x["num"],x["cid"]))
    out.mkdir(parents=True)
    batches=[]
    for i in range(0,len(rows),LIMIT):
        part=rows[i:i+LIMIT]
        bid=f"offthid20261120_{i//LIMIT+5:02d}"
        fingerprint=defaultdict(list)
        for item in part:
            fingerprint[(item["lang"],item["code"])].append(item["sha"])
        source_set_sha={k:hashlib.sha256("".join(sorted(v)).encode("ascii")).hexdigest()
                        for k,v in fingerprint.items()}
        sets=[{"language":lang,"provider_set_id":code,
               "source_response_sha256":source_set_sha[(lang,code)],
               "provenance_note":"sha256 of pinned exact official per-card response hashes, not raw set response",
               "source_repo":"Official Pokemon Card Asia public card-detail HTML",
               "source_crosswalk_sha256":r["crosswalk_sha256"]}
             for lang,code in sorted(fingerprint)]
        cards=[{"canonical_printing_id":x["cid"],"language":x["lang"],"provider_set_id":x["code"],
                "status":"exact_unique",
                "provider_card_id":x["code"]+"-"+x["detailid"],
                "native_card_name":x["name"],
                "printed_collector_number":x["num"]}
               for x in part]
        images=[{"canonical_printing_id":x["cid"],"language":x["lang"],
                 "provider_set_id":x["code"],"image_url":x["url"]}
                for x in part]
        obj={"schema_version":1,"source":"official_crosswalk",
             "sets":sets,"cards":cards,"images":images,
             "artwork_rights_verified":False,"image_binaries_downloaded":0}
        data=gzip.compress(json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode(),mtime=0)
        if not 0<len(data)<=72000:raise ValueError("oversized_bridge_staging_chunk")
        name=bid+".gz"
        (out/name).write_bytes(data)
        batches.append({"batch_id":bid,"file":name,"sha256":hashlib.sha256(data).hexdigest(),
                       "bytes":len(data),"cards":len(part),"image_sources":len(part),
                       "sets":len(sets),"live_vault_writes":0,
                       "artwork_rights_verified":False})
    proof={
      "classification":"PRIVATE_OFFICIAL_TH_ID_6_9_STAGE_NOT_LIVE",
      "source_crosswalk_sha256":r["crosswalk_sha256"],
      "source_sha256_pages":r["source_page_sha256"],
      "total_candidates":len(rows),"total_batches":len(batches),
      "batches":batches,
      "new_canonical_card_records":0,"live_vault_writes":0,
      "image_binaries_downloaded":0,"artwork_rights_verified":False,
      "safe_for_public_artwork":False,"live_exact_card_identity_still_required":True,
    }
    (out/"receipt.json").write_text(json.dumps(proof,ensure_ascii=False,sort_keys=True,indent=2)+"\n")
    return proof

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--out",type=Path,required=True)
    a=parser.parse_args()
    rec=build(a.source,a.out)
    print("OFFICIAL_TH_ID_MULTIBATCH_STAGE_VERIFIED",json.dumps({
      "batches":rec["total_batches"],"source_url_candidates":rec["total_candidates"],
      "hashes":[x["sha256"] for x in rec["batches"]],
      "live_changes":0,"image_downloads":0,
    },sort_keys=True))
