#!/usr/bin/env python3
"""Extract complete Korean and Traditional Chinese card metadata and original image URLs.

All data is read once from a fixed GitHub source checkout (not per-card HTTP).
Read-only private research: public image mirroring, third-party text/illustration
rights, CardScanR identities and production writes remain separately gated.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

SOURCE = "type-null/PTCG-database"
COMMIT = "18e0de2feb7a35ebe4296ede0ba6cc768961489b"
LANGS = {"ko": ("data_ko", "ko"), "zh-tw": ("data_tc", "tc")}
TOKEN = re.compile(r"^[A-Za-z0-9_.+\-]{1,64}$")
URL_KO = re.compile(r"^/data/wmimages/[A-Za-z0-9_-]{1,32}/[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,64}_[A-Za-z0-9_-]{1,12}\.(png|jpg|webp)$")
URL_TW = re.compile(r"^/tw/card-img/tw[0-9]{8}\.(png|jpg|webp)$")

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def safe_url(value, lang: str) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= 600:
        return False
    try:
        u = urlsplit(value)
        if (u.scheme != "https" or u.username or u.password
                or u.port not in (None, 443) or u.query or u.fragment):
            return False
        return (lang == "ko" and u.hostname == "cards.image.pokemonkorea.co.kr"
                and bool(URL_KO.fullmatch(u.path))) or (
                lang == "zh-tw" and u.hostname == "asia.pokemon-card.com"
                and bool(URL_TW.fullmatch(u.path)))
    except ValueError:
        return False

def acceptable_text(value, limit=500):
    return isinstance(value, str) and len(value) <= limit and not any(ord(c) < 32 and c not in "\t\n" for c in value)

def examine(root: Path):
    rows, images = [], []
    counters = defaultdict(Counter)
    seen = set()
    for lang, (dirname, source_lang) in LANGS.items():
        folder = root / dirname
        if not folder.is_dir() or folder.is_symlink():
            raise ValueError("missing_source_locale:" + dirname)
        sets = sorted(p for p in folder.iterdir() if p.is_dir())
        if len(sets) < 1: raise ValueError("empty_source_sets")
        for setdir in sets:
            # A source set directory may contain punctuation or Unicode.
            # It is obtained directly from iterdir(), so it cannot traverse
            # outside this root; reject links and extreme filenames only.
            if setdir.is_symlink() or not 1 <= len(setdir.name) <= 128:
                raise ValueError("unsafe_source_set_directory:" + setdir.name)
            files = sorted(setdir.glob("*.json"))
            if not files: continue
            counters[lang]["sets_with_card_data"] += 1
            for f in files:
                # Some official card files have punctuation in their stem.
                # glob() only yields direct children of an already verified set
                # folder, so allow those filenames while rejecting links,
                # nonfiles and unexpectedly large source blobs.
                if (f.is_symlink() or not f.is_file() or not 1 <= len(f.stem) <= 128
                        or f.stat().st_size > 1_000_000):
                    raise ValueError("unsafe_source_card_file:" + f.relative_to(root).as_posix())
                blob = f.read_bytes()
                doc = json.loads(blob)
                if not isinstance(doc, dict) or doc.get("lang") != source_lang:
                    raise ValueError("wrong_source_language")
                native_name, number, sid = doc.get("name"), doc.get("number"), doc.get("set_name")
                has_identity = (
                    acceptable_text(native_name, 300) and bool(native_name.strip())
                    and type(number) in (str,int) and acceptable_text(str(number), 80)
                    and bool(str(number).strip()) and sid == setdir.name
                )
                if not has_identity:
                    # Do not discard a source file just because upstream skipped
                    # a printed number or a set label. Retain its raw details in
                    # a separate source-only quarantine, never in the image list.
                    counters[lang]["incomplete_identity_quarantined"] += 1
                    rows.append({
                        "classification":"SOURCE_CARD_IDENTITY_INCOMPLETE_QUARANTINED",
                        "source_repo":SOURCE,"source_commit":COMMIT,"language":lang,
                        "source_path":(Path(dirname)/setdir.name/f.name).as_posix(),
                        "source_raw_sha256":sha(blob),"source_complete_record":doc,
                        "source_set_directory":setdir.name,
                        "source_identity_valid":False,"image_url_reference":None,
                        "safe_for_vault_apply":False,"artwork_rights_verified":False,
                        "safe_for_public_artwork":False,
                    })
                    counters[lang]["source_card_files"] += 1
                    continue
                print_key = doc.get("print_key")
                valid_print_key = isinstance(print_key, str) and print_key.startswith("asia:") and len(print_key) <= 200
                if not valid_print_key:
                    counters[lang]["missing_or_unrecognized_print_key"] += 1
                    print_key = None
                k = (lang, setdir.name, f.stem)
                if k in seen: raise ValueError("duplicate_source_file_identity")
                seen.add(k)
                url = doc.get("img")
                allowed = safe_url(url, lang)
                row = {
                    "classification": "PRIVATE_SOURCE_FULL_CARD_DETAILS_NO_LIVE_IMPORT",
                    "source_repo": SOURCE, "source_commit": COMMIT,
                    "language": lang, "source_path": (Path(dirname)/setdir.name/f.name).as_posix(),
                    "source_raw_sha256": sha(blob),
                    "source_print_key": print_key,
                    "source_complete_record": doc,
                    "source_set_code": sid,
                    "collector_number": str(number),
                    "collector_denominator": doc.get("set_total"),
                    "native_card_name": native_name,
                    "native_set_name": doc.get("set_full_name"),
                    "source_card_url": doc.get("url"),
                    "category": doc.get("card_type"),
                    "rarity": doc.get("rarity"),
                    "regulation": doc.get("regulation"),
                    "hp": doc.get("hp"),
                    "types": doc.get("types"),
                    "evolves_from": doc.get("evolves_from"),
                    "stage": doc.get("stage"),
                    "attacks": doc.get("attacks"),
                    "abilities": doc.get("abilities"),
                    "weakness": doc.get("weakness"),
                    "resistance": doc.get("resistance"),
                    "retreat": doc.get("retreat"),
                    "artist": doc.get("author"),
                    "pokedex_number": doc.get("pokedex_number"),
                    "flavor_text": doc.get("flavor_text"),
                    "pokemon_category": doc.get("pokemon_category"),
                    "height": doc.get("height"), "weight": doc.get("weight"),
                    "other_record_fields": sorted(set(doc) - {
                        "url","name","img","card_type","ko_id","lang","regulation","set_name",
                        "set_img","set_full_name","number","set_total","rarity","author",
                        "pokedex_number","pokemon_category","height","weight","flavor_text",
                        "stage","hp","types","attacks","abilities","weakness","resistance",
                        "retreat","print_key","card_key","types_en","evolves_from"}),
                    "image_url_reference": url if allowed else None,
                    "image_url_from_official_host": allowed,
                    "source_image_url_quarantined": bool(url) and not allowed,
                    "cardscanr_live_id_verified": False,
                    "safe_for_vault_apply": False,
                    "artwork_rights_verified": False,
                    "safe_for_public_artwork": False,
                }
                rows.append(row)
                counters[lang]["source_card_files"] += 1
                if row["hp"] is not None: counters[lang]["hp_present"] += 1
                if row["attacks"]: counters[lang]["attacks_present"] += 1
                if row["abilities"]: counters[lang]["abilities_present"] += 1
                if row["artist"]: counters[lang]["artist_present"] += 1
                if allowed and valid_print_key:
                    counters[lang]["official_image_url_refs"] += 1
                    images.append({
                        "classification": "PRIVATE_OFFICIAL_SOURCE_IMAGE_URL_REFERENCE_ONLY",
                        "language": lang, "source_set_code": sid,
                        "source_print_key": print_key, "collector_number": str(number),
                        "native_card_name": native_name, "image_url_reference": url,
                        "source_file_sha256": sha(blob),
                        "source_path": row["source_path"],
                        "canonical_printing_id": None,
                        "live_vault_identity_verified": False,
                        "image_downloaded": False,
                        "artwork_rights_verified": False,
                        "safe_for_public_artwork": False,
                    })
                elif url and not allowed: counters[lang]["unsafe_image_url_quarantined"] += 1
    if not rows: raise ValueError("zero_source_cards")
    return rows, images, {k: dict(v) for k,v in counters.items()}

def write(root: Path, out: Path, *, require_minimum=False):
    if out.exists() or out.is_symlink(): raise FileExistsError("output_must_be_new")
    rows, images, counters = examine(root)
    if require_minimum and any(counters[lang]["source_card_files"] < 1000 for lang in LANGS):
        raise ValueError("unexpectedly_short_pinned_source_archive")
    out.mkdir(parents=True)
    desc={}
    for name, entries in (("full_card_details.jsonl.gz", rows),
                          ("original_image_urls.jsonl.gz", images)):
        payload = "".join(json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",",":"))+"\n"
                          for x in entries).encode("utf8")
        compressed = gzip.compress(payload,mtime=0,compresslevel=9)
        (out/name).write_bytes(compressed)
        desc[name] = {"rows":len(entries), "sha256_raw":sha(payload),
                      "sha256_gzip":sha(compressed), "gzip_bytes":len(compressed)}
    report = {
        "classification": "PRIVATE_KO_ZHTW_COMPLETE_SOURCE_CARD_DETAILS_AND_IMAGE_URLS",
        "source_repo": SOURCE, "source_commit": COMMIT,
        "by_language": counters,
        "source_card_rows":len(rows), "source_official_image_url_refs":len(images),
        "artifacts":desc,
        "exact_live_matches":0, "new_canonical_cards":0,
        "new_live_image_links":0, "images_downloaded":0, "production_writes":0,
        "source_metadata_artwork_rights_granted":False,
        "public_artwork_approved":False,
        "note":"Source metadata includes transcribed Pokémon card text and public official URLs. Code licence does not authorize commercial republication of artwork or text. Private research only pending identity and rights checks.",
    }
    (out/"receipt.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf8")
    (out/"NOTICE.txt").write_text(
        "Read-only source metadata and official hosted image URL references. "
        "No card binaries downloaded; source work is MIT-licensed but third-party Pokemon "
        "card text, names and artwork have separate rights. Research only. "
        "Do not publish artwork without permission and exact live print validation.\n",encoding="utf8")
    return report

def self_test():
    assert safe_url("https://cards.image.pokemonkorea.co.kr/data/wmimages/MEGA/M1L/M1L_001.png","ko")
    assert safe_url("https://asia.pokemon-card.com/tw/card-img/tw00013958.png","zh-tw")
    assert not safe_url("https://asia.pokemon-card.com/tw/card-img/tw00013958.png?sig=xyz","zh-tw")
    assert not safe_url("https://evil.example/tw/card-img/tw00013958.png","zh-tw")
    with tempfile.TemporaryDirectory() as d:
        root=Path(d)
        for lang,(dirname,source_lang) in LANGS.items():
            f=root/dirname/"M1L"/"001.json"
            f.parent.mkdir(parents=True)
            row={"lang":source_lang,"name":"Test","number":"001","set_name":"M1L",
                 "print_key":"asia:M1L-1","img":(
                 "https://cards.image.pokemonkorea.co.kr/data/wmimages/MEGA/M1L/M1L_001.png"
                 if lang=="ko" else "https://asia.pokemon-card.com/tw/card-img/tw00013958.png"),
                 "attacks":[{"name":"Example","damage":"10"}],"hp":80}
            f.write_text(json.dumps(row))
        result=write(root,root/"out")
        assert result["source_card_rows"]==2 and result["source_official_image_url_refs"]==2
        assert result["new_live_image_links"] == result["production_writes"] == 0
    print("PASS full source metadata, official URL host, rights and hash guards")

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--root",type=Path)
    ap.add_argument("--output",type=Path)
    ap.add_argument("--self-test",action="store_true")
    a=ap.parse_args()
    if a.self_test: self_test()
    else:
        if not a.root or not a.output: ap.error("root and output required")
        r=write(a.root,a.output,require_minimum=True)
        print("CARD_SCANR_COMPLETE_KO_TW_DETAILS_SOURCE_PASS",
              json.dumps({"source_card_records":r["source_card_rows"],
                          "official_image_url_refs":r["source_official_image_url_refs"],
                          "by_language":r["by_language"],
                          "new_live_links":0,"production_writes":0},sort_keys=True))
