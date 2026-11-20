#!/usr/bin/env python3
"""Pack exact TCGdex card metadata URLs into private SHA-pinned VaultSync batches.

Consumes a verified *separate* 29-set read-only source/archive reconciliation.
No picture HTTP GET, image bodies, canonical card insert, or public display.
"""
from __future__ import annotations
import argparse,gzip,hashlib,json,re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

ALLOW={"zh-tw","th","id","ko"}
SET_ID=re.compile(r"^tcgdex:asia:set:([A-Za-z0-9_.-]{1,48})$")
SRC_ID=re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
HASH=re.compile(r"^[0-9a-f]{64}$")
SAFE_IMG=re.compile(r"^/[A-Za-z0-9-]+/[A-Za-z0-9-]+/[A-Za-z0-9-]+/[A-Za-z0-9-]+/low[.]webp$")
MAX_CARDS=500

def validate_candidate(row:dict) -> dict:
    if not isinstance(row,dict) or row.get("status")!="exact_unique" or row.get("candidate_for_missing_pack_image_ref") is not True:
        raise ValueError("not_exact_missing_image_candidate")
    lang=row.get("language")
    code=row.get("provider_set_id")
    cid=row.get("canonical_printing_id")
    sid=row.get("provider_card_id")
    url=row.get("provider_low_webp_url")
    sha=row.get("source_set_sha256")
    if lang not in ALLOW or not isinstance(code,str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}",code):
        raise ValueError("incorrect_language_or_source_set")
    if not isinstance(cid,str) or not SRC_ID.fullmatch(cid) or not isinstance(sid,str) or not SRC_ID.fullmatch(sid) or not sid.startswith(code+"-"):
        raise ValueError("unverified_canonical_or_provider_card_id")
    if not isinstance(sha,str) or not HASH.fullmatch(sha):
        raise ValueError("source_response_missing_sha")
    if not isinstance(url,str) or len(url)>500:
        raise ValueError("invalid_provider_image_url")
    p=urlsplit(url)
    if p.scheme!="https" or p.hostname!="assets.tcgdex.net" or p.username or p.password or p.port not in (None,443) or p.query or p.fragment or not SAFE_IMG.fullmatch(p.path):
        raise ValueError("unapproved_tcgdex_image_url")
    if row.get("image_binaries_downloaded") or row.get("artwork_rights_verified"):
        raise ValueError("unexpected_artwork_rights_or_download")
    if row.get("pack_had_image_ref"):
        raise ValueError("archived_pack_not_missing_source")
    if row.get("source_image_base_url")+"/low.webp"!=url:
        raise ValueError("source_image_url_base_mismatch")
    return {"lang":lang,"code":code,"cid":cid,"sid":sid,"url":url,"sha":sha}

def prepare(source:Path,out:Path):
    if out.exists() or out.is_symlink():
        raise FileExistsError("stage_destination_must_be_new")
    r=json.loads((source/"receipt.json").read_text("utf8"))
    if (r.get("classification")!="PRIVATE_29_SET_IMAGE_REFERENCE_CROSSWALK"
       or r.get("source_target_count")!=29
       or r.get("source_sets_succeeded")!=29
       or r.get("source_sets_failed")!=0
       or r.get("image_binaries_downloaded")!=0 or r.get("production_writes")!=0
       or r.get("customer_ownership_read") or r.get("canonical_id_changes")
       or r.get("artwork_rights_cleared") or r.get("safe_to_publish")):
        raise ValueError("source_crosswalk_not_exact_or_incomplete")
    data=(source/"valid_image_candidates.jsonl").read_bytes()
    expected=r.get("valid_image_candidates.jsonl")
    if (not isinstance(expected,dict) or len(data)!=expected.get("bytes")
        or hashlib.sha256(data).hexdigest()!=expected.get("sha256")):
        raise ValueError("crosswalk_sha256_manifest_mismatch")
    rows=[validate_candidate(json.loads(x)) for x in data.splitlines()]
    if (not 100<=len(rows)<=5000 or len(rows)!=r.get("valid_missing_image_ref_candidates")):
        raise ValueError("unexpected_candidate_count")
    ids=[x["cid"] for x in rows]
    if len(ids)!=len(set(ids)):
        raise ValueError("duplicated_canonical_identity")
    source_shas=defaultdict(set)
    for x in rows:
        source_shas[(x["lang"],x["code"])].add(x["sha"])
    if any(len(v)!=1 for v in source_shas.values()):
        raise ValueError("conflicting_source_set_sha")
    rows.sort(key=lambda x:(x["lang"],x["code"],x["cid"]))
    out.mkdir(parents=True)
    packs=[]
    for index in range(0,len(rows),MAX_CARDS):
        part=rows[index:index+MAX_CARDS]
        bid=f"tcg29bulk20261121_{index//MAX_CARDS:02d}"
        source_sets={(x["lang"],x["code"]) for x in part}
        doc={
          "schema_version":1,"source":"tcgdex",
          "sets":[{"language":lang,"provider_set_id":sid,
                   "source_response_sha256":next(iter(source_shas[(lang,sid)])),
                   "source_repo":"TCGdex public JSON API",
                   "provenance_note":"SHA256 of exact source set JSON; rights not granted"}
                   for lang,sid in sorted(source_sets)],
          "cards":[{"canonical_printing_id":x["cid"],"language":x["lang"],
                    "provider_set_id":x["code"],"status":"exact_unique",
                    "provider_card_id":x["sid"]}
                   for x in part],
          "images":[{"canonical_printing_id":x["cid"],"language":x["lang"],
                     "provider_set_id":x["code"],"image_url":x["url"]}
                    for x in part],
          "artwork_rights_verified":False,
          "image_binaries_downloaded":0,
        }
        blob=gzip.compress(json.dumps(doc,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf8"),mtime=0)
        if not 0<len(blob)<=72000:
            raise ValueError("batch_exceeds_one_chunk")
        filename=bid+".gz"
        (out/filename).write_bytes(blob)
        packs.append({"batch_id":bid,"filename":filename,
                      "sha256":hashlib.sha256(blob).hexdigest(),
                      "bytes":len(blob),"cards":len(part),"sources":len(part),
                      "sets":len(doc["sets"]),"live_applied":False})
    receipt={
      "classification":"PRIVATE_TCGDEX_29_SET_EXACT_VAULT_BATCH_STAGE_ONLY",
      "source_crosswalk_sha256":expected["sha256"],
      "archive_verified_pack_shas":r.get("sha_verified_language_packs"),
      "total_candidate_urls":len(rows),"batches":packs,
      "new_canonical_cards":0,"image_downloads":0,
      "public_artwork_permission":False,"live_vault_writes":0,
    }
    (out/"receipt.json").write_text(json.dumps(receipt,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return receipt

def selftest():
    base={"language":"th","provider_set_id":"SV7s","canonical_printing_id":"test:id",
          "provider_card_id":"SV7s-1","status":"exact_unique",
          "candidate_for_missing_pack_image_ref":True,
          "source_image_base_url":"https://assets.tcgdex.net/th/sv/sv7s/1",
          "provider_low_webp_url":"https://assets.tcgdex.net/th/sv/sv7s/1/low.webp",
          "source_set_sha256":"a"*64,"pack_had_image_ref":False,
          "artwork_rights_verified":False,"image_binaries_downloaded":0}
    assert validate_candidate(base)["lang"]=="th"
    for field,val in (("provider_low_webp_url","https://evil.example/card/1/low.webp"),
                      ("pack_had_image_ref",True),
                      ("artwork_rights_verified",True),
                      ("candidate_for_missing_pack_image_ref",False)):
        test=dict(base,**{field:val})
        try:validate_candidate(test)
        except ValueError:pass
        else:raise AssertionError("unsafe candidate accepted: "+field)
    print("PASS: exact metadata, safe image URLs, no public artwork")

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--source",type=Path)
    p.add_argument("--output",type=Path)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:selftest()
    else:
        if not a.source or not a.output:p.error("--source and --output required")
        r=prepare(a.source,a.output)
        print("TCGDEX_29_STAGE_READY",json.dumps({
          "image_candidates":r["total_candidate_urls"],"batches":len(r["batches"]),
          "digests":[b["sha256"] for b in r["batches"]],"live_writes":0},sort_keys=True))
