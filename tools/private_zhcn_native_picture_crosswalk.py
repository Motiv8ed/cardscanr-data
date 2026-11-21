#!/usr/bin/env python3
"""One-pass, strictly private native Chinese / CardCorpus picture evidence join.

No network access, image downloads, public image rights, canonical rewrites or
vault writes. Source evidence is not authorization to display copyrighted art.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import unicodedata

NATIVE_SOURCE = "zsjy947/PTCG-card-data"
NATIVE_COMMIT = "7f03f38f4538f01d8ee03121aa91aad3b0ae1149"
CORPUS_COMMIT = "c4f0b415d1dd3d4fbaadb8c32a0a591126746ed6"
PIC_BASE = "https://img.cardcorpus.com/m/"
PIC_PATTERN = re.compile(r"^[0-9a-f]{32}$")

def code(s):
    return "".join(x for x in s.strip().upper() if x.isalnum()) if isinstance(s, str) else ""

def number(s):
    if not isinstance(s, str): return ""
    v = s.strip().upper().split("/", 1)[0].lstrip("0")
    return (v or "0") if len(v) <= 50 else ""

def name(s):
    return unicodedata.normalize("NFC", s).strip() if isinstance(s, str) else ""

def verified_jsonl(directory, payload_name, sha_field):
    receipt = json.loads((directory / "receipt.json").read_text(encoding="utf8"))
    payload = (directory / payload_name).read_bytes()
    if hashlib.sha256(payload).hexdigest() != receipt.get(sha_field):
        raise ValueError("pinned_artifact_payload_sha_mismatch:" + payload_name)
    rows = [json.loads(x) for x in payload.splitlines()]
    return receipt, rows

def corpus_sets(root):
    folder = root / "v1/sets/zh-hans"
    if not folder.is_dir(): raise ValueError("missing_pinned_corpus_sets")
    mapping = defaultdict(set)
    for path in sorted(folder.glob("*.json")):
        if path.is_symlink() or path.stat().st_size > 4_000_000:
            raise ValueError("unsafe_corpus_set")
        obj = json.loads(path.read_text(encoding="utf8"))
        if obj.get("catalog") != "zh-hans" or obj.get("language") != "zh-Hans":
            raise ValueError("corpus_language_drift")
        if obj.get("id") != "zh-hans/" + path.stem:
            raise ValueError("corpus_set_id_drift")
        if code(obj.get("code")): mapping[obj["id"]].add(code(obj["code"]))
    return {sid: next(iter(codes)) for sid, codes in mapping.items() if len(codes) == 1}

def join(native_rows, provisional_rows, mappings):
    by_print = defaultdict(list)
    for row in native_rows:
        if (row.get("language") != "zh-cn" or
                row.get("image_url") is not None or
                row.get("image_rights_granted") is not False or
                row.get("safe_for_vault_apply") is not False):
            raise ValueError("untrusted_native_row")
        if row.get("source_identity_unique") is True and row.get("source_file_set_code_verified") is True:
            k = (code(row.get("source_set_code")), number(row.get("source_collector_number")))
            if all(k): by_print[k].append(row)
    # Multiple source rows at the same set/number, even with different
    # names, are not a safe identity match. Don't silently select one.
    by_canonical = Counter(row.get("archived_canonical_printing_id") for row in provisional_rows)
    by_picture = Counter(row.get("source_picture_id") for row in provisional_rows)
    reasons = Counter()
    output = []
    for p in provisional_rows:
        if (p.get("classification") != "PROVISIONAL_SET_CODE_COLLECTOR_NUMBER_ONLY" or
                p.get("safe_for_vault_apply") is not False or
                p.get("image_rights_cleared") is not False or
                p.get("live_vault_identity_verified") is not False):
            raise ValueError("untrusted_provisional_row")
        photo = p.get("source_picture_id")
        if not isinstance(photo, str) or not PIC_PATTERN.fullmatch(photo) or p.get("source_picture_url_reference") != PIC_BASE + photo:
            raise ValueError("source_picture_template_or_id_drift")
        canonical = p.get("archived_canonical_printing_id")
        if not isinstance(canonical, str) or not canonical: raise ValueError("missing_archived_canonical_id")
        source_set = p.get("source_set_id")
        setcode = mappings.get(source_set)
        key = (setcode or "", number(p.get("printed_collector_number")))
        matches = by_print.get(key, []) if all(key) else []
        reason = "NATIVE_NAME_AND_NUMBER_CORROBORATED_VARIANT_UNVERIFIED"
        if not setcode: reason = "NO_UNIQUE_SOURCE_SET_CODE"
        elif not matches: reason = "NO_INDEPENDENT_NATIVE_SET_NUMBER"
        elif len(matches) != 1: reason = "MULTIPLE_NATIVE_ROWS_SAME_SET_NUMBER"
        elif name(matches[0].get("native_chinese_name")) != name(p.get("archived_native_chinese_name")):
            reason = "NATIVE_CHINESE_NAME_DISAGREEMENT"
        elif by_canonical[canonical] != 1 or by_picture[photo] != 1:
            reason = "AMBIGUOUS_CANONICAL_OR_IMAGE_REFERENCE"
        native = matches[0] if len(matches) == 1 else None
        row = {
            "classification": reason,
            "archived_canonical_printing_id": canonical,
            "source_set_id": source_set,
            "normalized_source_set_code": setcode,
            "printed_collector_number": p.get("printed_collector_number"),
            "archived_native_chinese_name": p.get("archived_native_chinese_name"),
            "independent_native_name": native.get("native_chinese_name") if native else None,
            "independent_native_rarity": native.get("source_rarity") if native else None,
            "source_picture_id": photo,
            "source_picture_url_reference": p["source_picture_url_reference"],
            "independent_native_identity_corroborated": reason == "NATIVE_NAME_AND_NUMBER_CORROBORATED_VARIANT_UNVERIFIED",
            "finish_or_variant_verified": False,
            "live_vault_id_verified": False,
            "artwork_rights_granted": False,
            "safe_for_vault_apply": False,
            "safe_for_publication": False,
        }
        reasons[reason] += 1
        output.append(row)
    output.sort(key=lambda r: (r["classification"], r["archived_canonical_printing_id"], r["source_picture_id"]))
    if sum(reasons.values()) != len(provisional_rows): raise AssertionError("lost_provisional_candidates")
    return output, dict(sorted(reasons.items()))

def run(native, provisional, corpus, output):
    if output.exists() or output.is_symlink(): raise FileExistsError("output_must_be_new")
    nr, nrows = verified_jsonl(native, "native_identity.jsonl", "native_identity_jsonl_sha256")
    pr, prows = verified_jsonl(provisional, "candidates.jsonl", "candidates_jsonl_sha256")
    if (nr.get("source_repository") != NATIVE_SOURCE or nr.get("source_commit") != NATIVE_COMMIT or
            nr.get("source_card_rows") != 13304 or nr.get("unique_native_identity_rows") != 13247 or
            len(nrows) != 13304 or nr.get("artwork_url_count") != 0 or nr.get("live_vault_writes") != 0):
        raise ValueError("native_source_receipt_drift")
    if (pr.get("source_commit") != CORPUS_COMMIT or pr.get("archived_chinese_pack_rows") != 41185 or
            pr.get("source_cards_examined") != 67243 or pr.get("provisional_candidates") != len(prows) or
            pr.get("verified_live_printing_matches") != 0 or pr.get("live_vault_writes") != 0):
        raise ValueError("provisional_source_receipt_drift")
    mappings = corpus_sets(corpus)
    rows, reasons = join(nrows, prows, mappings)
    output.mkdir(parents=True)
    data = "".join(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows).encode("utf8")
    (output / "private_crosswalk.jsonl").write_bytes(data)
    receipt = {
        "classification": "PRIVATE_SOURCE_NATIVE_PICTURE_CROSSWALK_ONLY",
        "native_source_commit": NATIVE_COMMIT,
        "picture_source_commit": CORPUS_COMMIT,
        "native_rows_examined": len(nrows),
        "native_unique_identity_rows": nr["unique_native_identity_rows"],
        "provisional_picture_candidates_examined": len(prows),
        "source_set_codes_available": len(mappings),
        "classification_counts": reasons,
        "native_name_corroborated_candidates": reasons.get("NATIVE_NAME_AND_NUMBER_CORROBORATED_VARIANT_UNVERIFIED", 0),
        "source_image_urls_not_licensed": len(prows),
        "exact_variant_matches": 0, "live_vault_ids_verified": 0,
        "image_binaries_downloaded": 0, "production_writes": 0, "new_image_links": 0,
        "new_canonical_cards": 0, "rights_granted": False,
        "safe_to_publish": False, "safe_for_vault_apply": False,
        "private_crosswalk_jsonl_sha256": hashlib.sha256(data).hexdigest(),
        "note": "Only exact-source-name corroboration against archived pack. Variant/finish, live canonical identity and image rights unverified. No image import.",
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf8")
    return receipt

def self_test():
    pic = "a" * 32
    p = {"classification": "PROVISIONAL_SET_CODE_COLLECTOR_NUMBER_ONLY", "safe_for_vault_apply": False,
         "image_rights_cleared": False, "live_vault_identity_verified": False,
         "source_picture_id": pic, "source_picture_url_reference": PIC_BASE + pic,
         "source_set_id": "zh-hans/test", "printed_collector_number": "001/100",
         "archived_canonical_printing_id": "canonical1", "archived_native_chinese_name": "皮卡丘"}
    n = {"language": "zh-cn", "image_url": None, "image_rights_granted": False,
         "safe_for_vault_apply": False, "source_identity_unique": True,
         "source_file_set_code_verified": True, "source_set_code": "CSV1C",
         "source_collector_number": "001", "native_chinese_name": "皮卡丘", "source_rarity": "C"}
    rows, reasons = join([n], [p], {"zh-hans/test": "CSV1C"})
    assert reasons.get("NATIVE_NAME_AND_NUMBER_CORROBORATED_VARIANT_UNVERIFIED") == 1
    assert rows[0]["independent_native_identity_corroborated"] is True
    assert rows[0]["safe_for_vault_apply"] is False and rows[0]["artwork_rights_granted"] is False
    rows, reasons = join([n, dict(n)], [p], {"zh-hans/test": "CSV1C"})
    assert reasons.get("MULTIPLE_NATIVE_ROWS_SAME_SET_NUMBER") == 1
    rows, reasons = join([dict(n, native_chinese_name="喵喵")], [p], {"zh-hans/test": "CSV1C"})
    assert reasons.get("NATIVE_CHINESE_NAME_DISAGREEMENT") == 1
    rows, reasons = join([n], [p, dict(p, source_picture_id="b"*32, source_picture_url_reference=PIC_BASE+"b"*32)], {"zh-hans/test": "CSV1C"})
    assert reasons.get("AMBIGUOUS_CANONICAL_OR_IMAGE_REFERENCE") == 2
    print("PASS: strict native-name corroboration, ambiguous variants quarantined, no live writes")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--native", type=Path)
    ap.add_argument("--provisional", type=Path)
    ap.add_argument("--corpus", type=Path)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test: self_test()
    else:
        if not all([args.native, args.provisional, args.corpus, args.output]):
            ap.error("native, provisional, corpus and output required")
        result = run(args.native, args.provisional, args.corpus, args.output)
        print("SOURCE_ONLY_NATIVE_CROSSWALK_PASS", json.dumps({
            "source_candidates": result["provisional_picture_candidates_examined"],
            "native_name_corroborated": result["native_name_corroborated_candidates"],
            "classifications": result["classification_counts"],
            "new_image_links": 0}, sort_keys=True))
