#!/usr/bin/env python3
"""Read-only Korean Pocket image URL discovery on an independently permitted site.

Never requests image files, never guesses URL templates, no publication rights.
Refuses a source if robots disallows collection; no authentication or bypass.
"""
from __future__ import annotations
import argparse, hashlib, html, json, re, time
from pathlib import Path
from urllib import request,robotparser
from urllib.parse import unquote,urlsplit
from urllib.error import HTTPError,URLError

SOURCE="https://tcgpockettrades.com"
HOST="tcgpockettrades.com"
CDN="tcgpocket-card-art-a.pages.dev"
SET=re.compile(r"^[A-Z][0-9]{1,2}[ab]?$",re.I)
URL=re.compile(r"https://tcgpocket-card-art-a\.pages\.dev/images/game-cards/v2/[A-Za-z0-9_/-]{1,110}/ko\.[A-Za-z0-9]{8,64}\.webp")
UA="CardScanR-Catalogue-Research/1.0 (read-only source URLs, no image download)"

def valid_art_url(url):
    try:
        p=urlsplit(url)
        return (p.scheme=="https" and p.hostname==CDN and p.port in (None,443)
                and not p.username and not p.password and not p.query and not p.fragment
                and bool(URL.fullmatch(url)))
    except ValueError: return False

def urls_in_html(page):
    text=html.unescape(page.replace("\\u002F","/").replace("\\/","/"))
    # Accept only real observed absolute URLs, not generated pattern paths.
    candidates=set(URL.findall(text))
    # Next.js often percent-encodes a URL into the thumb proxy.
    candidates |= set(URL.findall(unquote(text)))
    return sorted(url for url in candidates if valid_art_url(url))

def can_fetch_page(path):
    robots=robotparser.RobotFileParser()
    robots.set_url(SOURCE+"/robots.txt")
    try:robots.read()
    except (HTTPError,URLError,TimeoutError) as exc:
        raise ValueError("cannot_verify_source_robots") from exc
    return robots.can_fetch(UA,SOURCE+path)

def fetch_page(path):
    if not path.startswith("/ko/themed-collections/") or not path.endswith("/"):
        raise ValueError("unapproved_source_path")
    url=SOURCE+path
    r=request.Request(url,headers={"User-Agent":UA,"Accept":"text/html"})
    with request.urlopen(r,timeout=20) as response:
        if response.status!=200 or "text/html" not in response.headers.get("Content-Type",""):
            raise ValueError("source_html_status_or_type")
        body=response.read(3_000_001)
        if len(body)>3_000_000:raise ValueError("source_html_exceeds_limit")
        return body

def collect(sets,out,pace=1.2):
    if out.exists() or out.is_symlink():raise FileExistsError("output_not_fresh")
    if not 1.0<=pace<=5.0 or not 1<=len(sets)<=3 or any(not SET.fullmatch(s) for s in sets):
        raise ValueError("invalid_source_pacing_or_set")
    receipts=[]
    for i,s in enumerate(sets):
        path="/ko/themed-collections/"+s+"/"
        if not can_fetch_page(path):
            raise ValueError("source_robots_disallow_"+s)
        if i:time.sleep(pace)
        page=fetch_page(path)
        urls=urls_in_html(page.decode("utf8"))
        receipts.append({"source_url":SOURCE+path,
          "set_code":s,"html_sha256":hashlib.sha256(page).hexdigest(),
          "observed_korean_image_urls":urls,
          "distinct_observed_source_urls":len(urls),
          "exact_live_card_matches_verified":0})
    all_urls=set(url for r in receipts for url in r["observed_korean_image_urls"])
    report={"classification":"PRIVATE_KO_POCKET_EXTERNAL_URLS_ONLY",
      "pages":receipts,"source_pages":len(receipts),
      "observed_korean_image_url_refs":len(all_urls),
      "exact_live_card_matches_verified":0,"image_binaries_downloaded":0,
      "production_writes":0,"artwork_rights_granted":False,
      "commercial_redistribution_allowed":False}
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
    return report

def self_test():
    url="https://tcgpocket-card-art-a.pages.dev/images/game-cards/v2/PK_10_000010_00/ko.eab63f0edd1d5aca.webp"
    assert valid_art_url(url)
    assert urls_in_html('<img alt="이상해씨" src="'+url+'">')==[url]
    assert not valid_art_url(url.replace("/ko.","/en."))
    assert not valid_art_url(url.replace(CDN,"bad.example"))
    assert not valid_art_url(url+"?x=1")
    assert urls_in_html('<img src="https://bad.example/foo.webp">')==[]
    print("PASS Korean-specific source HTML URL extraction, source controls, no binaries")

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--set",action="append",dest="sets")
    p.add_argument("--out",type=Path)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:self_test()
    else:
        if not a.out:p.error("--out required")
        doc=collect(a.sets or ["A1"],a.out)
        print("KO_POCKET_ALT_SOURCE_PILOT",json.dumps({
            "source_pages":doc["source_pages"],
            "observed_korean_image_url_refs":doc["observed_korean_image_url_refs"],
            "production_writes":doc["production_writes"],
            "image_binaries_downloaded":doc["image_binaries_downloaded"]
        },sort_keys=True))
