#!/usr/bin/env python3
"""Archive pinned multi-language PTCG research metadata and original image URLs.

The archive is not a canonical CardScanR import; URL provenance and artwork
copyright permission must be reviewed before any public image is displayed.
It does not download images, touch the owner's PC, or change customer data.
"""
from __future__ import annotations
import argparse, gzip, hashlib, json, re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

UPSTREAM_COMMIT="18e0de2feb7a35ebe4296ede0ba6cc768961489b"
SOURCE_REPO="type-null/PTCG-database"
SOURCE_LICENSE="MIT for source repository; third-party card art/text rights separate"
LANGUAGE_FOLDERS={
    "jp":"ja", "tc":"zh-tw", "ko":"ko", "en":"en", "sc":"zh-cn",
    "de":"de","fr":"fr","es":"es","it":"it","pt":"pt","pocket":"pocket",
}
PUBLISHED_COUNTS={
    "de":20247,"en":21086,"es":15510,"fr":22169,
    "it":15741,"jp":24039,"ko":20323,"pocket":3879,
    "pt":13907,"sc":877,"tc":14583,
}
EXPECTED_SUM=172361
LIMIT_FILE_BYTES=240_000
FIELDS=("name","card_type","lang","regulation","set_name","set_full_name",
    "number","set_total","rarity","author","pokedex_number","pokemon_category",
    "height","weight","flavor_text","stage","hp","types","attacks","abilities",
    "weakness","resistance","retreat","types_en","tags","effects","rule_box",
    "set_code","series","print_key","card_key","jp_id","ko_id","rarity",
    "trainer_type","subtype","effect","energy","sources","date","level")

def safe_url(raw):
    if not isinstance(raw,str) or len(raw)>1500:
        return None
    try:
        p=urlsplit(raw)
        if (p.scheme!="https" or not p.hostname or p.username or p.password or
                p.port not in (None,443) or p.query or p.fragment):
            return None
        return raw
    except ValueError:
        return None

def classify(relative:Path):
    if len(relative.parts)<3 or not relative.parts[0].startswith("data_") or relative.suffix!=".json":
        raise ValueError("invalid_source_data_path")
    lang=relative.parts[0][5:]
    if lang not in LANGUAGE_FOLDERS:raise ValueError("unsupported_data_language")
    if any(part in (".","..") or not part for part in relative.parts):
        raise ValueError("invalid_source_file_part")
    return lang

def compact_card(rec,rel,lang):
    if not isinstance(rec,dict) or not isinstance(rec.get("name"),str):
        raise ValueError("source_card_no_name")
    fields={k:rec[k] for k in FIELDS if k in rec}
    img=safe_url(rec.get("img"))
    page=safe_url(rec.get("url"))
    host=urlsplit(img).hostname if img else None
    return {
        "provider":"type-null/PTCG-database",
        "source_file":rel.as_posix(),
        "provider_language":lang,
        "cardscanr_language_hint":LANGUAGE_FOLDERS[lang],
        "source_set_name":rec.get("set_name"),
        "source_print_key":rec.get("print_key"),
        "source_card_key":rec.get("card_key"),
        "source_page_url":page,
        "source_image_url_reference":img,
        "source_image_host":host,
        "details":fields,
        "canonical_printing_id":None,
        "exact_cardscanr_identity_verified":False,
        "image_file_downloaded":False,
        "image_publication_permission_verified":False,
    }

def save_gzip(path,rows):
    sha=hashlib.sha256()
    count=0
    with path.open("xb") as fb:
        with gzip.GzipFile(fileobj=fb,mode="wb",mtime=0) as g:
            for record in rows:
                b=(json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode()
                sha.update(b); g.write(b); count+=1
    return {"rows":count,"sha256_uncompressed":sha.hexdigest(),
            "sha256_compressed":hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes_compressed":path.stat().st_size}

def collect(root:Path,out:Path):
    if out.exists() or out.is_symlink():
        raise FileExistsError("evidence_directory_must_be_new")
    if not root.is_dir():raise ValueError("source_checkout_missing")
    counts=Counter()
    host_counts=Counter()
    entries=[]
    skipped=[]
    hashed_sources=hashlib.sha256()
    files_seen=set()
    for folder in sorted(root.glob("data_*")):
        if not folder.is_dir() or folder.is_symlink(): continue
        lang=folder.name[5:]
        if lang not in LANGUAGE_FOLDERS:continue
        for path in sorted(folder.rglob("*.json")):
            relative=path.relative_to(root)
            if path.is_symlink() or not path.is_file() or path.stat().st_size>LIMIT_FILE_BYTES:
                skipped.append({"path_sha256":hashlib.sha256(relative.as_posix().encode()).hexdigest(),
                                "reason":"unsafe_or_large_file"})
                continue
            lang=classify(relative)
            try:
                raw=path.read_bytes()
                record=json.loads(raw)
                item=compact_card(record,relative,lang)
            except (ValueError,TypeError,UnicodeError,json.JSONDecodeError) as exc:
                skipped.append({"path_sha256":hashlib.sha256(relative.as_posix().encode()).hexdigest(),
                                "reason":type(exc).__name__})
                continue
            if item["source_file"] in files_seen:
                raise ValueError("duplicate_source_file")
            files_seen.add(item["source_file"])
            hashed_sources.update(relative.as_posix().encode()+b"\0"+hashlib.sha256(raw).digest())
            counts[lang]+=1
            if item["source_image_host"]: host_counts[item["source_image_host"]]+=1
            entries.append(item)
    if len(entries)!=EXPECTED_SUM or dict(counts)!=PUBLISHED_COUNTS:
        raise ValueError("upstream_readme_counts_or_checkout_drift:"+json.dumps({
            "total":len(entries),"expected":EXPECTED_SUM,
            "count_by_language":dict(counts),"skipped":len(skipped)
        },sort_keys=True))
    out.mkdir(parents=True)
    files={
       "source_cards_11_languages.jsonl.gz":save_gzip(out/"source_cards_11_languages.jsonl.gz",entries),
       "source_quarantine.jsonl.gz":save_gzip(out/"source_quarantine.jsonl.gz",skipped)
    }
    proof={
        "classification":"PRIVATE_THIRD_PARTY_RESEARCH_ONLY",
        "upstream_repository":SOURCE_REPO,"upstream_commit":UPSTREAM_COMMIT,
        "source_catalogue_file_digest_sha256":hashed_sources.hexdigest(),
        "generated_utc":datetime.now(timezone.utc).isoformat(),
        "total_provider_card_records":len(entries),
        "source_language_counts":dict(sorted(counts.items())),
        "valid_https_image_url_references":sum(host_counts.values()),
        "image_url_hosts":dict(sorted(host_counts.items())),
        "unreadable_or_quarantined":len(skipped),
        "sha256_manifest":files,
        "source_license":SOURCE_LICENSE,
        "images_downloaded":0,"cardscanr_new_canonical_cards":0,
        "cardscanr_new_verified_image_links":0,
        "vault_writes":0,"production_writes":0,
        "all_source_identity_matches_provisional":True,
        "image_rights_unverified":True,
        "attribution":"type-null/PTCG-database, https://github.com/type-null/PTCG-database"
    }
    (out/"receipt.json").write_text(json.dumps(proof,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf8")
    (out/"NOTICE.txt").write_text(
       "Research metadata sourced from https://github.com/type-null/PTCG-database.\n"
       "Repository source code license MIT. Card artwork/text may have separate copyrights.\n"
       "No artwork files downloaded or publication rights granted. CardScanR records unchanged.\n",
       encoding="utf8")
    return proof

def self_test():
    assert safe_url("https://cards.image.pokemonkorea.co.kr/data/image.png")
    assert safe_url("http://example.com/test.png") is None
    assert safe_url("https://user:pass@example.com/a.png") is None
    assert safe_url("https://example.com/a.png?token=x") is None
    c=compact_card({"name":"Test","img":"https://example.com/x.png",
       "set_name":"SV","number":"001"},"data_tc/SV/001.json" if False else Path("data_tc/SV/001.json"),"tc")
    assert c["cardscanr_language_hint"]=="zh-tw"
    assert not c["exact_cardscanr_identity_verified"]
    assert not c["image_publication_permission_verified"]
    print("PASS source-only archive guards")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--source-root",type=Path)
    ap.add_argument("--output",type=Path)
    ap.add_argument("--self-test",action="store_true")
    args=ap.parse_args()
    if args.self_test:self_test();return
    if not args.source_root or not args.output:ap.error("--source-root and --output required")
    proof=collect(args.source_root,args.output)
    print(json.dumps({"result":"PRIVATE_MULTI_LANGUAGE_SOURCE_ONLY",
       "cards":proof["total_provider_card_records"],
       "image_url_references":proof["valid_https_image_url_references"],
       "languages":len(proof["source_language_counts"]),
       "live_card_additions":0,"image_downloads":0},sort_keys=True))
if __name__=="__main__":main()
