#!/usr/bin/env python3
"""Private grouped TCGdex source URLs from 4-language bulk set reconciliation.

Stages only SHA-verified set+native-name+number archived exact printings. Every
candidate must still pass a *live* VaultSync identity/duplicate preflight before
a backup or import. Never download copyrighted art or modify canonical rows.
"""
from __future__ import annotations
import argparse,gzip,hashlib,json
from pathlib import Path
from collections import defaultdict
from tools.private_tcgdex_29_source_vault_stage import validate_candidate
EXPECT={"zh-tw":1809,"th":737,"id":681}
LABEL="BULK_TCGDEX_FULL_SET_EXACT_SOURCE_URLS_NOT_LIVE"
LIMIT=500
def package(source:Path,out:Path):
    if out.exists() or out.is_symlink():raise FileExistsError("output_must_be_new")
    all_rows=[]
    verified={}
    for lang,count in EXPECT.items():
        folder=source/("CardScanR-BulkTCGdex-"+lang+"-0-Exact-Source-URLs")
        receipt=json.loads((folder/"receipt.json").read_text(encoding="utf8"))
        if (receipt.get("classification")!=LABEL or receipt.get("language")!=lang
            or receipt.get("offset")!=0 or receipt.get("request_cap")!=180
            or receipt.get("source_url_candidates")!=count or
            receipt.get("live_vault_writes")!=0 or receipt.get("image_files_downloaded")!=0
            or receipt.get("new_canonical_records")!=0 or
            receipt.get("source_url_rights_unverified") is not True or
            receipt.get("published_artwork") is not False):
            raise ValueError("not_a_verified_source_language_batch")
        original=(folder/"image_source_candidates.jsonl").read_bytes()
        digest=hashlib.sha256(original).hexdigest()
        if digest!=receipt.get("candidate_rows_sha256"):
            raise ValueError("source_row_sha256_mismatch")
        rows=[json.loads(line) for line in original.splitlines()]
        if len(rows)!=count:raise ValueError("source_card_count_drift")
        before=len(all_rows)
        for row in rows:
            if (row.get("classification")!="TCGDEX_SOURCE_ONLY_EXACT_UNIQUE_ARCHIVED_PRINTING"
                or row.get("language")!=lang or row.get("live_vault_match_verified") is not False
                or row.get("rights_granted") is not False
                or row.get("artwork_downloaded") is not False):
                raise ValueError("unverified_source_identity_or_rights")
            url=row.get("source_image_url")
            if not isinstance(url,str) or not url.endswith("/low.webp"):
                raise ValueError("source_artwork_path_drift")
            ref={
               "language":lang,"provider_set_id":row.get("provider_set_id"),
               "canonical_printing_id":row.get("canonical_printing_id"),
               "provider_card_id":row.get("provider_card_id"),
               "status":"exact_unique","candidate_for_missing_pack_image_ref":True,
               "source_image_base_url":url.removesuffix("/low.webp"),
               "provider_low_webp_url":url,
               "source_set_sha256":row.get("source_set_sha256"),
               "pack_had_image_ref":False,
               "artwork_rights_verified":False,"image_binaries_downloaded":0,
            }
            all_rows.append(validate_candidate(ref))
        if len(all_rows)-before!=count:raise ValueError("invalid_archive_candidate_count")
        verified[lang]={"source_candidate_rows":count,
                        "candidate_sha256":digest,
                        "archive_pack_sha256":receipt["verified_archive_pack_sha256"],
                        "source_sets_succeeded":receipt["source_sets_succeeded"],
                        "source_sets_failed":receipt["source_sets_failed"]}
    if len(all_rows)!=3227 or len({r["cid"] for r in all_rows})!=3227:
        raise ValueError("incomplete_or_duplicate_archived_exact_identity")
    all_rows.sort(key=lambda x:(x["lang"],x["code"],x["cid"]))
    source_shas=defaultdict(set)
    for r in all_rows:source_shas[(r["lang"],r["code"])].add(r["sha"])
    if any(len(d)!=1 for d in source_shas.values()):
        raise ValueError("conflicting_sha_for_one_source_set")
    out.mkdir(parents=True)
    packs=[]
    for start in range(0,len(all_rows),LIMIT):
        part=all_rows[start:start+LIMIT]
        bid="tcgset20261121_"+format(start//LIMIT,"02d")
        keys=sorted({(r["lang"],r["code"]) for r in part})
        doc={
           "schema_version":1,"source":"tcgdex",
           "sets":[{"language":lang,"provider_set_id":code,
                    "source_response_sha256":next(iter(source_shas[(lang,code)])),
                    "source_repo":"TCGdex set JSON API",
                    "provenance_note":"SHA256 of exact set JSON; image rights not granted"}
                    for lang,code in keys],
           "cards":[{"canonical_printing_id":r["cid"],"language":r["lang"],
                     "provider_set_id":r["code"],"status":"exact_unique",
                     "provider_card_id":r["sid"]}
                     for r in part],
           "images":[{"canonical_printing_id":r["cid"],"language":r["lang"],
                      "provider_set_id":r["code"],"image_url":r["url"]}
                     for r in part],
           "artwork_rights_verified":False,"image_binaries_downloaded":0,
        }
        blob=gzip.compress(json.dumps(doc,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode("utf8"),mtime=0)
        if len(blob)>72000:raise ValueError("reviewed_single_chunk_size_limit")
        (out/(bid+".gz")).write_bytes(blob)
        packs.append({"batch_id":bid,"file":bid+".gz",
                      "sha256":hashlib.sha256(blob).hexdigest(),
                      "source_candidates":len(part),"set_count":len(keys),
                      "compressed_bytes":len(blob),"already_imported":False,
                      "requires_live_duplicate_preflight":True})
    proof={
       "classification":"PRIVATE_BULK_3227_TCGDEX_EXACT_ARCHIVED_SOURCE_STAGE_ONLY",
       "languages":verified,
       "total_source_candidates":len(all_rows),"batches":packs,
       "new_canonical_cards":0,"image_binaries_downloaded":0,
       "live_vault_writes":0,"artwork_rights_verified":False,
       "public_artwork_enabled":False,"live_identity_verified":False,
       "ready_to_apply_without_live_preflight":False,
    }
    (out/"receipt.json").write_text(json.dumps(proof,indent=2,sort_keys=True)+"\n",encoding="utf8")
    return proof

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",required=True,type=Path)
    parser.add_argument("--output",required=True,type=Path)
    args=parser.parse_args()
    rec=package(args.source,args.output)
    print("BULK_SOURCE_3227_SHA_STAGE_REVIEW",json.dumps({
        "source_refs":rec["total_source_candidates"],
        "batches":len(rec["batches"]),
        "sha256":[b["sha256"] for b in rec["batches"]],
        "source_only":True,"live_added":0},sort_keys=True))
