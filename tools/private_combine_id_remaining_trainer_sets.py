#!/usr/bin/env python3
"""Combine LIVE-PREFLIGHTED native Thai artwork source reference batches.

Offline-only aggregation of unchanged SHA-proven stage bytes, preserving every
set's original source_response_sha256 and card printing identity. Single
transaction/SQLite backup rather than repeated 1.5 GB backups. No artwork
binary downloads, licensing, live DB write or public publishing here.
"""
from __future__ import annotations
import argparse,base64,gzip,hashlib,json,re
from pathlib import Path
from urllib.parse import urlsplit

BATCH_ID="idremaincombined20261122_01"
MAX_SETS=3
MAX_CARDS=250
BID=re.compile(r"^idremain20261122[a-z0-9-]{2,32}$")
SHA=re.compile(r"^[0-9a-f]{64}$")
IMAGE=re.compile(r"^/id/card-img/id[0-9]{8}\.png$")


def safe_url(u):
    if not isinstance(u,str):return False
    try:
        p=urlsplit(u)
        return (p.scheme=="https" and p.hostname=="asia.pokemon-card.com"
                and p.port in (None,443) and not p.username and not p.password
                and not p.query and not p.fragment and IMAGE.fullmatch(p.path) is not None)
    except ValueError:return False


def build(manifest:Path,out:Path,print_stage=False):
    if out.exists() or out.is_symlink():raise FileExistsError("combined_output_must_be_new")
    if not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_size>150_000:
        raise ValueError("input_manifest_absent_or_unbounded")
    top=json.loads(manifest.read_text(encoding="utf8"))
    if (top.get("classification")!="PRE_FLIGHTED_ID_PRIVATE_SOURCE_STAGE_INPUTS"
        or top.get("production_writes")!=0
        or top.get("artwork_rights_verified") is not False
        or top.get("public_artwork_enabled") is not False
        or not isinstance(top.get("batches"),list)
        or not 2<=len(top["batches"])<=MAX_SETS):
        raise ValueError("manifest_scope_or_rights_invalid")
    seen_batches=set();sets=[];cards=[];images=[];counts=[]
    seen_cid=set();seen_sets=set()
    for entry in top["batches"]:
        bid=entry.get("draft_id");digest=entry.get("expected_sha256")
        if (not isinstance(bid,str) or not BID.fullmatch(bid)
            or bid in seen_batches or not isinstance(digest,str)
            or not SHA.fullmatch(digest)
            or entry.get("live_preflight_source_candidates",0)<=0
            or entry.get("live_preflight_newly_covered_cards",0)<=0
            or entry.get("live_preflight_rights_granted") is not False
            or not isinstance(entry.get("content"),str)):
            raise ValueError("missing_positive_live_preflight_or_duplicate_source")
        seen_batches.add(bid)
        zipped=base64.b64decode(entry["content"],validate=True)
        if hashlib.sha256(zipped).hexdigest()!=digest:
            raise ValueError("stage_compressed_sha_mismatch")
        if not 0<len(zipped)<=72_000:
            raise ValueError("source_stage_chunk_size_invalid")
        doc=json.loads(gzip.decompress(zipped))
        if (doc.get("schema_version")!=1 or doc.get("source")!="official_crosswalk"
            or doc.get("artwork_rights_verified") is not False
            or doc.get("image_binaries_downloaded")!=0
            or len(doc.get("sets",[]))!=1
            or len(doc.get("cards",[]))!=len(doc.get("images",[]))
            or len(doc["cards"])!=entry["live_preflight_source_candidates"]):
            raise ValueError("source_stage_card_rows_or_rights_invalid")
        source_set=doc["sets"][0]
        code=source_set.get("provider_set_id")
        if (not isinstance(code,str) or len(code)>48
            or source_set.get("language")!="id"
            or not SHA.fullmatch(source_set.get("source_response_sha256",""))
            or code in seen_sets):
            raise ValueError("source_set_identity_or_duplicate")
        seen_sets.add(code)
        local={}
        for c in doc["cards"]:
            cid=c.get("canonical_printing_id")
            if (not isinstance(cid,str) or not cid.startswith("tcgdex_asia_"+code+"_")
                or cid in seen_cid or c.get("language")!="id"
                or c.get("provider_set_id")!=code or c.get("status")!="exact_unique"
                or not isinstance(c.get("native_card_name"),str)
                or not c["native_card_name"].strip()
                or not isinstance(c.get("printed_collector_number"),str)
                or not c["printed_collector_number"].strip()):
                raise ValueError("printing_duplicate_or_identity_invalid")
            seen_cid.add(cid);local[cid]=c
        if len(local)!=len(doc["cards"]):
            raise ValueError("source_card_duplicate")
        image_ids=set()
        for im in doc["images"]:
            cid=im.get("canonical_printing_id")
            if (cid in image_ids or cid not in local or
                im.get("provider_set_id")!=code or im.get("language")!="id"
                or not safe_url(im.get("image_url"))):
                raise ValueError("image_not_exact_source_card_or_url_unsafe")
            image_ids.add(cid)
        if image_ids!=set(local):
            raise ValueError("image_card_bijection_failed")
        sets.append(source_set)
        cards.extend(doc["cards"])
        images.extend(doc["images"])
        counts.append({"source_batch_id":bid,"source_sha256":digest,
                       "source_cards":len(doc["cards"]),
                       "live_preflight_newly_covered_cards":entry["live_preflight_newly_covered_cards"]})
    if not 2<=len(sets)<=MAX_SETS or not 1<=len(cards)<=MAX_CARDS or len(cards)!=len(images):
        raise ValueError("combined_batch_count_exceeds_safe_scope")
    payload={"schema_version":1,"source":"official_crosswalk","sets":sets,
             "cards":cards,"images":images,
             "artwork_rights_verified":False,"image_binaries_downloaded":0}
    blob=gzip.compress(json.dumps(payload,sort_keys=True,ensure_ascii=False,
                       separators=(",",":")).encode("utf8"),mtime=0,compresslevel=9)
    if not len(blob)<=72_000:
        raise ValueError("combined_stage_exceeds_single_chunk_limit")
    sha=hashlib.sha256(blob).hexdigest()
    out.mkdir(parents=True)
    (out/(BATCH_ID+".gz")).write_bytes(blob)
    result={"classification":"COMBINED_SHA_VERIFIED_ID_PRIVATE_BATCH_NOT_LIVE",
            "batch_id":BATCH_ID,"gzip_sha256":sha,"gzip_bytes":len(blob),
            "source_batch_receipts":counts,
            "set_count":len(sets),"source_url_records":len(images),
            "preflight_newly_covered_at_time_of_source_checks":sum(
                x["live_preflight_newly_covered_cards"] for x in counts),
            "current_live_preflight_mandatory":True,
            "new_live_image_links":0,"new_live_canonical_cards":0,
            "artwork_binaries_downloaded":0,"rights_verified":False,
            "public_artwork_enabled":False}
    (out/"receipt.json").write_text(json.dumps(result,sort_keys=True,indent=2)+"\n")
    if print_stage:
        print("COMBINED_ID_PRIVATE_STAGE_CONTENT",json.dumps({
            "action":"artwork_vault_stage","draft_id":BATCH_ID,
            "expected_sha256":sha,"chunk_index":0,"limit":1,
            "content":base64.b64encode(blob).decode("ascii")
        },sort_keys=True))
    return result


def selftest():
    assert safe_url("https://asia.pokemon-card.com/id/card-img/id00001234.png")
    assert not safe_url("https://asia.pokemon-card.com/th/card-img/th00001234.png")
    assert not safe_url("http://asia.pokemon-card.com/id/card-img/id00001234.png")
    assert not safe_url("https://asia.pokemon-card.com/id/card-img/id00001234.png?other=1")
    assert BID.fullmatch("idremain20261122svam")
    assert not BID.fullmatch("../../outside")
    assert len(BATCH_ID)<60
    print("PASS bounded strict Thai safe merge, SHA staged evidence, rights and live preflight")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--source-manifest",type=Path)
    p.add_argument("--out",type=Path)
    p.add_argument("--self-test",action="store_true")
    p.add_argument("--print-stage-b64",action="store_true")
    a=p.parse_args()
    if a.self_test:selftest()
    else:
        if a.source_manifest is None or a.out is None:
            p.error("--source-manifest and --out required")
        result=build(a.source_manifest,a.out,a.print_stage_b64)
        print("ID_COMBINED_PRIVATE_SHA_STAGE_READY",json.dumps({
            "sets":result["set_count"],"url_refs":result["source_url_records"],
            "source_live_preflight_covered":result["preflight_newly_covered_at_time_of_source_checks"],
            "new_live_links":result["new_live_image_links"],
            "gzip_sha256":result["gzip_sha256"]
        },sort_keys=True))
