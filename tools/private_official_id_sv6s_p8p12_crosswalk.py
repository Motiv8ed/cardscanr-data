#!/usr/bin/env python3
"""Read-only official Indonesian SV6s pages 8-12 versus authenticated R2 pack.

Duplicate source identities and repeated source detail IDs are quarantined
BEFORE an exact native name/set/number crosswalk. No live DB writes.
"""
from __future__ import annotations
import argparse,hashlib,json,sqlite3,tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import quote
from tools.private_official_id_sv6s_p2p7_crosswalk import (
    FIELDS,TARGETS,identity,check
)
from tools.private_official_id_sv6s_p8p12_source import CLASSIFICATION
from tools.preflight_multilingual_crossrelease import CANARY,MAX_GZIP,get,inspect_pack
from tools.preflight_legacy_korean_pack import pick

OUT_CLASS="OFFICIAL_ID_SV6S_P8P12_ARCHIVED_EXACT_SOURCE_IMAGE_ONLY"


def separate_source_variants(rows):
    fingerprints=Counter()
    details=Counter()
    for row in rows:
        key=identity(row["language"],row["source_product_code"],
                     str(row["source_collector_number"] or ""),row["source_name_native"])
        fingerprints[key]+=1
        details[(row["language"],row["official_detail_id"])]+=1
    clean=[];quarantine=[]
    for row in rows:
        k=identity(row["language"],row["source_product_code"],
                   str(row["source_collector_number"] or ""),row["source_name_native"])
        if (k is None or fingerprints[k]!=1 or
                details[(row["language"],row["official_detail_id"])]!=1):
            quarantine.append({"official_detail_id":row["official_detail_id"],
                "reason":"ambiguous_source_card_or_printing_identity"})
        else:clean.append(row)
    return clean,quarantine


def run(source:Path,out:Path):
    if out.exists() or out.is_symlink():raise FileExistsError("new_output_required")
    rec=json.loads((source/"receipt.json").read_text(encoding="utf8"))
    if (rec.get("classification")!=CLASSIFICATION
        or type(rec.get("source_cards")) is not int
        or not 0<=rec["source_cards"]<=100
        or rec["source_cards"]!=rec.get("image_url_references")
        or rec["source_identity_duplicates"]!=0
        or rec["image_binaries_downloaded"]!=0 or rec["live_vault_writes"]!=0
        or rec["artwork_rights_granted"]):
        raise ValueError("source_receipt_not_safe")
    blob=(source/"official_asia_card_source_urls.jsonl").read_bytes()
    if hashlib.sha256(blob).hexdigest()!=rec["source_url_manifest_sha256"]:
        raise ValueError("source_sha_changed")
    rows=[json.loads(x) for x in blob.splitlines()]
    if len(rows)!=rec["source_cards"]:raise ValueError("source_card_count_changed")
    for r in rows:
        if (r.get("language")!="id" or r.get("source_product_code")!="SV6s"
            or r.get("source_list_page") not in tuple(range(8,13))
            or r.get("artwork_rights_cleared") or r.get("image_downloaded")):
            raise ValueError("source_set_or_rights_invalid")
    clean,quarantine=separate_source_variants(rows)
    matches=[]
    reasons=Counter()
    pack_sha=None
    pack_rows=0
    if clean:
        master=json.loads(get(CANARY,2_000_000))
        desc=pick(master,"id")
        with tempfile.TemporaryDirectory(prefix="cardscanr-id-sv6s-8-12-") as temp:
            data=get(desc["compressedDatabaseUrl"],MAX_GZIP)
            dbpath=Path(temp)/"id.sqlite"
            verified,_,_,_,_,_=inspect_pack(desc,data,dbpath)
            if verified["sqlite_quick_check"]!="ok":
                raise ValueError("archive_sqlite_bad")
            pack_sha=desc["sha256"]
            uri="file:"+quote(dbpath.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1"
            with sqlite3.connect(uri,uri=True) as db:
                db.execute("PRAGMA query_only=ON")
                cols={x[1] for x in db.execute("PRAGMA table_info(cards)")}
                if not set(FIELDS).issubset(cols):
                    raise ValueError("archive_missing_identity_columns")
                rows_pack=[dict(zip(FIELDS,rr)) for rr in db.execute(
                    "SELECT "+",".join(FIELDS)+" FROM cards")]
                if any(x["language"]!="id" for x in rows_pack):
                    raise ValueError("archive_language_drift")
            pack_rows=len(rows_pack)
            matches,reasons=check(rows_pack,clean)
    out.mkdir(parents=True)
    raw="".join(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n"
                for x in matches).encode("utf8")
    (out/"archived_exact_crosswalk.jsonl").write_bytes(raw)
    q="".join(json.dumps(x,sort_keys=True,separators=(",",":"))+"\n"
              for x in quarantine).encode("utf8")
    (out/"duplicate_source_quarantine.jsonl").write_bytes(q)
    doc={
        "classification":OUT_CLASS,"source_cards":len(rows),
        "source_sha256":rec["source_url_manifest_sha256"],
        "archived_pack_sha256":pack_sha,"archived_pack_card_rows":pack_rows,
        "unique_source_identity_rows":len(clean),
        "duplicate_or_ambiguous_source_rows":len(quarantine),
        "matched_unique_archived_printings":len(matches),
        "exact_candidate_records":sum(x["classification"]==
            "exact_missing_image_source_url_candidate" for x in matches),
        "matching_results":dict(reasons),
        "crosswalk_sha256":hashlib.sha256(raw).hexdigest(),
        "quarantine_sha256":hashlib.sha256(q).hexdigest(),
        "actual_live_new_image_links":0,"new_live_canonical_cards":0,
        "image_binaries_downloaded":0,"artwork_rights_verified":False,
        "public_artwork_enabled":False,"current_live_preflight_required":True
    }
    (out/"receipt.json").write_text(json.dumps(doc,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return doc


def test():
    row={"language":"id","source_product_code":"SV6s",
         "source_collector_number":"150","source_name_native":"Sample",
         "official_detail_id":"9001"}
    hits,q=separate_source_variants([row])
    assert len(hits)==1 and not q
    hits,q=separate_source_variants([row,row])
    assert not hits and len(q)==2
    other=dict(row,official_detail_id="9002")
    hits,q=separate_source_variants([row,other])
    assert not hits and len(q)==2
    print("PASS exact source identity collision quarantine, no archive writes")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--source",type=Path)
    p.add_argument("--out",type=Path)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:test()
    else:
        if not a.source or not a.out:p.error("--source/out required")
        d=run(a.source,a.out)
        print("ID_SV6S_P8P12_EXACT_ARCHIVE_RESULTS",json.dumps({
            k:d[k] for k in ("source_cards","unique_source_identity_rows",
              "duplicate_or_ambiguous_source_rows",
              "matched_unique_archived_printings","exact_candidate_records",
              "actual_live_new_image_links")
        },sort_keys=True))
