#!/usr/bin/env python3
"""Inspect whether robots-approved Pokémon Asia list pages carry card art + identities.

Exactly two list HTML requests, zero image or detail page requests, zero writes
to CardScanR production or VaultSync. Returns a bounded public markup shape
summary, not private account data. No artwork reuse rights are inferred.
"""
from __future__ import annotations
from bs4 import BeautifulSoup
import hashlib,json,re,tempfile
from pathlib import Path
from urllib.parse import urljoin
from cardscanr_worldwide.collectors.pokemon_asia import BASE_URL,Collector,parse_card_list
from tools.private_official_asia_th_id_url_pilot import accept_url

TARGETS=(("th","SV7s"),("id","SV4a"))

def run(out:Path):
    if out.exists() or out.is_symlink():raise FileExistsError("output_must_not_exist")
    samples=[]
    with tempfile.TemporaryDirectory(prefix="cardscanr-official-list-only-") as temp:
        for lang,code in TARGETS:
            c=Collector(lang,Path(temp),0.9)
            try:
                url=f"{BASE_URL}/{lang}/card-search/list/?expansionCodes={code}"
                result=c.fetch(url)
                if result.status!=200 or len(result.content)>3_000_000:
                    raise ValueError("official_list_unexpected_http_or_size")
                soup=BeautifulSoup(result.content.decode("utf8"),"html.parser")
                cardids,nextpage=parse_card_list(str(soup),lang)
                regex=re.compile(rf"^/{lang}/card-search/detail/([0-9]{{1,10}})/$")
                anchors=[]
                for a in soup.find_all("a",href=True):
                    m=regex.fullmatch(a["href"])
                    if m:
                        images=[]
                        for im in a.find_all("img"):
                            # Official website lazy loads images. Actual
                            # observed URL is in data-original, while src="".
                            raw_src=str(im.get("data-original") or im.get("src") or "")
                            src=urljoin(url,raw_src) if raw_src else None
                            images.append({"art":src if src and accept_url(src,lang) else None,
                                          "source_attribute":"data-original" if im.get("data-original") else "src",
                                          "alt":str(im.get("alt") or "")[:100],
                                          "class":list(im.get("class") or [])[:4]})
                        anchors.append({
                            "detail_id":m.group(1),
                            "class":list(a.get("class") or [])[:5],
                            "direct_text":a.get_text(" ",strip=True)[:250],
                            "images":images[:4],
                            "inner_html_shape":str(a)[:700],
                        })
                examples=anchors[:4]
                samples.append({
                    "locale":lang,"provider_set_code":code,
                    "listing_html_sha256":hashlib.sha256(result.content).hexdigest(),
                    "detail_links":len(cardids),
                    "anchors_with_image":sum(any(im["art"] for im in a["images"]) for a in anchors),
                    "anchors_with_native_text":sum(bool(a["direct_text"]) for a in anchors),
                    "first_four_public_anchor_shapes":examples,
                    "next_page_detected":nextpage,
                    "image_binaries_downloaded":0,
                    "detail_page_requests":0,
                })
            finally:c.close()
    out.mkdir(parents=True)
    report={"classification":"PRIVATE_OFFICIAL_TH_ID_LISTING_HTML_IDENTITY_SHAPE_RESEARCH_ONLY",
            "source_list_pages":2,"sources":samples,
            "image_binaries_downloaded":0,"detail_page_requests":0,
            "live_vault_writes":0,"public_artwork_rights_granted":False}
    (out/"shape_receipt.json").write_text(json.dumps(report,ensure_ascii=False,
                                        sort_keys=True,indent=2)+"\n",encoding="utf8")
    return report

def test():
    assert accept_url("https://asia.pokemon-card.com/id/card-img/id00009901.png","id")
    assert not accept_url("https://asia.pokemon-card.com/id/card-img/id00009901.png","th")
    print("PASS official Asia list-only robots-gated 2 requests, no artwork downloads")

if __name__=="__main__":
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument("--out",type=Path)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:test()
    else:
        if not a.out:p.error("--out required")
        r=run(a.out)
        print("OFFICIAL_ASIA_LISTING_ONLY_SHAPE_EVIDENCE",json.dumps(r,ensure_ascii=False))
