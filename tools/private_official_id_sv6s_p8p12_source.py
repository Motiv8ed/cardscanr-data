#!/usr/bin/env python3
"""Collect official Indonesian SV6s pages 8-12 with safe partial/empty endings.

This is native metadata plus private image URL references only. Uses existing
robots-aware Collector; does not fetch image binaries, modify databases, or
license publicly displayed artwork.
"""
from __future__ import annotations
import argparse,hashlib,json,re,tempfile
from pathlib import Path
from urllib.parse import urlsplit
from cardscanr_worldwide.collectors.pokemon_asia import (
    BASE_URL,Collector,parse_card_list,parse_card_detail
)

PAGES=tuple(range(8,13))
LANG="id"
SET="SV6s"
CLASSIFICATION="PRIVATE_OFFICIAL_ID_SV6S_PAGES_8_12_PARTIAL_SAFE_NATIVE_DETAILS"
ID=re.compile(r"^[0-9]{1,10}$")
IMAGE=re.compile(r"^/id/card-img/id[0-9]{8}\.png$")


def strict_url(url,lang="id"):
    if not isinstance(url,str):return False
    try:
        p=urlsplit(url)
        return (lang=="id" and p.scheme=="https" and p.hostname=="asia.pokemon-card.com"
                and p.port in (None,443) and not p.username and not p.password
                and not p.query and not p.fragment and IMAGE.fullmatch(p.path) is not None)
    except ValueError:return False


def plan_page(ids,already):
    """Avoid speculative page counts and never reassign repeated/variant ids."""
    if not isinstance(ids,list) or not 0<=len(ids)<=50:
        raise ValueError("page_count_unexpected")
    if len(ids)!=len(set(ids)) or any(not isinstance(x,str) or not ID.fullmatch(x) for x in ids):
        raise ValueError("bad_or_duplicate_card_ids_on_page")
    if any(x in already for x in ids):
        raise ValueError("card_identity_repeated_across_pages")
    return ids[:20]


def collect(out:Path,pace:float=0.75):
    if out.exists() or out.is_symlink():raise FileExistsError("new_output_required")
    if not 0.5<=pace<=4:raise ValueError("source_pace_invalid")
    records=[];source_pages=[];seen=set()
    with tempfile.TemporaryDirectory(prefix="cardscanr-id-sv6s-8-12-") as t:
        c=Collector(LANG,Path(t),pace)
        try:
            for page in PAGES:
                listurl=f"{BASE_URL}/{LANG}/card-search/list/?expansionCodes={SET}&pageNo={page}"
                fetched=c.fetch(listurl)
                if fetched.status!=200 or len(fetched.content)>3_000_000:
                    raise ValueError("official_list_http_or_size_drift")
                ids,_=parse_card_list(fetched.content.decode("utf8"),LANG)
                selected=plan_page(ids,seen)
                # Once the first empty page is observed, end. Never assume
                # later pages contain new cards or synthesize art identities.
                if len(ids)==0:
                    source_pages.append({"page":page,"found":0,"read":0,
                        "source_sha256":hashlib.sha256(fetched.content).hexdigest(),
                        "source_status":"empty_terminated"})
                    break
                if not selected:
                    raise ValueError("nonempty_page_no_card_ids")
                detail_count=0
                for card_id in selected:
                    detailurl=f"{BASE_URL}/{LANG}/card-search/detail/{card_id}/"
                    d=c.fetch(detailurl)
                    if d.status!=200 or len(d.content)>1_500_000:
                        raise ValueError("official_detail_http_or_size_drift")
                    details=parse_card_detail(d.content.decode("utf8"),detailurl)
                    name=details.get("local_name")
                    num=details.get("collector_number")
                    art=details.get("image_url")
                    if (not isinstance(name,str) or not name.strip() or len(name)>400
                        or not isinstance(num,str) or not num.strip() or len(num)>90
                        or not strict_url(art)):
                        raise ValueError("official_locale_name_number_or_image_missing")
                    if art!=f"{BASE_URL}/{LANG}/card-img/id{card_id.zfill(8)}.png":
                        raise ValueError("official_image_detail_id_conflict")
                    records.append({
                        "classification":"OFFICIAL_ASIA_SOURCE_URL_METADATA_ONLY",
                        "provider":"pokemon-card-asia","language":LANG,
                        "source_product_code":SET,
                        "official_detail_id":card_id,
                        "source_list_page":page,
                        "official_detail_url":detailurl,
                        "source_name_native":name,
                        "source_collector_number":num,
                        "source_printed_set_code":details.get("printed_set_code"),
                        "source_image_url_reference":art,
                        "source_response_sha256":hashlib.sha256(d.content).hexdigest(),
                        "source_list_sha256":hashlib.sha256(fetched.content).hexdigest(),
                        "exact_vault_match_proven":False,
                        "new_canonical_card":False,
                        "artwork_rights_cleared":False,
                        "image_downloaded":False,
                        "public_artwork_approved":False,
                    })
                    seen.add(card_id);detail_count+=1
                source_pages.append({"page":page,"found":len(ids),"read":detail_count,
                    "source_sha256":hashlib.sha256(fetched.content).hexdigest(),
                    "source_status":"partial" if len(ids)<20 else "full_or_more"})
                if len(ids)<20:
                    break
        finally:c.close()
    if len(records)>100 or len(records)!=len(seen):
        raise ValueError("source_card_total_or_duplicate")
    out.mkdir(parents=True)
    raw="".join(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n"
                for x in records).encode("utf8")
    (out/"official_asia_card_source_urls.jsonl").write_bytes(raw)
    receipt={
        "classification":CLASSIFICATION,"source_cards":len(records),
        "image_url_references":len(records),"source_pages":source_pages,
        "max_page":12,"first_page":8,"source_request_count":len(source_pages)+len(records),
        "source_url_manifest_sha256":hashlib.sha256(raw).hexdigest(),
        "source_identity_duplicates":0,"artwork_rights_granted":False,
        "image_binaries_downloaded":0,"live_vault_writes":0,
        "new_canonical_cards":0,"public_artwork_enabled":False,
        "current_live_missing_ids_verified":0
    }
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return receipt


def selftest():
    assert plan_page(["99991","99992"],{"99990"})==["99991","99992"]
    assert plan_page([],set())==[]
    for bad in (["1","1"],["1","bad"],["1","2"]):
        try:plan_page(bad,{"1"} if bad==["1","2"] else set())
        except ValueError:pass
        else:raise AssertionError("duplicate/invalid not rejected")
    assert strict_url("https://asia.pokemon-card.com/id/card-img/id00009635.png")
    assert not strict_url("https://asia.pokemon-card.com/th/card-img/th00009635.png")
    assert not strict_url("https://evil.example/id/card-img/id00009635.png")
    assert not strict_url("https://asia.pokemon-card.com/id/card-img/id00009635.png?a=1")
    print("PASS p8-12 bounded source, no duplicated ids, partial pages, official locale art only")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--out",type=Path)
    p.add_argument("--self-test",action="store_true")
    args=p.parse_args()
    if args.self_test:selftest()
    else:
        if args.out is None:p.error("--out required")
        rec=collect(args.out)
        print("OFFICIAL_ID_SV6S_PAGES8_12_SOURCE",json.dumps({
            "cards":rec["source_cards"],"page_results":rec["source_pages"],
            "image_urls":rec["image_url_references"],"live_imports":0
        },sort_keys=True))
