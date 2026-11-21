#!/usr/bin/env python3
"""Cross-check actual live-missing TW energy identities against pinned official URL refs.

This tool reads a historical, explicit page-1 sample of the AUTHENTICATED live
Artwork Vault, then verifies the corresponding upstream card JSON documents
from a pinned public Git checkout. Strict source-only proof, no live writes.
A matching source URL does NOT grant the right to download/display artwork.
"""
from __future__ import annotations
import argparse,hashlib,json,re
from pathlib import Path
from urllib.parse import urlsplit

PIN="18e0de2feb7a35ebe4296ede0ba6cc768961489b"
LIVE_SNAPSHOT="5bf28a63fe1ed62f67b48b91af238ca102cf328b05526675040b18f3125d44e2"
NAMES={"DAR":"基本惡能量","FIG":"基本鬥能量","FIR":"基本火能量","GRA":"基本草能量",
       "LIG":"基本雷能量","MET":"基本鋼能量","PSY":"基本超能量","WAT":"基本水能量"}
SOURCE_SET_CODES={
    "S10b":("DAR","FIG","FIR","GRA","LIG","MET","PSY","WAT"),
    "S8a":("DAR","FIG","FIR","GRA","LIG","MET","PSY","WAT"),
    "SH":("DAR","FIG","FIR","GRA"),
}
IMAGE_PATH=re.compile(r"/tw/card-img/tw[0-9]{8}\.png\Z")
URL_PATH=re.compile(r"/tw/card-search/detail/[0-9]{1,12}/\Z")

def safe(url,pattern):
    if not isinstance(url,str):return False
    try:
        u=urlsplit(url)
        return u.scheme=="https" and u.hostname=="asia.pokemon-card.com" and u.port in (None,443) \
            and u.username is None and u.password is None and not u.query and not u.fragment \
            and bool(pattern.fullmatch(u.path))
    except ValueError:return False

def check(root:Path,out:Path):
    if out.exists() or out.is_symlink():
        raise FileExistsError("refuse_existing_output")
    rows=[];errors=[];provenance=[]
    for code,nums in SOURCE_SET_CODES.items():
        for number in nums:
            source=root/"data_tc"/code/(number+".json")
            if not source.is_file() or source.is_symlink() or source.stat().st_size>200000:
                raise ValueError("source_file_unavailable_or_unsafe")
            raw=source.read_bytes()
            j=json.loads(raw)
            expected=NAMES[number]
            if j.get("set_name")!=code or j.get("number")!=number or j.get("name")!=expected \
               or not safe(j.get("img"),IMAGE_PATH) or not safe(j.get("url"),URL_PATH):
                errors.append({"set":code,"printed_number":number,"reason":"source_identity_or_url_mismatch"})
                continue
            cid=f"tcgdex_asia_{code}_{number}:zh-tw:TW"
            rows.append({
                "classification":"EXACT_SOURCE_NAME_NUMBER_MATCH_ARCHIVED_LIVE_SAMPLE_ONLY",
                "canonical_printing_id_live_sample":cid,
                "language":"zh-tw","set_id":"tcgdex:asia:set:"+code,
                "source_set_code":code,"printed_collector_number":number,
                "native_card_name":expected,"source_image_url_reference":j["img"],
                "source_detail_url":j["url"],
                "source_file":source.relative_to(root).as_posix(),
                "source_file_sha256":hashlib.sha256(raw).hexdigest(),
                "live_snapshot_sha256":LIVE_SNAPSHOT,
                "live_identity_recheck_needed":True,
                "source_artwork_rights_verified":False,
                "safe_for_public_use":False,
                "safe_for_vault_apply_without_recheck":False
            })
            provenance.append(hashlib.sha256(raw).hexdigest())
    if len(rows)!=20 or errors:
        raise ValueError("source_energy_exact_match_changed:"+repr(errors))
    raw=("".join(json.dumps(r,sort_keys=True,ensure_ascii=False,separators=(",",":"))+"\n"
                 for r in rows)).encode("utf8")
    out.mkdir(parents=True)
    (out/"tw_live_gap_source_urls.jsonl").write_bytes(raw)
    receipt={
        "classification":"PRIVATE_TW_20_EXACT_OFFICIAL_SOURCE_URL_CANDIDATES_NOT_LIVE",
        "source_repository":"type-null/PTCG-database",
        "source_commit":PIN,"live_snapshot_page1_sha256":LIVE_SNAPSHOT,
        "source_cards_exact_match":20,
        "source_distinct_image_urls":len({r["source_image_url_reference"] for r in rows}),
        "source_url_match_fields":["native_card_name","literal_source_set_code","printed_collector_number"],
        "source_files_sha256_chain":hashlib.sha256("".join(sorted(provenance)).encode("ascii")).hexdigest(),
        "source_manifest_sha256":hashlib.sha256(raw).hexdigest(),
        "source_image_binaries_downloaded":0,"new_live_image_links":0,
        "production_writes":0,"public_artwork_rights_granted":False,
        "current_live_snapshot_identities_verified":False,
        "note":"20 specific TW missing-image card IDs were read from authenticated first-page Vault sample. The source name/set/number and official image URLs now match pinned upstream. Must recheck LIVE identity and permissions before VaultSync stage/apply."
    }
    (out/"receipt.json").write_text(json.dumps(receipt,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return receipt

def selftest():
    assert safe("https://asia.pokemon-card.com/tw/card-img/tw00006605.png",IMAGE_PATH)
    assert not safe("https://evil.example/tw/card-img/tw00006605.png",IMAGE_PATH)
    assert not safe("https://asia.pokemon-card.com/tw/card-img/tw00006605.png?q=1",IMAGE_PATH)
    assert safe("https://asia.pokemon-card.com/tw/card-search/detail/6605/",URL_PATH)
    assert len({(c,n) for c,nn in SOURCE_SET_CODES.items() for n in nn})==20
    print("PASS source URL host, exact source identities, bounded set and safe rights defaults")

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--root",type=Path)
    p.add_argument("--out",type=Path)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:selftest()
    else:
        if a.root is None or a.out is None:p.error("--root and --out required")
        result=check(a.root,a.out)
        print("TW_20_PINNED_OFFICIAL_SOURCE_REFERENCES",json.dumps({
            k:result[k] for k in ("source_cards_exact_match","source_distinct_image_urls",
            "new_live_image_links","production_writes")
        },sort_keys=True))
