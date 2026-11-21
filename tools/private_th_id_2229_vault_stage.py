#!/usr/bin/env python3
"""Build ONE private, checksum-pinned, 2229-image TCGdex source stage from exact R2 matches.

Staging is NOT a live import. The authenticated PC VaultSync worker must check
all 2,229 exact current canonical IDs, language, set and missing link status
using artwork_vault_preflight before it may queue an atomic backed-up insert.
No image binaries, public art rights or customer data.
"""
from __future__ import annotations
import argparse,base64,gzip,hashlib,json,re
from collections import defaultdict,Counter
from pathlib import Path
from tools.private_th_id_tcgdex_set_image_source import safe_base

CLASS="PRIVATE_TH_ID_LARGE_TCGDEX_PINNED_ARCHIVE_EXACT_CROSSWALK_ONLY"
BATCH="thid2229live20261122_00"
SHA=re.compile(r"^[0-9a-f]{64}$")
MAX_CHUNK=65_000


def construct(archive,out,emit=False):
    if out.exists() or out.is_symlink():
        raise FileExistsError("output_must_be_new")
    r=json.loads((archive/"receipt.json").read_text(encoding="utf8"))
    if (r["classification"]!=CLASS or r["source_declared_image_refs"]!=2229
            or r["archive_exact_image_source_refs"]!=2229
            or r["source_identity_quarantined"]!=0
            or r["new_live_links"]!=0 or r["new_live_canonicals"]!=0
            or r["commercial_rights_verified"] is not False):
        raise ValueError("expected_validated_archived_exact_source_evidence")
    b=(archive/"archived_source_image_candidates.jsonl").read_bytes()
    if hashlib.sha256(b).hexdigest()!=r["candidates_file"]["sha256"]:
        raise ValueError("archive_crosswalk_sha256_drift")
    rows=[json.loads(x) for x in b.splitlines()]
    if len(rows)!=2229 or len(set(x["canonical_printing_id_archived"] for x in rows))!=2229:
        raise ValueError("archive_duplicate_or_missing_source_identity")
    cards=[];images=[];groups=defaultdict(list)
    for x in rows:
        cid=x["canonical_printing_id_archived"]
        lang=x["language"];code=x["set_code"]
        number=x["printed_collector_number"]
        url=x["source_image_url"]
        if (lang not in ("th","id")
                or x["set_id_archived"]!="tcgdex:asia:set:"+code
                or not cid.startswith("tcgdex_asia_"+code+"_")
                or not cid.endswith(":"+lang+":"+lang.upper())
                or not isinstance(number,str)
                or not isinstance(x["native_card_name"],str)
                or not x["native_card_name"]
                or x.get("live_identity_verified") is not False
                or x.get("live_image_missing_verified") is not False
                or x.get("public_artwork_rights_verified") is not False
                or not isinstance(url,str) or not url.endswith("/low.webp")
                or not safe_base(url[:-len("/low.webp")],lang,code,
                                 x["provider_card_id"].removeprefix(code+"-"))
                or not SHA.fullmatch(x["source_response_sha256"])):
            raise ValueError("bad_archived_candidate_image_identity_or_rights")
        groups[(lang,code)].append(x["source_response_sha256"])
        cards.append({
            "canonical_printing_id":cid,
            "language":lang,"provider_set_id":code,
            "provider_card_id":x["provider_card_id"],
            "status":"exact_unique",
            "native_card_name":x["native_card_name"],
            "printed_collector_number":number,
            "archive_identity_only":True,
        })
        images.append({"canonical_printing_id":cid,"language":lang,
                       "provider_set_id":code,"image_url":url})
    sets=[]
    for (lang,code),shas in sorted(groups.items()):
        if len(set(shas))!=1:
            raise ValueError("provider_source_set_response_sha_drift")
        sets.append({"language":lang,"provider_set_id":code,
                     "source_response_sha256":shas[0],
                     "evidence_class":"ARCHIVED_EXACT_NEEDS_LIVE_PREFLIGHT"})
    doc={
        "schema_version":1,"source":"tcgdex",
        "artwork_rights_verified":False,
        "image_binaries_downloaded":0,
        "sets":sets,"cards":cards,"images":images
    }
    packed=gzip.compress(json.dumps(doc,ensure_ascii=False,sort_keys=True,
                       separators=(",",":")).encode("utf8"),mtime=0,compresslevel=9)
    if not 0<len(packed)<=20_000_000:raise ValueError("archive_payload_too_large")
    chunks=[packed[n:n+MAX_CHUNK] for n in range(0,len(packed),MAX_CHUNK)]
    if not 1<=len(chunks)<=300:raise ValueError("stage_chunk_limits_exceeded")
    out.mkdir(parents=True)
    (out/"thid_2229_stage.gz").write_bytes(packed)
    manifest={
        "classification":"PRIVATE_ARCHIVE_MATCHED_TH_ID_2229_SHA_STAGE_NOT_LIVE",
        "batch_id":BATCH,
        "gzip_sha256":hashlib.sha256(packed).hexdigest(),
        "gzip_bytes":len(packed),
        "chunk_count":len(chunks),
        "cards":len(cards),"image_refs":len(images),"sets":len(sets),
        "by_language":dict(Counter(x["language"] for x in cards)),
        "source_crosswalk_sha256":r["candidates_file"]["sha256"],
        "source_provenance_sha256":r["source_provenance_sha256"],
        "source":doc["source"],
        "requires_authenticated_current_live_preflight":True,
        "live_current_missing_images":None,
        "actual_live_image_links_added":0,
        "actual_new_canonical_cards":0,
        "downloaded_image_binaries":0,
        "public_artwork_rights_granted":False,
        "public_artwork_enabled":False,
        "safety":"Never enqueue if read-only live preflight fails. Prefer one batch/backup; stage itself is not an apply."
    }
    (out/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,
                             sort_keys=True,indent=2)+"\n",encoding="utf8")
    if emit:
        for i,chunk in enumerate(chunks):
            print("TH_ID_2229_AUTHENTICATED_STAGE_CHUNK",json.dumps({
                "action":"artwork_vault_stage",
                "draft_id":BATCH,
                "expected_sha256":manifest["gzip_sha256"],
                "chunk_index":i,
                "limit":len(chunks),
                "content":base64.b64encode(chunk).decode("ascii")
            },sort_keys=True))
    return manifest


def test():
    assert 0<MAX_CHUNK<=72_000
    assert len(BATCH)>=8
    assert SHA.fullmatch("a"*64)
    assert not SHA.fullmatch("a"*63)
    print("PASS single immutable SHA source batch, split max 65k, public rights off")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--archive",type=Path)
    p.add_argument("--out",type=Path)
    p.add_argument("--emit-chunks",action="store_true")
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:test()
    else:
        if a.archive is None or a.out is None:p.error("--archive and --out required")
        r=construct(a.archive,a.out,a.emit_chunks)
        print("TH_ID_2229_READY_FOR_CURRENT_LIVE_PREFLIGHT_ONLY",
              json.dumps({k:r[k] for k in ("batch_id","gzip_sha256","chunk_count",
                    "cards","sets","by_language","actual_live_image_links_added")},sort_keys=True))
