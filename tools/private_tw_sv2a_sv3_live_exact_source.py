#!/usr/bin/env python3
"""Exact native TW image URL crosswalk against 397 pinned REAL live-missing identities.

This is SOURCE-ONLY research: all image references are rights-unverified,
variant/finish evidence is unavailable in the live export, and no URLs are
inserted into the live Vault. Never download image bytes or guess URLs.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

FOLDERS={"SV2a":"SV2a F","SV3":"SV3 F"}
EXPECTED={"SV2a":187,"SV3":118}
PINNED_UPSTREAM_COMMIT="18e0de2feb7a35ebe4296ede0ba6cc768961489b"
IMG=re.compile(r"^/tw/card-img/tw[0-9]{8}\.png$")
DETAIL=re.compile(r"^/tw/card-search/detail/[0-9]{1,12}/$")
CARD_FILE=re.compile(r"^([0-9]{3})(?:-([1-9][0-9]*))?\.json$")
ALLOWED_KEYS={"canonical_printing_id","language","set_id","native_card_name","printed_collector_number","has_image_link"}
MAX_SOURCE_BYTES=240000
MAX_IDENTITY_FILE_BYTES=128000


def norm(value):
    return unicodedata.normalize("NFC", str(value).strip())


def exact_official_url(value, path_pattern):
    if not isinstance(value,str):
        return False
    try:
        u=urlsplit(value)
        return (u.scheme=="https" and u.hostname=="asia.pokemon-card.com"
                and u.port in (None,443) and not u.username and not u.password
                and not u.query and not u.fragment
                and bool(path_pattern.fullmatch(u.path)))
    except ValueError:
        return False


def load_live(root):
    info=json.loads((root/"manifest.json").read_text(encoding="utf8"))
    if info["classification"]!="PINNED_AUTHENTIC_LIVE_UNLINKED_TW_IDENTITY_ROWS_SOURCE_ONLY":
        raise ValueError("live_manifest_wrong_classification")
    records=[]
    for p in info["files"]:
        file=root/Path(p["target_file"]).name
        if (not file.is_file() or file.is_symlink() or
                file.stat().st_size>MAX_IDENTITY_FILE_BYTES):
            raise ValueError("unsafe_live_identity_file")
        text=file.read_text(encoding="utf8")
        rows=[json.loads(line) for line in text.splitlines() if line.strip()]
        if len(rows)!=p["lines"]:
            raise ValueError("pinned_live_file_linecount_changed")
        records.extend(rows)
    if len(records)!=397:
        raise ValueError("live_identity_total_wrong")
    ids=set()
    counts=Counter()
    for row in records:
        if set(row)!=ALLOWED_KEYS or row["language"]!="zh-tw" or row["has_image_link"] is not False:
            raise ValueError("unexpected_authentic_live_identity")
        cid=row["canonical_printing_id"]
        if not isinstance(cid,str) or not cid or cid in ids:
            raise ValueError("duplicate_or_invalid_live_printing")
        ids.add(cid)
        if row["set_id"] in {"tcgdex:asia:set:SV2a","tcgdex:asia:set:SV3"}:
            code=row["set_id"].rsplit(":",1)[-1]
            expected_cid=f"tcgdex_asia_{code}_{row['printed_collector_number']}:zh-tw:TW"
            if cid!=expected_cid:
                raise ValueError("live_printing_set_number_identity_not_exact")
            counts[code]+=1
    if any(counts[k]!=v for k,v in EXPECTED.items()):
        raise ValueError("pinned_target_set_counts_changed")
    return records


def load_pinned_source(root):
    records=defaultdict(list)
    errors=[]
    inspected=Counter()
    for code,folder in FOLDERS.items():
        directory=root/"data_tc"/folder
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError("missing_pinned_upstream_source_folder")
        files=sorted(directory.iterdir(),key=lambda p:p.name)
        if not 100<=len(files)<=500:
            raise ValueError("pinned_set_file_count_outside_safe_bound")
        for f in files:
            inspected[code]+=1
            match=CARD_FILE.fullmatch(f.name)
            if not match or not f.is_file() or f.is_symlink() or f.stat().st_size>MAX_SOURCE_BYTES:
                errors.append({"set_code":code,"file":f.name,"reason":"unapproved_source_file"})
                continue
            try:
                blob=f.read_bytes()
                row=json.loads(blob)
                number=row.get("number")
                if (row.get("lang")!="tc" or row.get("set_name")!=folder
                    or not isinstance(number,str) or number!=match.group(1)
                    or norm(row.get("name",""))==""
                    or row.get("print_key")!=f"asia:{code}-{int(number)}"
                    or not exact_official_url(row.get("img"),IMG)
                    or not exact_official_url(row.get("url"),DETAIL)):
                    errors.append({"set_code":code,"file":f.name,
                                   "reason":"source_set_name_printkey_or_url_mismatch"})
                    continue
                item={"set_code":code,"source_folder":folder,"printed_number":number,
                      "native_name":norm(row["name"]),"image_url":row["img"],
                      "detail_url":row["url"],"source_file":f.name,
                      "source_file_sha256":hashlib.sha256(blob).hexdigest(),
                      "source_print_key":row["print_key"],
                      "source_printed_denominator":row.get("set_total"),
                      "source_artist":row.get("author"),
                      "source_rarity":row.get("rarity")}
                records[(code,number)].append(item)
            except (ValueError,TypeError,OverflowError) as exc:
                errors.append({"set_code":code,"file":f.name,"reason":"invalid_source_json_or_identity"})
    return records,dict(inspected),errors


def run(live_root,source_root,out):
    if out.exists() or out.is_symlink():
        raise FileExistsError("output_must_be_new")
    live=load_live(live_root)
    source,inspected,errors=load_pinned_source(source_root)
    results,quarantine=[],[]
    for row in live:
        sid=row["set_id"]
        if sid not in ("tcgdex:asia:set:SV2a","tcgdex:asia:set:SV3"):
            continue
        code=sid.rsplit(":",1)[-1]
        num=row["printed_collector_number"]
        name=norm(row["native_card_name"])
        found=source.get((code,num),[])
        matches=[x for x in found if x["native_name"]==name]
        choices={x["image_url"] for x in matches}
        base={"canonical_printing_id":row["canonical_printing_id"],
              "language":"zh-tw","set_id":sid,"set_code":code,
              "native_name":name,"printed_number":num,
              "live_printing_finish_available":False,
              "live_printed_denominator_available":False,
              "artwork_rights_verified":False,
              "safe_to_publish":False,
              "safe_for_automatic_import":False}
        if len(matches)==1 and len(choices)==1:
            candidate={**base,**{"source_image_url":matches[0]["image_url"],
                "source_detail_url":matches[0]["detail_url"],
                "source_file":f"data_tc/{FOLDERS[code]}/{matches[0]['source_file']}",
                "source_file_sha256":matches[0]["source_file_sha256"],
                "source_print_key":matches[0]["source_print_key"],
                "source_printed_denominator":matches[0]["source_printed_denominator"],
                "source_rarity":matches[0]["source_rarity"],
                "source_artist":matches[0]["source_artist"],
                "source_set_name_alias":FOLDERS[code],
                "classification":"SINGLE_PROVIDER_SOURCE_IMAGE_EXACT_NAME_AND_NUMBER_RESEARCH_ONLY"}}
            results.append(candidate)
        else:
            reason=("source_number_not_found" if not found else
                    "source_name_mismatch" if not matches else
                    "source_printing_variants_ambiguous")
            quarantine.append({**base,"reason":reason,
                "provider_rows_same_number":len(found),
                "source_name_match_rows":len(matches),
                "distinct_source_urls_for_match":len(choices)})
    if len(results)+len(quarantine)!=305:
        raise ValueError("target_live_card_accounting_mismatch")
    ids=[row["canonical_printing_id"] for row in results]
    if len(ids)!=len(set(ids)):
        raise ValueError("duplicate_exact_source_candidate")
    report={
        "classification":"PRIVATE_LIVE_TW_SV2A_SV3_EXACT_SOURCE_ONLY_NOT_APPLIED",
        "source_repository":"type-null/PTCG-database",
        "source_commit":PINNED_UPSTREAM_COMMIT,
        "source_folders":FOLDERS,
        "pinned_live_missing_card_count":397,
        "target_live_missing_card_count":305,
        "source_files_inspected_by_set":inspected,
        "source_files_rejected":len(errors),
        "single_observed_source_image_url_candidates":len(results),
        "distinct_observed_source_image_urls":len({x["source_image_url"] for x in results}),
        "quarantined_due_to_variant_or_identity":len(quarantine),
        "by_set":{code:{"exact_source_candidates":sum(r["set_code"]==code for r in results),
                        "quarantined":sum(r["set_code"]==code for r in quarantine)}
                   for code in FOLDERS},
        "current_live_link_inserts":0,"new_live_canonical_cards":0,
        "artwork_files_downloaded":0,"artwork_rights_verified":False,
        "source_research_only":True,"safe_for_public_artwork":False,
        "can_import_from_this_artifact":False,
        "warning":"No live denominator/finish. Do not assign alternate source printings or publicly display this artwork. Require live source identity and rights checks before import."
    }
    out.mkdir(parents=True)
    def write(name,rows):
        content="".join(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n"
                        for x in rows)
        p=out/name;p.write_text(content,encoding="utf8")
        return {"filename":name,"rows":len(rows),
                "sha256":hashlib.sha256(p.read_bytes()).hexdigest()}
    report["files"]={
        "source_matches":write("source_matches.jsonl",results),
        "quarantine":write("quarantined_live_missing.jsonl",quarantine),
        "source_errors":write("rejected_source_files.jsonl",errors)}
    (out/"receipt.json").write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf8")
    return report


def self_test():
    assert exact_official_url("https://asia.pokemon-card.com/tw/card-img/tw00008124.png",IMG)
    assert not exact_official_url("http://asia.pokemon-card.com/tw/card-img/tw00008124.png",IMG)
    assert not exact_official_url("https://evil.example/tw/card-img/tw00008124.png",IMG)
    assert not exact_official_url("https://asia.pokemon-card.com/tw/card-img/tw00008124.png?x=1",IMG)
    assert exact_official_url("https://asia.pokemon-card.com/tw/card-search/detail/8124/",DETAIL)
    assert norm("妙蛙種子")=="妙蛙種子"
    print("OFFLINE_TW_305_SET_ALIAS_AND_SOURCE_URL_GUARDS_PASS")


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--source-root",type=Path)
    p.add_argument("--live-root",type=Path)
    p.add_argument("--out",type=Path)
    p.add_argument("--self-test",action="store_true")
    a=p.parse_args()
    if a.self_test:self_test()
    else:
        if not a.source_root or not a.live_root or not a.out:
            p.error("source-root, live-root and out required")
        doc=run(a.live_root,a.source_root,a.out)
        print("REAL_AUTHENTIC_TW_305_SINGLE_SOURCE_URL_RESULTS",json.dumps({
            k:doc[k] for k in ("target_live_missing_card_count",
                "single_observed_source_image_url_candidates",
                "quarantined_due_to_variant_or_identity",
                "distinct_observed_source_image_urls",
                "source_files_rejected","current_live_link_inserts")
        },sort_keys=True))
