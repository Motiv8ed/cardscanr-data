#!/usr/bin/env python3
"""Convert SHA-verified private exact official source URLs to VaultSync stage packs.

No card creation, image byte download, public image permission or live DB access.
This converts only already EXACT matched and unique archived printing identities;
a separate Workbench importer must compare native name, set, number and language
against the actual PC vault before applying.
"""
from __future__ import annotations
import argparse,base64,gzip,hashlib,json,re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

BATCH_SIZE=400
EXPECTED=7112
LANGS={"zh-tw","ko"}
SOURCE_PREFIX="tcgdex:asia:set:"
URL_TW=re.compile(r"https://asia[.]pokemon-card[.]com/tw/card-img/tw[0-9]{8}[.]png")
URL_KO=re.compile(r"https://cards[.]image[.]pokemonkorea[.]co[.]kr/data/wmimages/[A-Za-z0-9_-]{1,16}/([A-Za-z0-9_-]{1,32})/([A-Za-z0-9_-]{1,32})_[0-9]{3}[.]png")

def accept(row):
    if not isinstance(row,dict) or row.get("status")!="exact_unique_pack_identity_source_url_candidate":
        raise ValueError("not_exact_verified_crosswalk")
    lang,sid,cid,url,name,num=(
        row.get("language"),row.get("set_id"),row.get("canonical_printing_id"),
        row.get("source_image_url_reference"),row.get("native_name"),
        row.get("printed_collector_number")
    )
    if lang not in LANGS or not isinstance(sid,str) or not sid.startswith(SOURCE_PREFIX):
        raise ValueError("invalid_language_or_pack_set")
    code=sid[len(SOURCE_PREFIX):]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}",code):
        raise ValueError("unsafe_set_code")
    if (not isinstance(cid,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}",cid)
            or not isinstance(name,str) or not name.strip()
            or not isinstance(num,str) or not num.strip()):
        raise ValueError("incomplete_live_card_proof")
    if not row.get("existing_pack_image_was_missing") or not row.get("live_vault_image_missing_not_yet_verified"):
        raise ValueError("source_gap_assertion_wrong")
    if row.get("artwork_rights_verified") or row.get("image_downloaded") or row.get("public_release_allowed"):
        raise ValueError("wrong_source_rights_claim")
    if lang=="zh-tw":
        if not isinstance(url,str) or not URL_TW.fullmatch(url):
            raise ValueError("taiwan_image_url_drift")
    else:
        m=URL_KO.fullmatch(url or "")
        if not m or m.group(1)!=code or m.group(2)!=code:
            raise ValueError("korean_image_url_or_set_drift")
    print_key=row.get("source_print_key")
    if not isinstance(print_key,str) or len(print_key)>120 or not print_key.startswith("asia:"+code+"-"):
        raise ValueError("source_print_key_drift")
    return {
       "cid":cid,"lang":lang,"sid":code,"url":url,
       "name":name,"number":num,"print_key":print_key,
       "source_file":row.get("source_file"),
    }

def build(source:Path,out:Path):
    if out.exists() or out.is_symlink():
        raise FileExistsError("output_must_be_new")
    receipt=json.loads((source/"receipt.json").read_text("utf8"))
    compressed=(source/"exact_source_url_candidates.jsonl.gz").read_bytes()
    raw=gzip.decompress(compressed)
    if hashlib.sha256(raw).hexdigest()!=receipt["candidate_uncompressed_sha256"]:
        raise ValueError("crosswalk_receipt_sha256_drift")
    if hashlib.sha256(compressed).hexdigest()!=receipt["candidate_compressed_sha256"]:
        raise ValueError("crosswalk_archive_gzip_sha256_drift")
    items=[accept(json.loads(line)) for line in raw.splitlines() if line]
    if len(items)!=EXPECTED or receipt["unique_matched_missing_pack_image_reference_candidates"]!=EXPECTED:
        raise ValueError("crosswalk_expected_size_drift")
    keys=[i["cid"] for i in items]
    if len(set(keys))!=len(keys):
        raise ValueError("duplicate_canonical_printing_identity")
    per_set=defaultdict(list)
    for item in items:
        per_set[(item["lang"],item["sid"])].append(item)
    fingerprints={}
    for key,arr in per_set.items():
        # This is hash of normalized exact *archive* records in the set.
        # It is not represented as a remote provider response SHA.
        content=json.dumps(arr,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()
        fingerprints[key]=hashlib.sha256(content).hexdigest()
    out.mkdir()
    receipts=[]
    for page,start in enumerate(range(0,len(items),BATCH_SIZE)):
        group=items[start:start+BATCH_SIZE]
        batchid=f"offcross_20261120_{page:02d}"
        sets_seen={}
        cards=[]
        images=[]
        for r in group:
            key=(r["lang"],r["sid"])
            sets_seen[key]={
                "language":r["lang"],
                "provider_set_id":r["sid"],
                "source_response_sha256":fingerprints[key],
                "provenance_note":"sha256 of pinned exact crosswalk set candidates; not original HTTP set response",
                "source_archive_sha256":receipt["candidate_uncompressed_sha256"],
                "source_repo":"type-null/PTCG-database",
                "source_commit":"18e0de2feb7a35ebe4296ede0ba6cc768961489b",
            }
            cards.append({
                "canonical_printing_id":r["cid"],
                "language":r["lang"],"provider_set_id":r["sid"],
                "status":"exact_unique",
                "provider_card_id":r["print_key"],
                "native_card_name":r["name"],
                "printed_collector_number":r["number"],
            })
            images.append({
                "canonical_printing_id":r["cid"],
                "language":r["lang"],"provider_set_id":r["sid"],
                "image_url":r["url"],
            })
        doc={"schema_version":1,"source":"official_crosswalk",
            "sets":[sets_seen[k] for k in sorted(sets_seen)],
            "cards":cards,"images":images,
            "artwork_rights_verified":False,"image_binaries_downloaded":0}
        payload=json.dumps(doc,ensure_ascii=False,separators=(",",":"),sort_keys=True).encode()
        compressed=gzip.compress(payload,mtime=0)
        if len(compressed)>64000:
            raise ValueError("batch_exceeds_one_worker_chunk")
        file_name=batchid+".gz"
        (out/file_name).write_bytes(compressed)
        receipts.append({
           "batch_id":batchid,"file":file_name,"sha256":hashlib.sha256(compressed).hexdigest(),
           "compressed_bytes":len(compressed),"source_set_count":len(sets_seen),
           "cards":len(cards),"images":len(images),"artwork_downloaded":0,
           "safe_to_apply_without_live_identity_checks":False,
        })
    proof={
       "classification":"PRIVATE_STAGED_SOURCE_CANDIDATES_NOT_LIVE",
       "batches":receipts,"total_source_candidates":len(items),
       "total_batches":len(receipts),
       "crosswalk_receipt_sha256":receipt["candidate_uncompressed_sha256"],
       "provenance":"Pinned source and SHA-verified public catalogue pack identity evidence",
       "source_rights_unverified":True,
       "live_card_identity_not_yet_verified":True,
       "vault_database_writes":0,"image_downloads":0,
    }
    (out/"receipt.json").write_text(json.dumps(proof,indent=2,sort_keys=True)+"\n")
    return proof

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--source",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--emit-bridge-stage-log",action="store_true")
    a=p.parse_args()
    doc=build(a.source,a.output)
    print("PACKS_READY",json.dumps({
        "batches":doc["total_batches"],
        "source_candidates":doc["total_source_candidates"],
        "live_writes":0,"rights_verified":False,
    },sort_keys=True))
    if a.emit_bridge_stage_log:
        # These URLs, source-printing identities and set data are already in
        # public GitHub repositories and public pack manifests. Never emit
        # API tokens, cookies, customer records, original artwork or secrets.
        for entry in doc["batches"]:
            encoded=base64.b64encode((a.output/entry["file"]).read_bytes()).decode("ascii")
            print("CARDSCANR_BRIDGE_STAGE "+json.dumps({
                "draft_id":entry["batch_id"],
                "expected_sha256":entry["sha256"],
                "chunk_index":0,"limit":1,"content":encoded
            },sort_keys=True))
if __name__=="__main__":
    main()
