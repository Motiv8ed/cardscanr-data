#!/usr/bin/env python3
"""Offline, full-page official Asia list evidence; never fetches images or writes vault data.

Input is an already archived official HTML list response. A list-page image is
only a candidate, NEVER an exact card-printing match or an import approval.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup

HOST = "asia.pokemon-card.com"
DETAIL = re.compile(r"^/(th|id)/card-search/detail/([0-9]{1,10})/$")


def official_image(url, locale):
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.hostname == HOST
                and p.port in (None, 443) and not p.username and not p.password
                and not p.query and not p.fragment
                and p.path.startswith(f"/{locale}/card-img/")
                and p.path.lower().endswith((".png", ".jpg", ".jpeg", ".webp")))
    except ValueError:
        return False


def extract(raw, locale, list_url):
    if locale not in ("th", "id"):
        raise ValueError("unsupported_locale")
    p = urlsplit(list_url)
    if not (p.scheme == "https" and p.hostname == HOST and p.port in (None, 443)
            and not p.username and not p.password
            and p.path == f"/{locale}/card-search/list/"):
        raise ValueError("unverified_list_origin")
    soup = BeautifulSoup(raw, "html.parser")
    grouped = {}
    for anchor in soup.find_all("a", href=True):
        match = DETAIL.fullmatch(anchor["href"])
        if not match or match.group(1) != locale:
            continue
        card_id = match.group(2)
        entry = grouped.setdefault(card_id, set())
        for img in anchor.find_all("img", src=True):
            candidate = urljoin(list_url, img["src"])
            if official_image(candidate, locale):
                entry.add(candidate)
    if len(grouped) > 50:
        raise ValueError("list_page_over_50_requires_review")
    digest = hashlib.sha256(raw).hexdigest()
    return [{
        "classification": "OFFICIAL_LIST_IMAGE_CANDIDATE_NOT_EXACT_PRINT",
        "language": locale,
        "official_detail_id": card_id,
        "official_detail_url": f"https://{HOST}/{locale}/card-search/detail/{card_id}/",
        "source_list_url": list_url,
        "source_list_sha256": digest,
        "candidate_image_urls": sorted(images),
        "ambiguous_image_variants": len(images) != 1,
        "exact_print_match_proven": False,
        "artwork_rights_cleared": False,
        "live_link_inserted": False,
    } for card_id, images in sorted(grouped.items(), key=lambda x: int(x[0]))]


def self_test():
    root = "https://asia.pokemon-card.com/id/card-search/list/?expansionCodes=SV4a"
    anchors = "".join(
        f'<a href="/id/card-search/detail/{i}/"><img src="/id/card-img/{i}.png"></a>'
        for i in range(1, 51))
    records = extract(anchors.encode(), "id", root)
    assert len(records) == 50 and records[-1]["official_detail_id"] == "50"
    assert all(len(r["candidate_image_urls"]) == 1 for r in records)
    assert all(not r["exact_print_match_proven"] for r in records)
    assert not official_image("https://asia.pokemon-card.com.evil.test/id/card-img/1.png", "id")
    assert not official_image("https://asia.pokemon-card.com@evil.test/id/card-img/1.png", "id")
    assert not official_image("http://asia.pokemon-card.com/id/card-img/1.png", "id")
    assert not official_image("https://asia.pokemon-card.com/id/card-img/1.png?sig=1", "id")
    try:
        extract((anchors + '<a href="/id/card-search/detail/51/">51</a>').encode(), "id", root)
    except ValueError:
        pass
    else:
        raise AssertionError("oversized page accepted")
    print("PASS: 50 complete IDs, exact origin, ambiguous-print quarantine")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", type=Path)
    parser.add_argument("--list-url")
    parser.add_argument("--locale", choices=("th", "id"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not all((args.html, args.list_url, args.locale, args.out)):
        parser.error("--html, --list-url, --locale, --out required")
    if args.out.exists():
        raise FileExistsError("refuse_overwrite")
    records = extract(args.html.read_bytes(), args.locale, args.list_url)
    args.out.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                                for r in records), encoding="utf-8")
    print(json.dumps({"candidate_details": len(records),
                      "candidate_image_urls": sum(len(r["candidate_image_urls"]) for r in records),
                      "exact_matches": 0, "live_inserts": 0, "artwork_downloads": 0}))


if __name__ == "__main__":
    main()
