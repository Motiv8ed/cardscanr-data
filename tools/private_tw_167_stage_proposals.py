#!/usr/bin/env python3
"""Build private SHA-pinned VaultSync staging PROPOSALS for 167 TW source URLs.

Source-only metadata; not an import, not a public artwork licence. Every actual
live identity still requires Workbench read-only preflight before queueing.
"""
from __future__ import annotations
import argparse,base64,gzip,hashlib,json
from collections import Counter
from pathlib import Path

MAX_PER_BATCH=45
EXPECTED={"SV2a":51,"SV3":116}
PREFIX={"SV2a":"twsv2a20261122","SV3":"twsv320261122"}


def plan(source_dir:Path,out_dir:Path,print_b64:bool=False):
    if out_dir.exists() or out_dir.is_symlink():
        raise FileExistsError("new_output_required")
    receipt=json.loads((source_dir/"receipt.json").read_text(encoding="utf8"))
    if (receipt["classification"]!="PRIVATE_LIVE_TW_SV2A_SV3_EXACT_SOURCE_ONLY_NOT_APPLIED"
            or receipt["single_observed_source_image_url_candidates"]!=167
            or receipt["target_live_missing_card_count"]!=305
            or receipt["current_live_link_inserts"]!=0
            or receipt["source_commit"]!="18e0de2feb7a35ebe4296ede0ba6cc768961489b"
            or not receipt["source_research_only"] or receipt["artwork_rights_verified"]):
        raise ValueError("untrusted_source_crosswalk_receipt")
    source=source_dir/receipt["files"]["source_matches"]["filename"]
    blob=source.read_bytes()
    if hashlib.sha256(blob).hexdigest()!=receipt["files"]["source_matches"]["sha256"]:
        raise ValueError("source_crosswalk_sha_changed")
    rows=[json.loads(s) for s in blob.splitlines() if s]
    if len(rows)!=167:
        raise ValueError("verified_source_row_count_changed")
    found=set()
    grouped={x:[] for x in EXPECTED}
    for r in rows:
        cid=r["canonical_printing_id"]
        code=r["set_code"]
        if (cid in found or code not in EXPECTED
                or r["classification"]!="SINGLE_PROVIDER_SOURCE_IMAGE_EXACT_NAME_AND_NUMBER_RESEARCH_ONLY"
                or r["safe_for_automatic_import"] is not False
                or r["artwork_rights_verified"] is not False
                or r["live_printing_finish_available"] is not False
                or r["live_printed_denominator_available"] is not False
                or r["set_id"]!="tcgdex:asia:set:"+code
                or cid!=f"tcgdex_asia_{code}_{r['printed_number']}:zh-tw:TW"
                or r["source_set_name_alias"]!=code+" F"):
            raise ValueError("source_not_exact_or_new_source_alias")
        found.add(cid);grouped[code].append(r)
    if {k:len(v) for k,v in grouped.items()}!=EXPECTED:
        raise ValueError("source_count_per_set_changed")
    out_dir.mkdir(parents=True)
    batches=[]
    for code,values in grouped.items():
        values.sort(key=lambda x:x["canonical_printing_id"])
        for number,start in enumerate(range(0,len(values),MAX_PER_BATCH)):
            chunk=values[start:start+MAX_PER_BATCH]
            provider_set_sha=hashlib.sha256(("18e0de2feb7a35ebe4296ede0ba6cc768961489b:"+
                ":".join(sorted(x["source_file_sha256"] for x in chunk))).encode("ascii")).hexdigest()
            doc={
                "schema_version":1,"source":"official_crosswalk",
                "artwork_rights_verified":False,"image_binaries_downloaded":0,
                "sets":[{
                    "language":"zh-tw","provider_set_id":code,
                    "source_response_sha256":provider_set_sha,
                    "source_repo":"type-null/PTCG-database",
                    "source_commit":"18e0de2feb7a35ebe4296ede0ba6cc768961489b",
                    "source_name_alias":code+" F"
                }],
                "cards":[{
                    "canonical_printing_id":r["canonical_printing_id"],
                    "language":"zh-tw","provider_set_id":code,
                    "status":"exact_unique",
                    "provider_card_id":code+"-"+r["printed_number"],
                    "native_card_name":r["native_name"],
                    "printed_collector_number":r["printed_number"],
                    "source_file_sha256":r["source_file_sha256"]
                } for r in chunk],
                "images":[{
                    "canonical_printing_id":r["canonical_printing_id"],
                    "language":"zh-tw","provider_set_id":code,
                    "image_url":r["source_image_url"]
                } for r in chunk]
            }
            raw=json.dumps(doc,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode("utf8")
            compressed=gzip.compress(raw,compresslevel=9,mtime=0)
            if len(compressed)>72_000:
                raise ValueError("vault_stage_single_chunk_too_large")
            bid=PREFIX[code]+("_%02d"%number)
            path=out_dir/(bid+".gz")
            path.write_bytes(compressed)
            entry={"batch_id":bid,"source_set_code":code,
                   "card_count":len(chunk),
                   "images":len(doc["images"]),
                   "gzip_sha256":hashlib.sha256(compressed).hexdigest(),
                   "gzip_bytes":len(compressed),
                   "stage_chunks":1,
                   "current_live_identity_recheck_required":True,
                   "actual_live_imports":0,
                   "source_rights_verified":False,
                   "safe_for_public_artwork":False}
            batches.append(entry)
            if print_b64:
                print("CARDSCANR_TW_167_PRIVATE_STAGE_PROPOSAL",
                      json.dumps({"action":"artwork_vault_stage",
                                  "draft_id":bid,
                                  "expected_sha256":entry["gzip_sha256"],
                                  "chunk_index":0,
                                  "limit":1,
                                  "content":base64.b64encode(compressed).decode("ascii")
                                 },sort_keys=True))
    if sum(x["card_count"] for x in batches)!=167 or len(batches)!=5:
        raise ValueError("unexpected_staging_batch_total")
    report={"classification":"UNAPPLIED_TW_167_VERIFIED_SOURCE_URL_BATCH_PROPOSALS",
            "research_source":"PR103 authentic TW 305 exact source-only crosswalk",
            "proposed_image_url_references":167,
            "batches":batches,
            "actual_new_live_links":0,"actual_new_canonical_cards":0,
            "artwork_binaries_downloaded":0,
            "artwork_rights_granted":False,
            "commercial_publication_allowed":False,
            "requires_current_live_workbench_preflight":True,
            "warning":"No public artwork rights or verified physical variant. Do NOT autoqueue without private reference policy check and positive exact LIVE source preflight."}
    (out_dir/"manifest.json").write_text(json.dumps(report,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return report


def self_test():
    assert sum((n+MAX_PER_BATCH-1)//MAX_PER_BATCH for n in EXPECTED.values())==5
    assert len(PREFIX)==2
    assert len(set(PREFIX.values()))==2
    assert all(8<=len(value+"_00")<=60 for value in PREFIX.values())
    print("PASS bounded TW private staging, five retained SHA proposals, no rights or binaries")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--research",type=Path)
    p.add_argument("--out",type=Path)
    p.add_argument("--print-b64",action="store_true")
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:self_test()
    else:
        if a.research is None or a.out is None:
            p.error("--research and --out required")
        r=plan(a.research,a.out,a.print_b64)
        print("TW_167_PRIVATE_STAGING_PROPOSAL_PASS",json.dumps({
            "proposed_source_urls":r["proposed_image_url_references"],
            "batches":len(r["batches"]),
            "links_inserted":r["actual_new_live_links"],
            "rights_granted":r["artwork_rights_granted"],
            "per_batch":[[x["batch_id"],x["card_count"],x["gzip_sha256"]] for x in r["batches"]]
        },sort_keys=True))
