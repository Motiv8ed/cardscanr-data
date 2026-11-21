#!/usr/bin/env python3
"""Source-only one-pass KO/zh-tw official URLs against SHA-pinned catalogue packs.

This is NOT a live import, a publisher licence or permission to mirror art.
Only exact language, literal set code, collector number and native card name,
with UNIQUE source and archived canonical rows, are eligible for review.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unicodedata
from urllib.parse import quote, urlsplit

SOURCE_CLASS = "PRIVATE_KO_ZHTW_COMPLETE_SOURCE_CARD_DETAILS_AND_IMAGE_URLS"
MATCH_CLASS = "PRIVATE_OFFICIAL_ARCHIVED_EXACT_MISSING_IMAGE_REFERENCE"
SOURCE_COMMIT = "18e0de2feb7a35ebe4296ede0ba6cc768961489b"
LANGS = ("ko", "zh-tw")
FIELDS = ("canonical_printing_id", "set_id", "language", "native_card_name",
          "printed_collector_number", "image_thumbnail_url", "image_display_url")
NUMBER = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
KO_IMAGE = re.compile(r"^/data/wmimages/[A-Za-z0-9_-]{1,32}/[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,64}_[A-Za-z0-9_-]{1,12}\.(png|jpg|webp)$")
TW_IMAGE = re.compile(r"^/tw/card-img/tw[0-9]{8}\.(png|jpg|webp)$")

def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()

def norm_number(s):
    if not isinstance(s, (str, int)) or isinstance(s, bool):
        return ""
    text = str(s).strip().upper().split("/", 1)[0]
    if not NUMBER.fullmatch(text): return ""
    return re.sub(r"\d+", lambda m: str(int(m.group(0))), text)

def norm_name(s):
    return " ".join(unicodedata.normalize("NFC", s).split()) if isinstance(s, str) else ""

def safe_url(value, lang):
    if not isinstance(value, str) or len(value) > 600: return False
    try:
        u = urlsplit(value)
        if u.scheme != "https" or u.username or u.password or u.port not in (None, 443) or u.query or u.fragment:
            return False
        return (lang == "ko" and u.hostname == "cards.image.pokemonkorea.co.kr"
                and bool(KO_IMAGE.fullmatch(u.path))) or (
                lang == "zh-tw" and u.hostname == "asia.pokemon-card.com"
                and bool(TW_IMAGE.fullmatch(u.path)))
    except ValueError:
        return False

def read_archive(source: Path):
    rec = json.loads((source / "receipt.json").read_text(encoding="utf8"))
    if (rec.get("classification") != SOURCE_CLASS
            or rec.get("source_commit") != SOURCE_COMMIT
            or rec.get("source_card_rows") != 34906
            or rec.get("source_official_image_url_refs") != 32230
            or rec.get("new_live_image_links") != 0
            or rec.get("production_writes") != 0
            or rec.get("source_metadata_artwork_rights_granted") is not False):
        raise ValueError("source_receipt_drift")
    data = {}
    for filename, target in (("full_card_details.jsonl.gz", "details"),
                             ("original_image_urls.jsonl.gz", "images")):
        blob = (source / filename).read_bytes()
        expected = rec["artifacts"][filename]
        if sha(blob) != expected["sha256_gzip"]:
            raise ValueError("compressed_source_sha_mismatch:" + filename)
        raw = gzip.decompress(blob)
        if sha(raw) != expected["sha256_raw"]:
            raise ValueError("raw_source_sha_mismatch:" + filename)
        rows = [json.loads(x) for x in raw.splitlines()]
        if len(rows) != expected["rows"]:
            raise ValueError("source_record_count_mismatch:" + filename)
        data[target] = rows
    return rec, data["details"], data["images"]

def build_source(details, images):
    valid = {}
    reasons = Counter()
    for row in details:
        if row.get("language") not in LANGS or row.get("safe_for_vault_apply") is not False:
            raise ValueError("invalid_source_detail")
        if row.get("classification") != "PRIVATE_SOURCE_FULL_CARD_DETAILS_NO_LIVE_IMPORT":
            reasons["quarantined_incomplete_source_identity"] += 1
            continue
        path = row.get("source_path")
        if not isinstance(path, str) or path in valid:
            raise ValueError("duplicate_or_invalid_source_detail_path")
        valid[path] = row
    indexed = defaultdict(list)
    picture_seen = set()
    for item in images:
        if (item.get("classification") != "PRIVATE_OFFICIAL_SOURCE_IMAGE_URL_REFERENCE_ONLY"
                or item.get("language") not in LANGS
                or item.get("live_vault_identity_verified") is not False
                or item.get("artwork_rights_verified") is not False):
            raise ValueError("untrusted_picture_source_row")
        path, art = item.get("source_path"), item.get("image_url_reference")
        original = valid.get(path)
        if original is None or not safe_url(art, item["language"]):
            raise ValueError("picture_has_no_valid_source_identity")
        if (original.get("source_raw_sha256") != item.get("source_file_sha256")
                or original.get("native_card_name") != item.get("native_card_name")
                or original.get("source_set_code") != item.get("source_set_code")
                or original.get("source_print_key") != item.get("source_print_key")
                or original.get("image_url_reference") != art):
            raise ValueError("picture_does_not_match_original_source_file")
        if path in picture_seen: raise ValueError("duplicate_source_picture_path")
        picture_seen.add(path)
        code = original.get("source_set_code")
        if not isinstance(code, str) or not code or len(code) > 128:
            reasons["invalid_source_set_code"] += 1
            continue
        key = (item["language"], "tcgdex:asia:set:" + code,
               norm_number(item.get("collector_number")), norm_name(item.get("native_card_name")))
        if not key[2] or not key[3]:
            reasons["invalid_source_number_or_name"] += 1
            continue
        indexed[key].append(item)
    return indexed, reasons

def compare(pack_rows, source_index):
    indexed = defaultdict(list)
    for row in pack_rows:
        if row["language"] not in LANGS: raise ValueError("invalid_pack_language")
        key = (row["language"], row["set_id"], norm_number(row["printed_collector_number"]),
               norm_name(row["native_card_name"]))
        if key[2] and key[3]:
            indexed[key].append(row)
    reasons = Counter()
    matched = []
    for key, sources in source_index.items():
        candidates = indexed.get(key, [])
        if len(sources) != 1:
            reasons["ambiguous_multiple_source_printings"] += len(sources)
            continue
        if len(candidates) == 0:
            reasons["no_exact_archived_pack_identity"] += 1
            continue
        if len(candidates) != 1:
            reasons["ambiguous_multiple_archived_printings"] += 1
            continue
        source, target = sources[0], candidates[0]
        if target.get("image_thumbnail_url") or target.get("image_display_url"):
            outcome = "exact_card_already_has_archive_image"
        else:
            outcome = "exact_archived_missing_image_url_candidate"
        reasons[outcome] += 1
        matched.append({
            "classification": MATCH_CLASS if outcome.startswith("exact_archived_missing") else "PRIVATE_ARCHIVED_ALREADY_HAS_IMAGE",
            "language": key[0],
            "set_id": key[1],
            "normalized_collector_number": key[2],
            "native_card_name": source["native_card_name"],
            "canonical_printing_id_archived": target["canonical_printing_id"],
            "original_source_path": source["source_path"],
            "original_source_file_sha256": source["source_file_sha256"],
            "source_print_key": source["source_print_key"],
            "source_image_url_reference": source["image_url_reference"],
            "live_vault_id_checked": False,
            "source_image_rights_granted": False,
            "safe_for_vault_apply": False,
            "public_artwork_allowed": False,
        })
    return matched, reasons

def run(source, output):
    if output.exists() or output.is_symlink():
        raise FileExistsError("output_must_be_new")
    rec, details, images = read_archive(source)
    indexed, issues = build_source(details, images)
    from tools.preflight_multilingual_crossrelease import CANARY, MAX_GZIP, get, inspect_pack
    from tools.preflight_legacy_korean_pack import pick
    man = json.loads(get(CANARY, 2_000_000))
    allrows = []
    sha_packs = {}
    with tempfile.TemporaryDirectory(prefix="cardscanr-source-only-ko-tw-") as t:
        for lang in LANGS:
            desc = pick(man, lang)
            raw = get(desc["compressedDatabaseUrl"], MAX_GZIP)
            path = Path(t) / (lang + ".sqlite")
            check, _, _, _, _, _ = inspect_pack(desc, raw, path)
            if check["sqlite_quick_check"] != "ok":
                raise ValueError("archived_pack_integrity_error")
            sha_packs[lang] = desc["sha256"]
            uri = "file:" + quote(path.resolve().as_posix(), safe="/:") + "?mode=ro&immutable=1"
            with sqlite3.connect(uri, uri=True) as db:
                db.execute("PRAGMA query_only=ON")
                cols = {r[1] for r in db.execute("PRAGMA table_info(cards)")}
                if not set(FIELDS).issubset(cols):
                    raise ValueError("archived_pack_columns_drift")
                for rawrow in db.execute("SELECT " + ",".join(FIELDS) + " FROM cards WHERE language=?", (lang,)):
                    allrows.append(dict(zip(FIELDS, rawrow)))
    hits, reasons = compare(allrows, indexed)
    counted = sum(reasons.values()) + issues.get("invalid_source_set_code", 0) + issues.get("invalid_source_number_or_name", 0)
    if counted != len(images):
        raise ValueError("source_picture_reference_accounting_mismatch")
    output.mkdir(parents=True)
    data = "".join(json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                   for x in sorted(hits, key=lambda x: (x["classification"], x["language"],
                         x["canonical_printing_id_archived"]))).encode()
    (output / "exact_archived_image_crosswalk.jsonl.gz").write_bytes(gzip.compress(data, mtime=0))
    report = {
        "classification": "PRIVATE_OFFICIAL_KO_TW_EXACT_ARCHIVED_SOURCE_CROSSWALK_NOT_LIVE",
        "source_commit": SOURCE_COMMIT,
        "source_original_card_records": len(details),
        "source_original_official_image_urls": len(images),
        "source_identity_quarantine": dict(issues),
        "language_pack_sha256": sha_packs,
        "archived_cards_examined": len(allrows),
        "matched_records": len(hits),
        "classification_counts": dict(sorted(reasons.items())),
        "exact_archived_missing_image_url_candidates": reasons["exact_archived_missing_image_url_candidate"],
        "already_archive_image_covered": reasons["exact_card_already_has_archive_image"],
        "image_refs_matched_to_exact_live_vault_ids": 0,
        "new_live_image_links": 0,
        "image_binaries_downloaded": 0,
        "production_writes": 0,
        "image_rights_granted": False,
        "safe_for_public_artwork": False,
        "crosswalk_uncompressed_sha256": sha(data),
        "crosswalk_compressed_sha256": sha((output / "exact_archived_image_crosswalk.jsonl.gz").read_bytes()),
        "note": "Only archived SHA-verified identity agreement. Current PC Vault identities, print variant identity and image usage permissions still require independent clearance.",
    }
    (output / "receipt.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf8")
    return report

def selftest():
    image = "https://asia.pokemon-card.com/tw/card-img/tw00013958.png"
    item = {"classification":"PRIVATE_OFFICIAL_SOURCE_IMAGE_URL_REFERENCE_ONLY",
        "language":"zh-tw","source_path":"data_tc/M1L/001.json",
        "source_set_code":"M1L","collector_number":"001",
        "native_card_name":"妙蛙種子","image_url_reference":image,
        "source_file_sha256":"a"*64,"source_print_key":"asia:M1L-1",
        "live_vault_identity_verified":False,"artwork_rights_verified":False}
    detail = {"classification":"PRIVATE_SOURCE_FULL_CARD_DETAILS_NO_LIVE_IMPORT",
        "language":"zh-tw","safe_for_vault_apply":False,"source_path":item["source_path"],
        "source_raw_sha256":"a"*64,"source_set_code":"M1L",
        "source_print_key":"asia:M1L-1","native_card_name":"妙蛙種子",
        "image_url_reference":image}
    source, issues = build_source([detail], [item])
    assert not issues and len(source) == 1
    archived = [{"language":"zh-tw","set_id":"tcgdex:asia:set:M1L",
        "native_card_name":"妙蛙種子","printed_collector_number":"1",
        "canonical_printing_id":"a", "image_thumbnail_url":None, "image_display_url":None}]
    hits, reasons = compare(archived, source)
    assert reasons["exact_archived_missing_image_url_candidate"] == 1
    assert not hits[0]["safe_for_vault_apply"]
    hits, reasons = compare(archived * 2, source)
    assert reasons["ambiguous_multiple_archived_printings"] == 1
    other = dict(item, image_url_reference="https://evil.example/img.png")
    try: build_source([detail], [other])
    except ValueError: pass
    else: raise AssertionError("untrusted_picture_url_accepted")
    assert norm_number("001/090") == "1"
    print("PASS exact source-to-archive mapping, ambiguity and URL host guards")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test: selftest()
    else:
        if not a.source or not a.output:
            p.error("--source and --output required")
        result = run(a.source, a.output)
        print("CARD_SCANR_KO_TW_ARCHIVED_SOURCE_RECONCILED",
              json.dumps({"source_urls":result["source_original_official_image_urls"],
                          "matched":result["matched_records"],
                          "archive_missing_image_candidates":result["exact_archived_missing_image_url_candidates"],
                          "already_archive_covered":result["already_archive_image_covered"],
                          "live_links_added":0}, sort_keys=True))
