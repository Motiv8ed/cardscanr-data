#!/usr/bin/env python3
"""Collect genuine official Indonesian SV6s card-detail identities on pages 2..7.

Robots-aware official source HTML only. No artwork binaries, customer data,
public publishing, database writes, or speculative image paths.
"""
from __future__ import annotations
import argparse,hashlib,json,tempfile
from pathlib import Path
from tools import private_official_asia_th_id_newset_url_pilot as pilot

PAGES=tuple(range(2,8))
SET="SV6s"
LANG="id"
COUNT=120
CLASS="PRIVATE_OFFICIAL_ID_SV6S_PAGES_2_7_SOURCE_DETAILS_ONLY"


def run(out):
    if out.exists() or out.is_symlink():
        raise FileExistsError("source_output_must_be_new")
    if pilot.TARGETS!=(("th","SV4K"),("id","SV6s")):
        raise ValueError("unexpected_pinned_original_source_allowlist")
    # Restrict the existing rights-safe collector to one official Indonesian
    # set, rather than wasting requests on already-linked Thai SV4K entries.
    pilot.TARGETS=((LANG,SET),)
    records=[]
    sources=[]
    seen=set()
    with tempfile.TemporaryDirectory(prefix="cardscanr-id-sv6s-p2p7-") as t:
        for page in PAGES:
            folder=Path(t)/("page-"+str(page))
            p=pilot.run(folder,limit=20,delay=0.75,page=page)
            if (p["classification"]!="PRIVATE_OFFICIAL_ASIA_CARD_URL_RESEARCH_ONLY"
                    or p["source_cards"]!=20 or p["image_url_references"]!=20
                    or p["source_request_budget"]!=21
                    or p["image_binaries_downloaded"]!=0 or p["live_vault_writes"]!=0):
                raise ValueError("official_page_source_missing_or_changed")
            payload=(folder/"official_asia_card_source_urls.jsonl").read_bytes()
            if hashlib.sha256(payload).hexdigest()!=p["source_url_manifest_sha256"]:
                raise ValueError("page_provenance_sha_drift")
            rows=[json.loads(line) for line in payload.splitlines()]
            if len(rows)!=20:raise ValueError("page_identity_rows_missing")
            for item in rows:
                k=item["official_detail_id"]
                url=item["source_image_url_reference"]
                if (item["language"]!=LANG or item["source_product_code"]!=SET
                    or item["source_list_page"]!=page
                    or not item["source_name_native"] or not item["source_collector_number"]
                    or k in seen
                    or url!="https://asia.pokemon-card.com/id/card-img/id"+k.zfill(8)+".png"
                    or item["artwork_rights_cleared"] or item["image_downloaded"]):
                    raise ValueError("source_locale_printing_or_card_url_identity_not_unique")
                seen.add(k)
            sources.extend(p["sources"])
            records.extend(rows)
    if len(records)!=COUNT or len(seen)!=COUNT:
        raise ValueError("expected_exact_120_distinct_official_card_details")
    out.mkdir(parents=True)
    raw="".join(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n"
                for row in records).encode("utf8")
    (out/"official_asia_card_source_urls.jsonl").write_bytes(raw)
    receipt={
        "classification":CLASS,
        "source_cards":COUNT,
        "image_url_references":COUNT,
        "source_pages":list(PAGES),
        "source_request_budget":21*len(PAGES),
        "sources":sources,
        "source_url_manifest_sha256":hashlib.sha256(raw).hexdigest(),
        "image_binaries_downloaded":0,
        "live_vault_writes":0,
        "verified_new_canonical_cards":0,
        "artwork_rights_granted":False,
        "public_artwork_enabled":False,
        "exact_live_canonical_ids_verified":0
    }
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf8")
    return receipt


def test():
    assert PAGES==(2,3,4,5,6,7)
    assert COUNT==20*len(PAGES)
    assert pilot.TARGETS==( ("th","SV4K"),("id","SV6s") )
    print("PASS bounded Indonesian official 120 detail source batch, no image files")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--out",type=Path)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:test()
    else:
        if not a.out:p.error("--out required")
        r=run(a.out)
        print("OFFICIAL_ID_SV6S_120_SOURCE_DETAILS",json.dumps({
           "source_rows":r["source_cards"],"pages":len(r["source_pages"]),
           "image_url_refs":r["image_url_references"],
           "live_writes":r["live_vault_writes"],"artwork_binaries":r["image_binaries_downloaded"]
        },sort_keys=True))
