#!/usr/bin/env python3
"""Official Asia source-only audit of last missing Thai and Indonesian printings.

Only real robots-permitted HTML card-detail pages and actually observed image
references. No fabricated URLs, images downloaded, commercial rights, live
database writes or automatic application of unverified variants.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import re
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from cardscanr_worldwide.collectors.pokemon_asia import (
    BASE_URL, Collector, parse_card_detail, parse_card_list,
)

MAX_PAGES=12
MAX_PER_PAGE=20
DETAIL=re.compile(r"^[0-9]{1,10}$")
SUPPORTED={("th","SVDs"),("id","SV4a"),("th","SV2a"),("id","SV2a")}
LIVE_KEYS={"canonical_printing_id","language","set_id","native_card_name","printed_collector_number"}


def name(value):
    return " ".join(unicodedata.normalize("NFKC",str(value or "")).casefold().split())


def valid_image(url,lang,detail_id):
    if not isinstance(url,str) or not DETAIL.fullmatch(detail_id):return False
    u=urlsplit(url)
    return (u.scheme=="https" and u.hostname=="asia.pokemon-card.com"
            and u.port in (None,443) and not u.query and not u.fragment
            and not u.username and not u.password
            and u.path==f"/{lang}/card-img/{lang}{detail_id.zfill(8)}.png")


def load_tail(path,lang,code):
    p=json.loads(path.read_text("utf8"))
    if p.get("classification")!="PINNED_LIVE_TH_ID_TAIL_20261122":
        raise ValueError("wrong_live_manifest")
    if set(p.get("snapshots",{}))!={"th","id"}:raise ValueError("missing_live_snapshot")
    hits=[x for x in p["rows"] if x.get("language")==lang and
          x.get("set_id")==f"tcgdex:asia:set:{code}"]
    expected={("th","SVDs"):23,("id","SV4a"):12,("th","SV2a"):6,("id","SV2a"):6}
    if len(hits)!=expected[(lang,code)]:raise ValueError("pinned_live_population_drift")
    seen=set()
    for r in hits:
        if set(r)!=LIVE_KEYS:raise ValueError("unexpected_live_identity_fields")
        cid=r["canonical_printing_id"]
        if cid!=f"tcgdex_asia_{code}_{r['printed_collector_number']}:{lang}:{'TH' if lang=='th' else 'ID'}" or cid in seen:
            raise ValueError("invalid_or_duplicate_live_card_id")
        seen.add(cid)
    return hits,p["snapshots"][lang]


def collect(lang,code,live,out):
    if (lang,code) not in SUPPORTED:raise ValueError("unapproved_source_set")
    if out.exists() or out.is_symlink():raise FileExistsError("new_output_required")
    wanted,snapshot=load_tail(live,lang,code)
    by_num=defaultdict(list)
    for r in wanted:by_num[r["printed_collector_number"]].append(r)
    details=defaultdict(list); errors=[]; pages=[]
    seen_ids=set()
    with tempfile.TemporaryDirectory(prefix="cardscanr-official-tail-") as temp:
        coll=Collector(lang,Path(temp),0.65)
        try:
            for page in range(1,MAX_PAGES+1):
                url=f"{BASE_URL}/{lang}/card-search/list/?expansionCodes={code}"
                if page>1:url+="&pageNo="+str(page)
                rsp=coll.fetch(url)
                if rsp.status!=200 or len(rsp.content)>3_000_000:raise ValueError("bad_official_list")
                ids,_=parse_card_list(rsp.content.decode("utf8"),lang)
                if len(ids)>MAX_PER_PAGE:raise ValueError("source_page_over_20_cards")
                if len(ids)!=len(set(ids)) or any(not DETAIL.fullmatch(i) for i in ids):
                    raise ValueError("duplicate_or_invalid_official_ids")
                if any(i in seen_ids for i in ids):raise ValueError("source_page_duplicate_detail")
                seen_ids.update(ids)
                pages.append({"page":page,"details_found":len(ids),
                    "list_html_sha256":hashlib.sha256(rsp.content).hexdigest()})
                if not ids:break
                for detail_id in ids:
                    url=f"{BASE_URL}/{lang}/card-search/detail/{detail_id}/"
                    try:
                        response=coll.fetch(url)
                        if response.status!=200 or len(response.content)>1_500_000:raise ValueError("bad_official_detail")
                        d=parse_card_detail(response.content.decode("utf8"),url)
                        num=str(d.get("collector_number") or "").strip()
                        if num not in by_num:continue
                        label=d.get("local_name")
                        img=d.get("image_url")
                        entry={"official_detail_id":detail_id,"detail_url":url,
                               "source_response_sha256":hashlib.sha256(response.content).hexdigest(),
                               "source_native_name":label,"source_collector_number":num,
                               "source_image_url":img if valid_image(img,lang,detail_id) else None,
                               "source_printed_set_code":d.get("printed_set_code")}
                        details[num].append(entry)
                    except Exception as exc:
                        errors.append({"detail_id":detail_id,"reason":type(exc).__name__+":"+str(exc)[:100]})
                if len(ids)<MAX_PER_PAGE:break
        finally:coll.close()
    output=[]
    for r in wanted:
        matched=[x for x in details.get(r["printed_collector_number"],[])
                 if name(x["source_native_name"])==name(r["native_card_name"]) and x["source_image_url"]]
        urls={x["source_image_url"] for x in matched}
        status=("UNIQUE_OFFICIAL_URL_NAME_NUMBER_VARIANT_UNVERIFIED" if len(matched)==1 and len(urls)==1 else
                "SOURCE_CARD_MISSING" if not details.get(r["printed_collector_number"]) else
                "OFFICIAL_NAME_OR_IMAGE_MISMATCH" if not matched else "AMBIGUOUS_SOURCE_VARIANTS")
        output.append({**r,"classification":status,
            "official_source":matched[0] if status=="UNIQUE_OFFICIAL_URL_NAME_NUMBER_VARIANT_UNVERIFIED" else None,
            "details_with_same_number":len(details.get(r["printed_collector_number"],[])),
            "finish_or_variant_verified":False,"artwork_rights_granted":False,
            "safe_for_vault_apply":False,"artwork_downloaded":0})
    raw="".join(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n" for x in output).encode()
    receipt={"classification":"OFFICIAL_ASIA_TH_ID_LAST75_SOURCE_ONLY",
             "locale":lang,"set_code":code,"live_snapshot_sha256":snapshot,
             "requested_missing_cards":len(wanted),"list_pages":pages,
             "official_details_scanned":len(seen_ids),"detail_errors":errors,
             "results":dict(Counter(x["classification"] for x in output)),
             "candidate_sha256":hashlib.sha256(raw).hexdigest(),
             "new_live_links":0,"images_downloaded":0,
             "commercial_artwork_rights_verified":False}
    out.mkdir()
    (out/"candidates.jsonl").write_bytes(raw)
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return receipt


def self_test():
    assert valid_image("https://asia.pokemon-card.com/id/card-img/id00001234.png","id","1234")
    assert not valid_image("https://asia.pokemon-card.com/th/card-img/th00001234.png","id","1234")
    assert not valid_image("https://asia.pokemon-card.com/id/card-img/id00001235.png","id","1234")
    assert not valid_image("https://asia.pokemon-card.com/id/card-img/id00001234.png?q=x","id","1234")
    assert name("  Nama Pokémon  ")==name("nama pokémon")
    print("PASS official URL exact observed detail ID and language, no image bytes")


if __name__=="__main__":
    a=argparse.ArgumentParser()
    a.add_argument("--language",choices=["th","id"])
    a.add_argument("--set-code")
    a.add_argument("--live",type=Path)
    a.add_argument("--out",type=Path)
    a.add_argument("--self-test",action="store_true")
    v=a.parse_args()
    if v.self_test:self_test()
    else:
        if not all((v.language,v.set_code,v.live,v.out)):a.error("missing arguments")
        print("OFFICIAL_LAST75_RESULT",json.dumps(collect(v.language,v.set_code,v.live,v.out),ensure_ascii=False))
