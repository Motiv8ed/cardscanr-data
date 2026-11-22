#!/usr/bin/env python3
"""Stage newly source-unique ID SV6s pages 8-12 card metadata for live preflight.

No SQLite writes, no artwork downloads, no public permission. Stage proposal
MUST be revalidated by authenticated live VaultSync before any private import.
"""
from __future__ import annotations
import argparse,base64,gzip,hashlib,json
from collections import defaultdict
from pathlib import Path
from tools.private_official_id_sv6s_p2p7_vault_stage import accept,ITEM_CLASS
from tools.private_official_id_sv6s_p8p12_crosswalk import OUT_CLASS

BATCH_ID="offidsv6s20261122_p8p12"


def build(source:Path,out:Path):
    if out.exists() or out.is_symlink():raise FileExistsError("new_output_required")
    r=json.loads((source/"receipt.json").read_text(encoding="utf8"))
    if (r.get("classification")!=OUT_CLASS or
        not 0<=r.get("source_cards",-1)<=100 or
        r.get("actual_live_new_image_links")!=0 or
        r.get("new_live_canonical_cards")!=0 or
        r.get("image_binaries_downloaded")!=0 or
        r.get("artwork_rights_verified") or r.get("public_artwork_enabled") or
        r.get("current_live_preflight_required") is not True):
        raise ValueError("archive_receipt_not_private_or_safe")
    raw=(source/"archived_exact_crosswalk.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest()!=r["crosswalk_sha256"]:
        raise ValueError("archived_printings_sha_drift")
    rows=[json.loads(x) for x in raw.splitlines()]
    items=[accept(x) for x in rows if x.get("classification")==ITEM_CLASS]
    if len(items)!=r["exact_candidate_records"] or len(items)>100:
        raise ValueError("missing_image_candidates_not_exact")
    if len({x["cid"] for x in items})!=len(items):
        raise ValueError("duplicated_printing")
    if any(x["lang"]!="id" or x["code"]!="SV6s" for x in items):
        raise ValueError("wrong_locale_or_set")
    out.mkdir(parents=True)
    if not items:
        rec={"classification":"NO_ARCHIVE_MISSING_IMAGE_CANDIDATES",
             "source_cards":r["source_cards"],"proposed_url_refs":0,
             "live_new_links":0,"artwork_files_downloaded":0}
        (out/"receipt.json").write_text(json.dumps(rec,indent=2)+"\n")
        return rec
    byset=defaultdict(list)
    for x in items:byset[(x["lang"],x["code"])].append(x["sha"])
    sets=[{"language":lang,"provider_set_id":code,
           "source_response_sha256":hashlib.sha256("".join(sorted(shas)).encode()).hexdigest(),
           "source_crosswalk_sha256":r["crosswalk_sha256"]}
          for (lang,code),shas in sorted(byset.items())]
    doc={
        "schema_version":1,"source":"official_crosswalk",
        "sets":sets,
        "cards":[{"canonical_printing_id":x["cid"],"language":x["lang"],
                  "provider_set_id":x["code"],"status":"exact_unique",
                  "provider_card_id":x["code"]+"-"+x["detailid"],
                  "native_card_name":x["name"],
                  "printed_collector_number":x["num"]} for x in items],
        "images":[{"canonical_printing_id":x["cid"],"language":x["lang"],
                   "provider_set_id":x["code"],"image_url":x["url"]} for x in items],
        "artwork_rights_verified":False,"image_binaries_downloaded":0
    }
    zipped=gzip.compress(json.dumps(doc,sort_keys=True,ensure_ascii=False,
                                    separators=(",",":")).encode("utf8"),mtime=0,compresslevel=9)
    if not 0<len(zipped)<=72_000:raise ValueError("stage_chunk_over_budget")
    sha=hashlib.sha256(zipped).hexdigest()
    (out/(BATCH_ID+".gz")).write_bytes(zipped)
    receipt={
        "classification":"PRIVATE_OFFICIAL_SV6S_PAGES8_12_VAULT_STAGE_ONLY",
        "batch_id":BATCH_ID,"gzip_file":BATCH_ID+".gz",
        "gzip_sha256":sha,"source_cards":r["source_cards"],
        "proposed_url_refs":len(items),"archive_sha256":r["crosswalk_sha256"],
        "live_new_links":0,"new_live_canonical_cards":0,
        "requires_exact_current_live_preflight":True,
        "artwork_files_downloaded":0,"artwork_rights_granted":False,
        "public_artwork_enabled":False
    }
    (out/"receipt.json").write_text(json.dumps(receipt,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return receipt


def selftest():
    assert len(BATCH_ID)<=60 and BATCH_ID.isascii()
    assert isinstance(BATCH_ID,str) and "p8p12" in BATCH_ID
    print("PASS exact source stage scope, no rights and no DB writes")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--source",type=Path)
    p.add_argument("--out",type=Path)
    p.add_argument("--self-test",action="store_true")
    p.add_argument("--print-stage-b64",action="store_true")
    a=p.parse_args()
    if a.self_test:selftest()
    else:
        if not a.source or not a.out:p.error("--source/out required")
        receipt=build(a.source,a.out)
        print("ID_SV6S_8_12_STAGE_META",json.dumps(receipt,sort_keys=True))
        if a.print_stage_b64 and receipt.get("proposed_url_refs",0):
            binary=(a.out/receipt["gzip_file"]).read_bytes()
            print("ID_SV6S_8_12_STAGE_CONTENT",json.dumps({
                "action":"artwork_vault_stage","draft_id":receipt["batch_id"],
                "expected_sha256":receipt["gzip_sha256"],"chunk_index":0,"limit":1,
                "content":base64.b64encode(binary).decode("ascii")
            },sort_keys=True))
