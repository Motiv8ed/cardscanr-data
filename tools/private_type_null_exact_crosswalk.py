#!/usr/bin/env python3
"""Strict read-only crosswalk: pinned official-source image URLs vs CardScanR packs.

No fuzzy or translated identity joins. A candidate requires exactly one source
record and exactly one CardScanR printing with the same language, exact TCGdex
Asia set ID, normalized collector number, and normalized native card name.
This is research for future VaultSync import, NOT a public artwork licence.
"""
from __future__ import annotations
import argparse, gzip, hashlib, json, re, sqlite3, tempfile
from collections import Counter,defaultdict
from pathlib import Path
from urllib.parse import quote,urlsplit

from tools.preflight_multilingual_crossrelease import CANARY,MAX_GZIP,get,inspect_pack
from tools.preflight_legacy_korean_pack import pick
from tools.private_image_identity_crosswalk import _number
from tools.private_shard_contract import normalize_website_search
from tools.private_type_null_metadata_archive import safe_url

LANGS={"jp":"ja","tc":"zh-tw","ko":"ko","sc":"zh-cn","en":"en"}
OFFICIAL={
    "ja":{"www.pokemon-card.com"},
    "zh-tw":{"asia.pokemon-card.com"},
    "ko":{"cards.image.pokemonkorea.co.kr"},
    "zh-cn":{"www.pokemon.cn"},
    "en":{"images.pokemontcg.io"},
}
COLS=("canonical_printing_id","set_id","language","printed_collector_number",
      "native_card_name","image_thumbnail_url","image_display_url")

def key_for_pack(row):
    sid=row["set_id"]
    if not isinstance(sid,str) or not sid.startswith("tcgdex:asia:set:"):
        return None
    code=sid.removeprefix("tcgdex:asia:set:")
    num=_number(row["printed_collector_number"])
    name=normalize_website_search(row["native_card_name"] or "")
    if not code or not num or not name:
        return None
    return (row["language"],code,num,name)

def key_for_source(doc, language):
    code=doc.get("set_name")
    num=doc.get("number")
    name=doc.get("name")
    printkey=doc.get("print_key")
    if (not isinstance(code,str) or not isinstance(num,str) or
            not isinstance(name,str) or not isinstance(printkey,str)):
        return None
    rawnum=_number(num)
    if not rawnum or not printkey.startswith("asia:"+code+"-"):
        return None
    source_num=_number(printkey[len("asia:"+code+"-"):])
    if rawnum!=source_num:
        return None
    normalized=normalize_website_search(name)
    return (language,code,rawnum,normalized) if normalized else None

def compare_rows(pack,source,language):
    packs=defaultdict(list)
    sources=defaultdict(list)
    for r in pack:
        key=key_for_pack(r)
        if key and key[0]==language: packs[key].append(r)
    for row in source:
        key=key_for_source(row,language)
        if key: sources[key].append(row)
    matches=[]
    stats=Counter()
    for key, candidates in sources.items():
        found=packs.get(key,[])
        if len(found)!=1 or len(candidates)!=1:
            stats["ambiguous" if found or len(candidates)>1 else "unmatched"]+=1
            continue
        c=found[0]; s=candidates[0]
        image=safe_url(s.get("img"))
        domain=urlsplit(image).hostname if image else None
        if domain not in OFFICIAL.get(language,set()):
            stats["image_domain_not_official_allowed"]+=1
            continue
        if c["image_thumbnail_url"] or c["image_display_url"]:
            stats["exact_already_had_pack_image_reference"]+=1
            continue
        matches.append({
            "language":language,
            "set_id":c["set_id"],
            "canonical_printing_id":c["canonical_printing_id"],
            "source_print_key":s["print_key"],
            "native_name":s["name"],
            "printed_collector_number":s["number"],
            "source_image_url_reference":image,
            "source_url":safe_url(s.get("url")),
            "source_image_host":domain,
            "source_file":s["_source_file"],
            "status":"exact_unique_pack_identity_source_url_candidate",
            "existing_pack_image_was_missing":True,
            "live_vault_image_missing_not_yet_verified":True,
            "artwork_rights_verified":False,
            "image_downloaded":False,
            "public_release_allowed":False,
        })
        stats["exact_missing_pack_image_source_url_candidate"]+=1
    return matches,stats

def crosswalk(root:Path,output:Path):
    if output.exists() or output.is_symlink():
        raise FileExistsError("output_must_be_new")
    manifest=json.loads(get(CANARY,2_000_000))
    proofs=[]
    results=[]
    by_lang={}
    with tempfile.TemporaryDirectory(prefix="cardscanr-type-null-crosswalk-") as td:
        for folder_lang,lang in LANGS.items():
            descriptor=pick(manifest,lang)
            zipped=get(descriptor["compressedDatabaseUrl"],MAX_GZIP)
            pack_file=Path(td)/(lang+".sqlite")
            inspect_pack(descriptor,zipped,pack_file)
            uri="file:"+quote(pack_file.resolve().as_posix(),safe="/:")+"?mode=ro&immutable=1"
            with sqlite3.connect(uri,uri=True) as db:
                db.execute("PRAGMA query_only=ON")
                pack=[dict(zip(COLS,row)) for row in db.execute(
                    "SELECT "+",".join(COLS)+" FROM cards")]
            source=[]
            for filename in sorted((root/("data_"+folder_lang)).rglob("*.json")):
                if filename.is_symlink() or filename.stat().st_size>240_000:
                    raise ValueError("source_file_unsafe")
                record=json.loads(filename.read_bytes())
                if not isinstance(record,dict):
                    continue
                record["_source_file"]=filename.relative_to(root).as_posix()
                source.append(record)
            matched,stats=compare_rows(pack,source,lang)
            results.extend(matched)
            by_lang[lang]={
                "source_cards":len(source),
                "canonical_pack_cards":len(pack),
                "status":dict(sorted(stats.items())),
                "exact_missing_pack_reference_candidates":len(matched),
                "pack_sha256_verified":descriptor["sha256"],
            }
            proofs.append({"lang":lang,"sha256":descriptor["sha256"]})
    keys=[x["canonical_printing_id"] for x in results]
    if len(keys)!=len(set(keys)):
        raise ValueError("duplicate_cross_language_canonical_matches")
    output.mkdir(parents=True)
    payload="".join(json.dumps(x,ensure_ascii=False,sort_keys=True)+"\n" for x in results).encode()
    with gzip.GzipFile(filename=str(output/"exact_source_url_candidates.jsonl.gz"),mode="wb",mtime=0) as f:
        f.write(payload)
    report={
        "classification":"PRIVATE_EXACT_SOURCE_URL_RESEARCH_ONLY",
        "source_repo":"type-null/PTCG-database",
        "source_commit":"18e0de2feb7a35ebe4296ede0ba6cc768961489b",
        "pack_provenance_sha256":proofs,
        "by_language":by_lang,
        "unique_matched_missing_pack_image_reference_candidates":len(results),
        "candidate_uncompressed_sha256":hashlib.sha256(payload).hexdigest(),
        "candidate_bytes":len(payload),
        "candidate_compressed_sha256":hashlib.sha256(
            (output/"exact_source_url_candidates.jsonl.gz").read_bytes()
        ).hexdigest(),
        "artwork_rights_granted":False,
        "source_url_is_not_image_download_or_license":True,
        "live_vault_gap_still_requires_direct_verification":True,
        "exact_canonical_card_insertions":0,
        "vault_writes":0,"production_writes":0,
        "artwork_downloaded":0,
    }
    (output/"receipt.json").write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
    return report

def self_test():
    p={"canonical_printing_id":"print001","set_id":"tcgdex:asia:set:AC1D",
       "language":"zh-tw","printed_collector_number":"001/157",
       "native_card_name":"蔓藤怪","image_thumbnail_url":None,"image_display_url":None}
    s={"set_name":"AC1D","number":"001","name":"蔓藤怪",
       "print_key":"asia:AC1D-1","img":"https://asia.pokemon-card.com/tw/card-img/tw00004285.png",
       "url":"https://asia.pokemon-card.com/tw/card-search/detail/4285/",
       "_source_file":"data_tc/AC1D/001.json"}
    rows,counts=compare_rows([p],[s],"zh-tw")
    assert len(rows)==1 and counts["exact_missing_pack_image_source_url_candidate"]==1
    assert rows[0]["canonical_printing_id"]=="print001"
    assert rows[0]["artwork_rights_verified"] is False
    assert not compare_rows([p,p],[s],"zh-tw")[0]
    assert not compare_rows([p],[s,s],"zh-tw")[0]
    assert not compare_rows([p],[{**s,"name":"Not the same card"}],"zh-tw")[0]
    assert not compare_rows([p],[{**s,"img":"https://evil.example/a.png"}],"zh-tw")[0]
    print("PASS: exact unique native-name/set/number and official-image-host guards")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--source-root",type=Path)
    ap.add_argument("--output",type=Path)
    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()
    if args.self_test:self_test();return
    if not args.source_root or not args.output:ap.error("Source root/output required")
    doc=crosswalk(args.source_root,args.output)
    print(json.dumps({"result":"EXACT_PRIVATE_CANDIDATE_AUDIT",
         "source_url_candidates":doc["unique_matched_missing_pack_image_reference_candidates"],
         "by_language":doc["by_language"],"vault_writes":0,
         "rights_granted":False},ensure_ascii=False,sort_keys=True))
if __name__=="__main__":main()
