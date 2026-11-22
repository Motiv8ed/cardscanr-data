#!/usr/bin/env python3
"""Offline fail-closed full-page coverage gate; no network, artwork, or vault writes."""
import argparse
import json
import re
from pathlib import Path

def verify(manifest, rows):
    if manifest.get("source") != "official_pokemon_asia_archived_listing":
        raise ValueError("untrusted_manifest")
    expected = {}
    for p in manifest["pages"]:
        key = (p["language"], p["set"], p["page"])
        ids = p["official_detail_ids"]
        sha = p["source_list_sha256"]
        if (p["language"] not in ("th", "id") or not p["set"] or
            type(p["page"]) is not int or not 1 <= p["page"] <= 100 or
            not isinstance(ids, list) or not 1 <= len(ids) <= 50 or
            any(not isinstance(i, str) or not re.fullmatch(r"[0-9]{1,10}", i) for i in ids) or
            len(set(ids)) != len(ids) or
            not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) or
            key in expected):
            raise ValueError("invalid_or_duplicate_manifest_page")
        expected[key] = (set(ids), sha)
    if not expected:
        raise ValueError("empty_manifest")
    seen = {key: set() for key in expected}
    for r in rows:
        key = (r.get("language"), r.get("source_product_code"), r.get("source_list_page"))
        if key not in expected:
            raise ValueError("unmanifested_page")
        detail = r.get("official_detail_id")
        if detail not in expected[key][0] or detail in seen[key]:
            raise ValueError("unexpected_or_duplicate_detail")
        if r.get("source_list_sha256") != expected[key][1]:
            raise ValueError("list_sha_mismatch")
        if not isinstance(r.get("source_response_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", r["source_response_sha256"]):
            raise ValueError("missing_detail_sha")
        if r.get("exact_vault_match_proven") is not False or r.get("artwork_rights_cleared") is not False:
            raise ValueError("unsupported_live_or_rights_claim")
        seen[key].add(detail)
    for key, (ids, _) in expected.items():
        if seen[key] != ids:
            raise ValueError("truncated_page:" + str(key))
    return {"status": "PASS", "pages": len(expected), "details": sum(len(v[0]) for v in expected.values()), "new_live_links": 0}

def self_test():
    sha = "a" * 64
    ids = [str(i) for i in range(1, 51)]
    m = {"source": "official_pokemon_asia_archived_listing", "pages": [
        {"language": "th", "set": "SV3", "page": 1, "source_list_sha256": sha, "official_detail_ids": ids}]}
    rows = [{"language": "th", "source_product_code": "SV3", "source_list_page": 1,
             "official_detail_id": i, "source_list_sha256": sha, "source_response_sha256": sha,
             "exact_vault_match_proven": False, "artwork_rights_cleared": False} for i in ids]
    assert verify(m, rows)["details"] == 50
    for bad in (rows[:20], rows[:-1], rows + [rows[0]]):
        try:
            verify(m, bad)
        except ValueError:
            continue
        raise AssertionError("invalid page accepted")
    print("PASS 50-card page and truncated/duplicate fixtures")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--records", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        if not args.manifest or not args.records:
            parser.error("--manifest and --records required")
        m = json.loads(args.manifest.read_text("utf-8"))
        rows = [json.loads(line) for line in args.records.read_text("utf-8").splitlines() if line.strip()]
        print(json.dumps(verify(m, rows), sort_keys=True))
