#!/usr/bin/env python3
"""Acquire source-declared TCGdex card URLs for the actual live Thai/ID tail.

Source-only: no image bytes, copyrighted-art publication, canonical writes,
invented URLs, silent variant equivalence, or hidden live matching claims.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib import request
from urllib.error import HTTPError, URLError

from private_th_id_tcgdex_set_image_source import safe_base

BASE = "https://api.tcgdex.net/v2"
UA = "CardScanR-Private-Last75-Card-URL-Research/1.0"
CODES = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
NUMBER = re.compile(r"^[A-Za-z0-9-]{1,18}$")
EXPECTED = {"th": 49, "id": 26}
LIMIT = 1_900_000


def canonical_name(value):
    return " ".join(unicodedata.normalize("NFC", str(value or "")).casefold().split())


def fetch(url):
    if not url.startswith(BASE + "/") or any(x in url for x in ("?", "#", "..")):
        raise ValueError("source_url_not_allowlisted")
    req = request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
    with request.urlopen(req, timeout=24) as res:
        if res.status != 200 or "json" not in res.headers.get("Content-Type", "").lower():
            raise ValueError("source_not_json_200")
        blob = res.read(LIMIT + 1)
        if len(blob) > LIMIT: raise ValueError("source_response_over_limit")
    return json.loads(blob), hashlib.sha256(blob).hexdigest()


def read_live(file):
    doc = json.loads(file.read_text(encoding="utf8"))
    if doc.get("classification") != "PINNED_LIVE_TH_ID_TAIL_20261122":
        raise ValueError("untrusted_identity_manifest")
    if not isinstance(doc.get("snapshots"), dict) or set(doc["snapshots"]) != set(EXPECTED):
        raise ValueError("missing_live_snapshot_provenance")
    seen = set()
    counts = Counter()
    for row in doc["rows"]:
        if set(row) != {"canonical_printing_id", "language", "set_id",
                        "native_card_name", "printed_collector_number"}:
            raise ValueError("unexpected_live_identity_field")
        lang = row["language"]
        if lang not in EXPECTED or not isinstance(row["canonical_printing_id"], str):
            raise ValueError("bad_live_language_or_id")
        cid = row["canonical_printing_id"]
        if cid in seen: raise ValueError("repeated_live_printing")
        seen.add(cid)
        counts[lang] += 1
        code = row["set_id"].split(":")[-1]
        if (not CODES.fullmatch(code) or
            not NUMBER.fullmatch(row["printed_collector_number"]) or
            not canonical_name(row["native_card_name"])):
            raise ValueError("invalid_live_code_number_or_name")
        if row["set_id"].startswith("tcgdex:asia:set:"):
            expect = f"tcgdex_asia_{code}_{row['printed_collector_number']}:{lang}:{'TH' if lang=='th' else 'ID'}"
            if cid != expect: raise ValueError("live_canonical_printing_mismatch")
    if dict(counts) != EXPECTED: raise ValueError("missing_live_identity_rows")
    return doc["rows"], doc["snapshots"]


def research(live, out, pace=0.65):
    if out.exists() or out.is_symlink(): raise FileExistsError("new_output_required")
    if not 0.5 <= pace <= 4: raise ValueError("invalid_rate")
    rows, snapshots = read_live(live)
    grouped = {}
    for row in rows:
        lang, code = row["language"], row["set_id"].split(":")[-1]
        grouped.setdefault((lang,code), []).append(row)
    source = {}
    errors = []
    for lang,code in sorted(grouped):
        endpoint = f"{BASE}/{lang}/sets/{code}"
        time.sleep(pace)
        try:
            obj,sha = fetch(endpoint)
            if not isinstance(obj,dict) or obj.get("id") != code or not isinstance(obj.get("cards"),list):
                raise ValueError("wrong_set_or_missing_cards")
            index = {}
            for c in obj["cards"]:
                num = c.get("localId")
                if not isinstance(num,str): continue
                index.setdefault(num,[]).append(c)
            source[(lang,code)] = (index,sha,endpoint)
        except (HTTPError, URLError, TimeoutError, ValueError, TypeError) as err:
            errors.append({"language":lang,"set_code":code,"source_url":endpoint,
                           "error":type(err).__name__+":"+str(err)[:120]})
    results = []
    for row in rows:
        lang = row["language"]
        code = row["set_id"].split(":")[-1]
        num = row["printed_collector_number"]
        matches,sha,endpoint = source.get((lang,code),({},None,None))
        candidates = matches.get(num,[])
        accepted = []
        for c in candidates:
            if c.get("id") != f"{code}-{num}": continue
            base = c.get("image")
            if safe_base(base,lang,code,num):
                accepted.append((c.get("name"),base+"/low.webp"))
        distinct = list(dict.fromkeys((canonical_name(n),url) for n,url in accepted))
        status = "SOURCE_NOT_DECLARED"
        if len(distinct) > 1: status = "AMBIGUOUS_PROVIDER_CARD"
        elif len(distinct) == 1:
            status = ("EXACT_NATIVE_NAME_SOURCE_URL_VARIANT_UNVERIFIED"
                      if distinct[0][0] == canonical_name(row["native_card_name"])
                      else "SOURCE_NATIVE_NAME_MISMATCH")
        results.append({
            **row, "classification":status,"source_set_response_sha256":sha,
            "source_set_url":endpoint,"source_declarations":len(accepted),
            "source_image_url":distinct[0][1] if len(distinct)==1 else None,
            "source_name":accepted[0][0] if len(distinct)==1 else None,
            "live_variant_verified":False, "source_image_rights_verified":False,
            "safe_for_vault_apply":False, "artwork_downloaded":0
        })
    result = "".join(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n" for x in results).encode()
    counts = dict(sorted(Counter(x["classification"] for x in results).items()))
    receipt = {
        "classification":"REAL_TH_ID_TAIL_SOURCE_URL_RESEARCH_ONLY",
        "live_snapshot_sha256_by_language":snapshots,
        "live_unlinked_identity_rows":len(rows),
        "source_sets_attempted":len(grouped),
        "source_sets_loaded":len(source),
        "source_errors":errors,
        "candidate_counts":counts,
        "candidate_jsonl_sha256":hashlib.sha256(result).hexdigest(),
        "actual_new_live_links":0,"image_binaries_downloaded":0,
        "artwork_rights_verified":False,"public_artwork_enabled":False
    }
    out.mkdir()
    (out/"candidates.jsonl").write_bytes(result)
    (out/"receipt.json").write_text(json.dumps(receipt,sort_keys=True,indent=2,ensure_ascii=False)+"\n",encoding="utf8")
    return receipt


def self_test():
    row = {"canonical_printing_id":"tcgdex_asia_SV2a_196:id:ID","language":"id",
           "set_id":"tcgdex:asia:set:SV2a","native_card_name":"Undangan Erika[Supporter]",
           "printed_collector_number":"196"}
    assert canonical_name("Undangan Erika[Supporter]") == "undangan erika[supporter]"
    assert safe_base("https://assets.tcgdex.net/id/SV/SV2a/196","id","SV2a","196")
    assert not safe_base("https://assets.tcgdex.net/th/SV/SV2a/196","id","SV2a","196")
    assert row["canonical_printing_id"] == "tcgdex_asia_SV2a_196:id:ID"
    print("PASS exact real TCGdex image declaration required; no derived URLs")


if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--live",type=Path)
    ap.add_argument("--out",type=Path)
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test: self_test()
    else:
        if not a.live or not a.out: ap.error("--live and --out required")
        print("SOURCE_TAIL_RESEARCH_PASS",json.dumps(research(a.live,a.out),sort_keys=True))
