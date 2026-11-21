#!/usr/bin/env python3
"""Bounded Korean Pokémon TCG Pocket card-detail/image-link source pilot.

Source-only research. Downloads NO image bytes. Does not grant copyright,
store customer data, claim existing live IDs, or modify any CardScanR database.
Only observes card image URL references written in Korean card-detail HTML.
"""
from __future__ import annotations

import argparse
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
from urllib import robotparser, request
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, unquote

SOURCE = "https://tcgpocket.centropkmn.com"
HOST = "tcgpocket.centropkmn.com"
ASSET_HOST = "ptcgp-assets.centropkmn.com"
SET = re.compile(r"^[A-Z][0-9]{1,2}[ab]?$", re.I)
LOCAL = re.compile(r"^[0-9]{3}$")
DETAIL = re.compile(r"^/ko_KR/([a-z0-9]+(?:[ab])?)/([0-9]{3})-([a-z0-9][a-z0-9-]{0,110})$")
ASSET = re.compile(
    r"^/cards/[A-Za-z0-9_-]{1,100}/ko_KR/"
    r"([A-Za-z0-9]{2,5})_([0-9]{3})_[A-Za-z0-9_-]{1,120}\.(?:png|webp)$"
)
USER_AGENT = "CardScanR-CardMetadataResearch/1.0 (source-only, no images)"
MAX_LIST_BYTES = 4_000_000
MAX_DETAIL_BYTES = 1_500_000


class SourceHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.images = []
        self.title = ""
        self._h1 = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "img":
            for key in ("src", "data-src"):
                if a.get(key):
                    self.images.append(a[key])
        if tag == "meta" and a.get("property") in ("og:image", "twitter:image"):
            if a.get("content"):
                self.images.append(a["content"])
        if tag == "h1":
            self._h1 = True

    def handle_endtag(self, tag):
        if tag == "h1":
            self._h1 = False

    def handle_data(self, data):
        if self._h1:
            self.title += data.strip()


def allowed_html(url):
    u = urlsplit(url)
    return (u.scheme == "https" and u.hostname == HOST and u.port in (None, 443)
            and u.username is None and u.password is None and not u.query and not u.fragment)


def safe_card_image(url, code, num):
    try:
        p = urlsplit(url)
        if (p.scheme != "https" or p.hostname != ASSET_HOST
                or p.port not in (None, 443) or p.username or p.password
                or p.query or p.fragment):
            return False
        m = ASSET.fullmatch(unquote(p.path))
        return bool(m and m.group(1).upper() == code.upper() and m.group(2) == num)
    except ValueError:
        return False


def extract_cards(source_html, code):
    parser = SourceHTML()
    parser.feed(source_html)
    found = {}
    for href in parser.links:
        full = urljoin(SOURCE, unescape(href))
        if not allowed_html(full):
            continue
        m = DETAIL.fullmatch(urlsplit(full).path)
        if m and m.group(1).upper() == code.upper():
            found.setdefault(m.group(2), full)
    return dict(sorted(found.items()))


def extract_images(detail_html, code, num):
    parser = SourceHTML()
    parser.feed(detail_html)
    # Some frameworks embed URLs in scripts; accept only ACTUAL observed strings,
    # not fabricated deterministic paths.
    candidates = [unescape(s) for s in parser.images]
    embedded = re.findall(
        r"https://ptcgp-assets\.centropkmn\.com/cards/[A-Za-z0-9_/%.-]{1,280}",
        unescape(detail_html.replace("\\u002F", "/").replace("\\/", "/"))
    )
    candidates += embedded
    safe = set()
    for candidate in candidates:
        full = unquote(candidate)
        if safe_card_image(full, code, num):
            safe.add(full)
    return parser.title, sorted(safe)


def fetch_html(url):
    if not allowed_html(url):
        raise ValueError("source_request_url_not_allowlisted")
    with request.urlopen(request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"}
    ), timeout=20) as response:
        if response.status != 200:
            raise ValueError("source_html_not_200")
        length = MAX_LIST_BYTES if urlsplit(url).path.count("/") == 2 else MAX_DETAIL_BYTES
        raw = response.read(length + 1)
        if len(raw) > length:
            raise ValueError("source_response_exceeded_budget")
        if not response.headers.get("Content-Type", "").lower().startswith("text/html"):
            raise ValueError("source_not_html")
        return raw


def allowed_robots(path):
    robots = robotparser.RobotFileParser()
    robots.set_url(SOURCE + "/robots.txt")
    try:
        robots.read()
    except (URLError, HTTPError, TimeoutError) as exc:
        raise ValueError("source_robots_unavailable") from exc
    return robots.can_fetch(USER_AGENT, SOURCE + path)


def pilot(code="A1", count=3, output=None, pace=1.2):
    if not SET.fullmatch(code) or not 1 <= count <= 5 or not 1 <= pace <= 6:
        raise ValueError("invalid_pilot_bounds")
    lower = code.lower()
    page = "/ko_KR/" + lower
    if not allowed_robots(page):
        raise ValueError("source_robots_disallow_set")
    listed = fetch_html(SOURCE + page)
    links = extract_cards(listed.decode("utf8"), code)
    if not links:
        raise ValueError("source_list_no_card_details")
    rows = []
    for num, url in list(links.items())[:count]:
        path = urlsplit(url).path
        if not allowed_robots(path):
            raise ValueError("source_robots_disallow_detail")
        time.sleep(pace)
        raw = fetch_html(url)
        title, images = extract_images(raw.decode("utf8"), code, num)
        rows.append({
            "language": "ko", "set_code": code, "printed_number": num,
            "source_detail_url": url, "source_native_title": title,
            "source_html_sha256": hashlib.sha256(raw).hexdigest(),
            "observed_korean_image_urls": images,
            "exact_live_canonical_id_proven": False,
            "image_rights_verified": False,
            "image_binaries_downloaded": 0,
            "commercial_publication_allowed": False,
        })
    receipt = {
        "classification": "PRIVATE_KOREAN_POCKET_SOURCE_LINK_PILOT_ONLY",
        "source_list_url": SOURCE + page,
        "source_list_html_sha256": hashlib.sha256(listed).hexdigest(),
        "source_detail_urls_found": len(links),
        "source_cards_checked": len(rows),
        "cards_with_observed_korean_image_urls": sum(bool(x["observed_korean_image_urls"]) for x in rows),
        "distinct_observed_korean_image_urls": len(set(
            url for r in rows for url in r["observed_korean_image_urls"]
        )),
        "source_rows": rows,
        "production_writes": 0, "new_live_links": 0, "new_canonicals": 0,
        "artwork_binaries_downloaded": 0, "commercial_artwork_rights_verified": False
    }
    if output is not None:
        p = Path(output)
        if p.exists() or p.is_symlink():
            raise FileExistsError("output_must_not_exist")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    return receipt


def self_test():
    sample = ('<html><a href="/ko_KR/a1/001-bulbasaur">Bulbasaur</a>'
              '<a href="/ko_KR/a1/002-ivysaur">Ivysaur</a>'
              '<a href="https://unsafe.example/ko_KR/a1/003-venusaur">bad</a></html>')
    assert len(extract_cards(sample, "A1")) == 2
    art = "https://ptcgp-assets.centropkmn.com/cards/A1_Genetic_Apex/ko_KR/A1_001_Bulbasaur.png"
    assert safe_card_image(art, "A1", "001")
    assert not safe_card_image(art, "A1", "002")
    assert not safe_card_image(art.replace("/ko_KR/", "/en_US/"), "A1", "001")
    assert not safe_card_image(art.replace(ASSET_HOST, "evil.example"), "A1", "001")
    assert not safe_card_image(art + "?redirect=x", "A1", "001")
    name, images = extract_images('<h1>이상해씨</h1><img src="' + art + '">', "A1", "001")
    assert name == "이상해씨" and images == [art]
    assert extract_images('<img src="' + art + '">', "A1", "002")[1] == []
    print("PASS: Korean image refs exact set/number/language/host, no binary fetch")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--set", default="A1")
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--pace", type=float, default=1.2)
    p.add_argument("--output", type=Path)
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        self_test()
    else:
        if not a.output:
            p.error("--output required")
        r = pilot(a.set, a.count, a.output, a.pace)
        print("KO_POCKET_SOURCE_URL_PILOT", json.dumps({
            k: r[k] for k in ("source_detail_urls_found",
                "source_cards_checked", "cards_with_observed_korean_image_urls",
                "distinct_observed_korean_image_urls", "production_writes",
                "artwork_binaries_downloaded")
        }, sort_keys=True))
