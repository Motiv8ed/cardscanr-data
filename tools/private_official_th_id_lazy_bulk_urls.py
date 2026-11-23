#!/usr/bin/env python3
"""Read official Pokémon Asia lazy image URLs from actual HTML list pages.

No artwork binaries, no guessing, no card detail requests, no live imports,
no use-rights assertion. Every row must be observed in a robots-permitted HTML
response and crosschecked against the exact source detail ID. Card's native
name/number/printing finish are intentionally UNVERIFIED until detail matching.
"""
from __future__ import annotations
import argparse,hashlib,json,re,tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin,urlsplit
from bs4 import BeautifulSoup
from cardscanr_worldwide.collectors.pokemon_asia import BASE_URL,Collector,parse_card_list
from tools.private_official_asia_th_id_url_pilot import accept_url

SETS={"th":("SV7s","SV4K","SV3","SV2a"),
      "id":("SV4a","SV6s","SV7a","SV5a")}
MAX_PAGE=20  # Official Indonesian SV4a listing currently spans 18 pages; 12 truncates it.
DETAIL=re.compile(r"^/(th|id)/card-search/detail/([0-9]{1,10})/$")
IMAGE=re.compile(r"^/(th|id)/card-img/(th|id)([0-9]{8})\.png$")
MAX_LIST_BYTES=3_000_000


def source_rows(html,source_url,lang,code,page,sha):
    soup=BeautifulSoup(html,"html.parser")
    entries=[]
    byid=set()
    for a in soup.find_all("a",href=True):
        match=DETAIL.fullmatch(a.get("href",""))
        if not match or match.group(1)!=lang:continue
        cid=match.group(2)
        found=[]
        for img in a.find_all("img"):
            # Lazy loaded on official site; src is deliberately empty.
            raw=img.get("data-original") or img.get("src")
            if not raw:continue
            full=urljoin(source_url,str(raw))
            if not accept_url(full,lang):continue
            u=urlsplit(full);m=IMAGE.fullmatch(u.path)
            if (not m or m.group(1)!=lang or m.group(2)!=lang
                    or int(m.group(3))!=int(cid)):
                raise ValueError("official_card_detail_image_id_mismatch")
            found.append(full)
        if len(found)!=1 or cid in byid:
            raise ValueError("official_list_missing_or_ambiguous_image")
        byid.add(cid)
        entries.append({
            "classification":"PUBLIC_OFFICIAL_LISTING_LAZY_DECLARED_IMAGE_URL_NOT_PRINTING",
            "provider":"pokemon-asia","language":lang,"set_filter_code":code,
            "source_list_page":page,"official_detail_id":cid,
            "source_detail_url":BASE_URL+"/"+lang+"/card-search/detail/"+cid+"/",
            "observed_image_url":found[0],
            "source_list_sha256":sha,
            "native_card_name":None,
            "printed_collector_number":None,
            "printing_variant_verified":False,
            "exact_live_identity_verified":False,
            "artwork_downloaded":False,
            "artwork_rights_verified":False,
            "public_artwork_allowed":False
        })
    cardids,nextpage=parse_card_list(html,lang)
    if len(entries)!=len(cardids) or len(entries)>50:
        raise ValueError("list_cards_missing_source_image_url")
    return entries,nextpage


def collect(out,pages=MAX_PAGE):
    if out.exists() or out.is_symlink():
        raise FileExistsError("output_must_be_fresh")
    if type(pages) is not int or not 1<=pages<=MAX_PAGE:
        raise ValueError("page_budget_invalid")
    sources=[]
    errors=[]
    rows=[]
    duplicate_pages=Counter()
    with tempfile.TemporaryDirectory(prefix="cardscanr-official-list-bulk-") as temp:
        for lang,codes in SETS.items():
            collector=Collector(lang,Path(temp),0.75)
            try:
                for code in codes:
                    for page in range(1,pages+1):
                        listurl=f"{BASE_URL}/{lang}/card-search/list/?expansionCodes={code}"
                        if page>1:listurl+="&pageNo="+str(page)
                        try:
                            fetched=collector.fetch(listurl)
                            if fetched.status!=200 or len(fetched.content)>MAX_LIST_BYTES:
                                raise ValueError("official_card_list_status_or_size_invalid")
                            sha=hashlib.sha256(fetched.content).hexdigest()
                            records,hasnext=source_rows(
                                fetched.content.decode("utf8"),listurl,lang,code,page,sha
                            )
                            if not records:
                                break
                            sources.append({"locale":lang,"set_filter_code":code,
                                "page":page,"url":listurl,"sha256":sha,
                                "source_image_url_refs":len(records)})
                            rows.extend(records)
                            if not hasnext:break
                        except Exception as exc:
                            errors.append({"locale":lang,"set_filter_code":code,
                                "page":page,"error":type(exc).__name__+":"+str(exc)[:150]})
                            break
            finally:
                collector.close()
    dedup={}
    conflicting=[]
    for row in rows:
        key=(row["language"],row["official_detail_id"])
        previous=dedup.get(key)
        if previous:
            if previous["observed_image_url"]!=row["observed_image_url"]:
                conflicting.append(key)
            duplicate_pages[row["language"]]+=1
        else:dedup[key]=row
    if conflicting:
        raise ValueError("same_official_card_id_conflicting_source_images")
    selected=sorted(dedup.values(),key=lambda x:(x["language"],int(x["official_detail_id"])))
    out.mkdir(parents=True)
    body="".join(json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n"
                  for x in selected).encode("utf8")
    (out/"official_lazy_card_source_urls.jsonl").write_bytes(body)
    receipt={
        "classification":"PRIVATE_OFFICIAL_TH_ID_LAZY_LIST_BULK_IMAGE_URL_REFS_ONLY",
        "requested_max_pages_per_set":pages,"requested_sets":sum(len(x) for x in SETS.values()),
        "pages_succeeded":len(sources),"pages_failed":len(errors),
        "image_url_refs_total":len(rows),"distinct_official_detail_image_refs":len(selected),
        "duplicate_listing_refs":dict(duplicate_pages),
        "manifest_sha256":hashlib.sha256(body).hexdigest(),
        "sources":sources,"errors":errors,
        "card_details_fetched":0,"artwork_binaries_downloaded":0,
        "exact_current_live_card_matches":0,"new_live_image_links":0,
        "new_canonical_cards":0,"artwork_rights_verified":False,
        "safe_for_public_artwork":False,
        "needs_native_name_printed_number_variant_crosswalk":True
    }
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf8")
    return receipt


def self_test():
    html='<li class="card"><a href="/th/card-search/detail/9635/"><div><img src="" data-original="https://asia.pokemon-card.com/th/card-img/th00009635.png"/></div></a></li>'
    a,n=source_rows(html,"https://asia.pokemon-card.com/th/card-search/list/?expansionCodes=SV7s","th","SV7s",1,"a"*64)
    assert len(a)==1 and a[0]["official_detail_id"]=="9635"
    assert a[0]["observed_image_url"].endswith("/th00009635.png")
    assert a[0]["native_card_name"] is None and a[0]["printed_collector_number"] is None
    assert not a[0]["artwork_rights_verified"]
    assert MAX_PAGE >= 18, "SV4a official listing has 18 pages; do not truncate at 12"
    bad=html.replace("th00009635","th00009636")
    try:source_rows(bad,"https://asia.pokemon-card.com/th/card-search/list/?expansionCodes=SV7s","th","SV7s",1,"a"*64)
    except ValueError:pass
    else:raise AssertionError("mismatched detail-to-image ID accepted")
    print("PASS official lazy image extraction + exact detail ID, source only")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--out",type=Path)
    p.add_argument("--pages",type=int,default=MAX_PAGE)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:self_test()
    else:
        if a.out is None:p.error("--out required")
        doc=collect(a.out,a.pages)
        print("TH_ID_BULK_GENUINE_OFFICIAL_LAZY_IMAGE_URLS",json.dumps({
            k:doc[k] for k in ("requested_sets","pages_succeeded","pages_failed",
                "image_url_refs_total","distinct_official_detail_image_refs",
                "card_details_fetched","exact_current_live_card_matches",
                "new_live_image_links")
        },sort_keys=True))
