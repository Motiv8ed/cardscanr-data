#!/usr/bin/env python3
"""Collect only uniquely evidenced official TW image source URL refs for remaining live gaps.

Source-only and non-public: no artwork download, no image rights assertion.
The input is an authenticated read-only 210-card snapshot captured AFTER 187
private source URL inserts, with one checksum spanning seven read pages.
"""
from __future__ import annotations
import argparse
import base64
from collections import Counter,defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

PINNED_LIVE_SHA="aad3795f064ae68a19f12626dd1f55f354c5a7c5e37150c70f1ecd36cec36e23"
SOURCE_COMMIT="18e0de2feb7a35ebe4296ede0ba6cc768961489b"
BATCH_ID="twremaining20261122_00"
IMAGE_RE=re.compile(r"^/tw/card-img/tw[0-9]{8}\.png$")
DETAIL_RE=re.compile(r"^/tw/card-search/detail/[0-9]{1,12}/$")
ALLOWED_NUM_RE=re.compile(r"^[A-Za-z0-9.-]{1,36}$")
ALLOWED_SET_RE=re.compile(r"^[A-Za-z0-9.-]{1,40}$")
TARGET=63


def verify_url(raw,pattern):
    if not isinstance(raw,str):
        return False
    try:
        u=urlsplit(raw)
        return (u.scheme=="https" and u.hostname=="asia.pokemon-card.com"
                and u.port in (None,443) and not u.username and not u.password
                and not u.query and not u.fragment and bool(pattern.fullmatch(u.path)))
    except ValueError:
        return False


def norm(t):
    return unicodedata.normalize("NFC",str(t))


def read_live(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size>130_000:
        raise ValueError("live_210_snapshot_missing_or_unsafe")
    doc=json.loads(path.read_text(encoding="utf8"))
    if (doc["classification"]!="AUTHENTIC_CURRENT_LIVE_TW_UNLINKED_IDENTITIES_READONLY"
            or doc["snapshot_sha256"]!=PINNED_LIVE_SHA or doc["total_missing"]!=210
            or doc["pages"]!=7 or len(doc["page_proofs"])!=7 or len(doc["rows"])!=210):
        raise ValueError("live_snapshot_metadata_drift")
    if any(not isinstance(z.get("page_sha256"),str) or len(z["page_sha256"])!=64
           for z in doc["page_proofs"]):
        raise ValueError("invalid_page_proof")
    ids=set()
    selected=[]
    for r in doc["rows"]:
        if (r.get("has_image_link") is not False or r["language"]!="zh-tw"
                or r["canonical_printing_id"] in ids):
            raise ValueError("snapshot_link_or_duplicate")
        ids.add(r["canonical_printing_id"])
        if r["set_id"].startswith("pokemon-asia-hk-official:"):
            continue  # HK printing IDs are NOT TW source records.
        if not r["set_id"].startswith("tcgdex:asia:set:"):
            raise ValueError("unknown_live_set_namespace")
        code=r["set_id"].rsplit(":",1)[-1]
        if code in ("SV2a","SV3"):
            continue  # 138 known alternative-art ambiguities must stay quarantined.
        if not ALLOWED_SET_RE.fullmatch(code) or not ALLOWED_NUM_RE.fullmatch(r["printed_collector_number"]):
            raise ValueError("live_code_or_number_invalid")
        if r["canonical_printing_id"]!=f"tcgdex_asia_{code}_{r['printed_collector_number']}:zh-tw:TW":
            raise ValueError("live_canonical_id_set_number_mismatch")
        selected.append(r)
    if len(selected)!=TARGET:
        raise ValueError("remaining_uncovered_target_count_changed")
    return selected


def source_candidates(source_root,code,number):
    folder=source_root/"data_tc"/code
    if not folder.is_dir() or folder.is_symlink():
        return [],"source_set_not_found"
    candidates=[]
    for p in folder.glob("*.json"):
        stem=p.stem
        if not (stem==number or (stem.startswith(number+"-")
                                and stem[len(number)+1:].isdigit())):
            continue
        if not p.is_file() or p.is_symlink() or p.stat().st_size>250000:
            return [],"unsafe_provider_source_file"
        raw=p.read_bytes()
        try:
            j=json.loads(raw)
        except ValueError:
            return [],"invalid_provider_json"
        if (j.get("set_name")!=code or j.get("lang")!="tc"
            or j.get("number")!=number
            or not verify_url(j.get("img"),IMAGE_RE)
            or not verify_url(j.get("url"),DETAIL_RE)
            or not isinstance(j.get("name"),str) or not j["name"]):
            return [],"provider_identity_or_url_failed"
        candidates.append({
            "source_file":p.relative_to(source_root).as_posix(),
            "source_file_sha256":hashlib.sha256(raw).hexdigest(),
            "source_name":norm(j["name"]),
            "image_url":j["img"],
            "source_url":j["url"],
            "source_denominator":j.get("set_total"),
            "source_rarity":j.get("rarity"),
        })
    return candidates, None


def run(source_root,live_path,out,print_b64=False):
    if out.exists() or out.is_symlink():
        raise FileExistsError("output_must_not_exist")
    rows=read_live(live_path)
    matches=[];quarantine=[]
    for r in rows:
        code=r["set_id"].rsplit(":",1)[-1]
        num=r["printed_collector_number"]
        found,error=source_candidates(source_root,code,num)
        equal=[x for x in found if x["source_name"]==norm(r["native_card_name"])]
        base={"canonical_printing_id":r["canonical_printing_id"],
              "language":"zh-tw","set_id":r["set_id"],"set_code":code,
              "printed_number":num,"native_card_name":r["native_card_name"]}
        if len(found)==len(equal)==1 and error is None:
            x=equal[0]
            matches.append({**base,**x})
        else:
            reason=error or ("no_source_card" if not found else
                "wrong_native_name" if not equal else
                "multiple_source_printing_variants")
            quarantine.append({**base,"reason":reason,
                               "source_printings_found":len(found),
                               "exact_native_name_matches":len(equal)})
    if len(matches)+len(quarantine)!=TARGET:
        raise ValueError("target_not_accounted_for")
    out.mkdir(parents=True)
    def write_lines(name,values):
        s="".join(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n"
                for x in values)
        p=out/name;p.write_text(s,encoding="utf8")
        return {"filename":name,"rows":len(values),
                "sha256":hashlib.sha256(p.read_bytes()).hexdigest()}
    source_only=write_lines("source_candidates.jsonl",matches)
    rejected=write_lines("quarantined.jsonl",quarantine)
    sets={}
    for x in matches:
        sets.setdefault(x["set_code"],[]).append(x)
    stage={
        "schema_version":1,"source":"official_crosswalk",
        "artwork_rights_verified":False,"image_binaries_downloaded":0,
        "sets":[{
            "language":"zh-tw","provider_set_id":code,
            "source_response_sha256":hashlib.sha256(
                (SOURCE_COMMIT+":"+";".join(sorted(z["source_file_sha256"]
                for z in values))).encode("ascii")).hexdigest(),
            "source_commit":SOURCE_COMMIT
        } for code,values in sorted(sets.items())],
        "cards":[{
            "canonical_printing_id":x["canonical_printing_id"],
            "language":"zh-tw","provider_set_id":x["set_code"],
            "provider_card_id":x["set_code"]+"-"+x["printed_number"],
            "native_card_name":x["native_card_name"],
            "printed_collector_number":x["printed_number"],
            "status":"exact_unique"
        } for x in matches],
        "images":[{
            "canonical_printing_id":x["canonical_printing_id"],
            "language":"zh-tw","provider_set_id":x["set_code"],
            "image_url":x["image_url"]
        } for x in matches]
    }
    compressed=gzip.compress(json.dumps(stage,ensure_ascii=False,sort_keys=True,
                             separators=(",",":")).encode("utf8"),mtime=0,compresslevel=9)
    if not 0<len(matches)<=TARGET or len(compressed)>64000:
        raise ValueError("stage_not_safe_or_no_matches")
    (out/"exact_tw_current_210_batch.gz").write_bytes(compressed)
    record={
        "classification":"PRIVATE_TW_REMAINING_63_EXACT_SOURCE_URL_PROPOSAL_UNAPPLIED",
        "current_authenticated_live_snapshot_sha256":PINNED_LIVE_SHA,
        "source_repository":"type-null/PTCG-database",
        "source_commit":SOURCE_COMMIT,
        "current_missing_tw_card_count":210,
        "target_unlinked_card_count":TARGET,
        "single_source_native_name_number_url_matches":len(matches),
        "ambiguous_or_source_unavailable":len(quarantine),
        "distinct_source_urls":len({x["image_url"] for x in matches}),
        "by_set":dict(Counter(x["set_code"] for x in matches)),
        "source_candidates_file":source_only,
        "quarantine_file":rejected,
        "batch_id":BATCH_ID,
        "compressed_sha256":hashlib.sha256(compressed).hexdigest(),
        "compressed_bytes":len(compressed),
        "new_live_image_links":0,"new_live_cards":0,
        "rights_granted":False,"downloaded_image_files":0,
        "public_artwork_enabled":False,
        "requires_new_live_preflight":True,
        "warning":"Exact set/native-name/number source match only; physical finish not in live snapshot. No public artwork rights."
    }
    (out/"receipt.json").write_text(json.dumps(record,ensure_ascii=False,
                                    sort_keys=True,indent=2)+"\n",encoding="utf8")
    if print_b64:
        print("TW_REMAINING_ACTUAL_SOURCE_STAGE_CANDIDATE",
              json.dumps({"action":"artwork_vault_stage","draft_id":BATCH_ID,
                          "expected_sha256":record["compressed_sha256"],
                          "chunk_index":0,"limit":1,
                          "content":base64.b64encode(compressed).decode("ascii")},sort_keys=True))
    return record


def selftest():
    assert verify_url("https://asia.pokemon-card.com/tw/card-img/tw00006041.png",IMAGE_RE)
    assert not verify_url("http://asia.pokemon-card.com/tw/card-img/tw00006041.png",IMAGE_RE)
    assert not verify_url("https://evil.example/tw/card-img/tw00006041.png",IMAGE_RE)
    assert not verify_url("https://asia.pokemon-card.com/tw/card-img/tw00006041.png?u=2",IMAGE_RE)
    assert TARGET==63 and BATCH_ID.isascii()
    print("PASS strict TW official URL and live identity boundary, no art download")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--source-root",type=Path)
    p.add_argument("--live-snapshot",type=Path)
    p.add_argument("--out",type=Path)
    p.add_argument("--print-b64",action="store_true")
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:selftest()
    else:
        if a.source_root is None or a.live_snapshot is None or a.out is None:
            p.error("--source-root, --live-snapshot and --out are required")
        r=run(a.source_root,a.live_snapshot,a.out,a.print_b64)
        print("TW_REMAINING_63_SOURCE_RESULTS",json.dumps({
            k:r[k] for k in ("current_missing_tw_card_count","target_unlinked_card_count",
                "single_source_native_name_number_url_matches",
                "ambiguous_or_source_unavailable","distinct_source_urls",
                "new_live_image_links","by_set")
        },sort_keys=True))
