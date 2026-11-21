#!/usr/bin/env python3
"""Bounded NEW-set Thai/Indonesian official Pokémon Asia image URL source probe.

Fetches two product card-list HTML pages and at most five card-detail HTML
pages for each language, obeying robots.txt. Never requests image binaries.
Research-only: no canonical inserts, vault writes or publication permission.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import tempfile
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit,urljoin
from bs4 import BeautifulSoup
from cardscanr_worldwide.collectors.pokemon_asia import (
  BASE_URL, Collector, parse_card_list, parse_card_detail
)

TARGETS=(("th","SV4K"),("id","SV6s"))
MAX_PER_LOCALE=20
SAFE_CARD=re.compile(r"^[0-9]{1,10}$")

def accept_url(url,locale):
    if not isinstance(url,str) or len(url)>250:
        return False
    try:
        p=urlsplit(url)
        return (p.scheme=="https" and p.hostname=="asia.pokemon-card.com"
                and p.port in (None,443) and p.username is None
                and p.password is None and not p.query and not p.fragment
                and p.path.startswith(f"/{locale}/card-img/")
                and p.path.endswith((".png",".jpg",".jpeg",".webp")))
    except ValueError:
        return False

def run(out:Path,limit:int=20,delay:float=1.5,page:int=1):
    if not 1<=limit<=MAX_PER_LOCALE:
        raise ValueError("unbounded_card_detail_limit")
    if type(page) is not int or not 1<=page<=12:
        raise ValueError("unbounded_official_source_page")
    if not 0.5<=delay<=5:
        raise ValueError("unsafe_collection_pacing")
    if out.exists() or out.is_symlink():
        raise FileExistsError("output_folder_must_not_exist")
    rows=[]
    stats=[]
    with tempfile.TemporaryDirectory(prefix="cardscanr-official-asian-meta-") as temp:
        for lang,code in TARGETS:
            collector=Collector(lang,Path(temp),delay)
            try:
                listurl=f"{BASE_URL}/{lang}/card-search/list/?expansionCodes={code}"
                if page>1:
                    listurl+="&pageNo="+str(page)
                fetched=collector.fetch(listurl)
                if fetched.status!=200 or len(fetched.content)>3_000_000:
                    raise ValueError("source_list_http_or_size_drift")
                html=fetched.content.decode("utf8")
                ids,_=parse_card_list(html,lang)
                soup=BeautifulSoup(html,"html.parser")
                listing_image_urls={}
                for anchor in soup.find_all("a",href=True):
                    if not re.fullmatch(rf"/{lang}/card-search/detail/([0-9]{{1,10}})/",anchor["href"]):
                        continue
                    image=anchor.find("img",src=True)
                    if image:
                        full=urljoin(listurl,image["src"])
                        if accept_url(full,lang):
                            listing_image_urls[anchor["href"]]=full
                if not 1<=len(ids)<=50:
                    raise ValueError("unexpected_source_list_count")
                dig=hashlib.sha256(fetched.content).hexdigest()
                got=0
                for card_id in ids[:limit]:
                    if not SAFE_CARD.fullmatch(card_id):
                        raise ValueError("invalid_source_card_id")
                    url=f"{BASE_URL}/{lang}/card-search/detail/{card_id}/"
                    doc=collector.fetch(url)
                    if doc.status!=200 or len(doc.content)>1_500_000:
                        raise ValueError("source_detail_http_or_size_drift")
                    d=parse_card_detail(doc.content.decode("utf8"),url)
                    art=d.get("image_url")
                    if art is not None and not accept_url(art,lang):
                        raise ValueError("official_source_image_host_or_path_drift")
                    name=d.get("local_name")
                    number=d.get("collector_number")
                    if not isinstance(name,str) or not name.strip():
                        raise ValueError("missing_native_name")
                    rows.append({
                      "classification":"OFFICIAL_ASIA_SOURCE_URL_METADATA_ONLY",
                      "provider":"pokemon-card-asia","language":lang,
                      "source_product_code":code,
                      "official_detail_id":card_id,
                      "source_list_page":page,
                      "official_detail_url":url,
                      "source_name_native":name,
                      "source_collector_number":number,
                      "source_printed_set_code":d.get("printed_set_code"),
                      "source_image_url_reference":art,
                      "source_response_sha256":hashlib.sha256(doc.content).hexdigest(),
                      "source_list_sha256":dig,
                      "exact_vault_match_proven":False,
                      "new_canonical_card":False,
                      "artwork_rights_cleared":False,
                      "image_downloaded":False,
                      "public_artwork_approved":False,
                    })
                    got+=1
                stats.append({
                  "locale":lang,"set":code,"source_list_page":page,"list_cards":len(ids),
                  "details_scrutinized":got,
                  "list_html_image_urls":len(listing_image_urls),
                  "list_image_refs_retrieved_without_detail_fetch":False,
                  "official_url_refs":sum(bool(r["source_image_url_reference"]) for r in rows if r["language"]==lang),
                  "source_list_sha256":dig,
                })
            finally:
                collector.close()
    out.mkdir(parents=True)
    file=out/"official_asia_card_source_urls.jsonl"
    raw=("\n".join(json.dumps(r,sort_keys=True,ensure_ascii=False,separators=(",",":"))
                    for r in rows)+"\n").encode()
    file.write_bytes(raw)
    receipt={
      "classification":"PRIVATE_OFFICIAL_ASIA_CARD_URL_RESEARCH_ONLY",
      "generated_utc":datetime.now(timezone.utc).isoformat(),
      "sources":stats,
      "source_cards":len(rows),
      "source_list_page":page,
      "image_url_references":sum(bool(x["source_image_url_reference"]) for x in rows),
      "source_url_manifest_sha256":hashlib.sha256(raw).hexdigest(),
      "source_request_budget":(limit+1)*len(TARGETS),
      "image_binaries_downloaded":0,
      "live_vault_writes":0,
      "verified_new_canonical_cards":0,
      "artwork_rights_granted":False,
      "public_artwork_enabled":False,
      "exact_live_canonical_ids_verified":0,
    }
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2,sort_keys=True)+"\n")
    return receipt

def self_test():
    assert accept_url("https://asia.pokemon-card.com/th/card-img/th00000419.png","th")
    assert not accept_url("https://asia.pokemon-card.com/th/card-img/th00000419.png","id")
    assert not accept_url("https://evil.example/th/card-img/th00000419.png","th")
    assert not accept_url("http://asia.pokemon-card.com/th/card-img/th00000419.png","th")
    assert not accept_url("https://asia.pokemon-card.com/th/card-img/th00000419.png?token=x","th")
    assert not accept_url("https://asia.pokemon-card.com@evil.example/th/card-img/test.png","th")
    print("PASS official source URL allowlist and no image file paths")

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--out",type=Path)
    p.add_argument("--limit",type=int,default=20)
    p.add_argument("--page",type=int,default=1)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:self_test()
    else:
        if not a.out:p.error("--out required")
        result=run(a.out,a.limit,page=a.page)
        print("OFFICIAL_ASIA_SOURCE_URL_PILOT",json.dumps({
          "cards":result["source_cards"],
          "image_url_references":result["image_url_references"],
          "sources":result["sources"],
          "canonical_additions":0,"artwork_downloads":0,
        },ensure_ascii=False,sort_keys=True))
