#!/usr/bin/env python3
"""Read-only exact TH/ID official pages 2-5 vs SHA-verified CardScanR packs.

Aggregates only independent source HTML card detail metadata artifacts. Never
downloads artwork, creates cards, modifies vault, or grants public image rights.
"""
from __future__ import annotations
import argparse,hashlib,json,sqlite3,tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import quote
from tools.preflight_multilingual_crossrelease import CANARY,MAX_GZIP,get,inspect_pack
from tools.preflight_legacy_korean_pack import pick
from tools.private_official_asia_th_id_exact_crosswalk import check,FIELDS,TARGETS

PAGES=(2,3,4,5)
PREFIX="CardScanR-Official-TH-ID-Page-"

def gather(root):
    sources=[]
    archive_proofs={}
    seen=set()
    for page in PAGES:
        folder=root/(PREFIX+str(page))
        receipt=json.loads((folder/"receipt.json").read_text("utf8"))
        if (receipt.get("classification")!="PRIVATE_OFFICIAL_ASIA_CARD_URL_RESEARCH_ONLY"
            or receipt.get("source_list_page")!=page or
            receipt.get("source_cards")!=40 or
            receipt.get("image_url_references")!=40 or
            receipt.get("source_request_budget")!=42 or
            receipt.get("image_binaries_downloaded")!=0 or
            receipt.get("live_vault_writes")!=0 or
            receipt.get("artwork_rights_granted") or
            receipt.get("public_artwork_enabled")):
            raise ValueError("source_page_receipt_unverified")
        data=(folder/"official_asia_card_source_urls.jsonl").read_bytes()
        if hashlib.sha256(data).hexdigest()!=receipt["source_url_manifest_sha256"]:
            raise ValueError("source_page_jsonl_sha_drift")
        rows=[json.loads(line) for line in data.splitlines()]
        if len(rows)!=40:raise ValueError("source_page_card_count_drift")
        for row in rows:
            if row.get("source_list_page")!=page or row.get("language") not in TARGETS:
                raise ValueError("source_page_identity_drift")
            key=(row["language"],row["official_detail_id"])
            if key in seen:raise ValueError("duplicate_official_source_card_across_pages")
            seen.add(key)
            if row.get("exact_vault_match_proven") or row.get("artwork_rights_cleared") or row.get("image_downloaded"):
                raise ValueError("source_page_rights_claim")
        sources.extend(rows)
        archive_proofs[str(page)]=receipt["source_url_manifest_sha256"]
    if len(sources)!=160 or len(seen)!=160:
        raise ValueError("expected_160_unique_source_detail_rows")
    return sources,archive_proofs

def audit(source_root:Path,output:Path):
    if output.exists() or output.is_symlink():
        raise FileExistsError("output_must_not_exist")
    source,sha_proofs=gather(source_root)
    manifest=json.loads(get(CANARY,2000000))
    packs=[]
    archive_digest={}
    with tempfile.TemporaryDirectory(prefix="cardscanr-thid-pages-") as temp:
        for lang in TARGETS:
            desc=pick(manifest,lang)
            archive=get(desc["compressedDatabaseUrl"],MAX_GZIP)
            file=Path(temp)/(lang+".sqlite")
            check_record,_,_,_,_,_=inspect_pack(desc,archive,file)
            if check_record["sqlite_quick_check"]!="ok":
                raise ValueError("sha_verified_pack_sqlite_corrupt")
            archive_digest[lang]=desc["sha256"]
            conn=sqlite3.connect("file:"+quote(file.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1",uri=True)
            try:
                conn.execute("PRAGMA query_only=ON")
                columns={x[1] for x in conn.execute("PRAGMA table_info(cards)")}
                if not set(FIELDS).issubset(columns):raise ValueError("archive_columns_mismatch")
                for row in conn.execute("SELECT "+",".join(FIELDS)+" FROM cards"):
                    rec=dict(zip(FIELDS,row))
                    if rec["language"]!=lang:raise ValueError("archive_language_mismatch")
                    packs.append(rec)
            finally:conn.close()
    matches,reasons=check(packs,source)
    output.mkdir(parents=True)
    data=("\n".join(json.dumps(x,sort_keys=True,ensure_ascii=False) for x in matches)+"\n").encode()
    (output/"exact_source_url_candidates.jsonl").write_bytes(data)
    receipt={
      "classification":"PRIVATE_TH_ID_OFFICIAL_PAGED_EXACT_ARCHIVE_CROSSWALK",
      "official_source_pages":list(PAGES),
      "source_page_sha256":sha_proofs,
      "source_records":len(source),
      "archived_pack_card_rows":len(packs),
      "archived_pack_sha256":archive_digest,
      "matched_records":len(matches),
      "candidate_records":sum(x["classification"]=="exact_missing_image_source_url_candidate" for x in matches),
      "reasons":dict(reasons),
      "crosswalk_sha256":hashlib.sha256(data).hexdigest(),
      "new_verified_live_canonical_records":0,
      "live_vault_writes":0,"image_binaries_downloaded":0,
      "artwork_rights_verified":False,"safe_for_public_artwork":False,
    }
    (output/"receipt.json").write_text(json.dumps(receipt,sort_keys=True,ensure_ascii=False,indent=2)+"\n")
    return receipt

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--source-root",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    a=p.parse_args()
    r=audit(a.source_root,a.output)
    print("OFFICIAL_ASIA_160_SOURCE_CROSSWALK",json.dumps({
      "sources":r["source_records"],"exact_matches":r["matched_records"],
      "missing_archive_image_candidates":r["candidate_records"],
      "reasons":r["reasons"],"live_writes":0,"artwork_downloads":0,
    },ensure_ascii=False,sort_keys=True))
