#!/usr/bin/env python3
"""Offline, fail-closed missing-only CardCorpus source proof auditor.

Inputs are private metadata only. This does not fetch, stage, or apply artwork.
Usage: python tools/audit_missing_only_source_proof.py --self-test
       python tools/audit_missing_only_source_proof.py --missing missing.json --source source.json --reserved reserved.json --expected-source-sha SHA --expected-missing-sha SHA

missing.json: list of {canonical_id, provider_set, printed_number, native_name, finish}
source.json: list of {provider_set, printed_number, native_name, finish, picture_id}
reserved.json: list of canonical_id strings from all staged/imported sources.
Only explicitly identical physical finishes qualify; opaque suffixes never qualify.
"""
import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

FINISHES = {"normal", "holo", "reverse-holo", "master-ball", "poke-ball"}
PICTURE = re.compile(r"^[0-9a-f]{32}$")

def norm(s):
    if not isinstance(s, str) or not s.strip():
        raise ValueError("missing or invalid name")
    return " ".join(unicodedata.normalize("NFKC", s).casefold().split())

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def read_pinned(path, expected):
    if not re.fullmatch(r"[0-9a-f]{64}", expected or ""):
        raise ValueError("missing exact SHA256 pin")
    raw = Path(path).read_bytes()
    if digest(raw) != expected:
        raise ValueError("SHA256 mismatch: " + str(path))
    return json.loads(raw)

def key(row):
    for field in ("provider_set", "printed_number", "native_name", "finish"):
        if field not in row:
            raise ValueError("missing field: " + field)
    finish = row["finish"]
    if finish not in FINISHES:
        raise ValueError("unverified physical finish")
    number = str(row["printed_number"]).strip()
    if not re.fullmatch(r"[0-9]{1,4}", number):
        raise ValueError("nonnumeric/ambiguous collector number")
    return (str(row["provider_set"]), number.zfill(4), norm(row["native_name"]), finish)

def audit(missing, source, reserved):
    if not isinstance(missing, list) or not isinstance(source, list) or not isinstance(reserved, list):
        raise ValueError("all inputs must be lists")
    if len(reserved) != len(set(reserved)):
        raise ValueError("duplicate reserved canonical ID")
    reserved = set(reserved)
    index = {}
    for s in source:
        k = key(s)
        pic = s.get("picture_id")
        if not isinstance(pic, str) or not PICTURE.fullmatch(pic):
            raise ValueError("invalid or missing picture ID")
        index.setdefault(k, []).append(pic)
    seen = set()
    eligible, quarantine = [], []
    for m in missing:
        cid = m.get("canonical_id")
        if not isinstance(cid, str) or not cid or cid in seen:
            raise ValueError("missing or duplicate canonical ID")
        seen.add(cid)
        try:
            k = key(m)
            pics = index.get(k, [])
            if cid in reserved:
                quarantine.append({"canonical_id": cid, "reason": "already_reserved"})
            elif len(pics) != 1:
                quarantine.append({"canonical_id": cid, "reason": "source_missing_or_ambiguous"})
            else:
                eligible.append({"canonical_id": cid, "picture_id": pics[0], "provider_set": k[0], "printed_number": k[1], "finish": k[3]})
        except ValueError:
            quarantine.append({"canonical_id": cid, "reason": "unverified_identity_or_finish"})
    return {"eligible": eligible, "quarantine": quarantine, "new_live_links": 0, "safe_for_vault_apply": False, "artwork_rights_verified": False}

def self_test():
    pic = "a" * 32
    base = dict(provider_set="cn-cs4dac", printed_number="005", native_name="Yanma", finish="reverse-holo")
    m = dict(base, canonical_id="cn:005:reverse")
    s = dict(base, picture_id=pic)
    assert len(audit([m], [s], [])["eligible"]) == 1
    assert len(audit([m], [dict(s, finish="holo")], [])["eligible"]) == 0
    assert len(audit([m], [s, s], [])["eligible"]) == 0
    assert len(audit([m], [s], [m["canonical_id"]])["eligible"]) == 0
    assert len(audit([dict(m, finish="opaque-x")], [s], [])["eligible"]) == 0
    assert len(audit([dict(m, printed_number="SV-P")], [s], [])["eligible"]) == 0
    for rows in ([m, m],):
        try:
            audit(rows, [s], [])
            raise AssertionError("duplicate accepted")
        except ValueError:
            pass
    print("PASS: seven offline fail-closed source-proof cases")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--missing")
    p.add_argument("--source")
    p.add_argument("--reserved")
    p.add_argument("--expected-missing-sha")
    p.add_argument("--expected-source-sha")
    p.add_argument("--expected-reserved-sha")
    p.add_argument("--out")
    a = p.parse_args()
    if a.self_test:
        self_test()
        return
    if not all((a.missing, a.source, a.reserved, a.expected_missing_sha, a.expected_source_sha, a.expected_reserved_sha, a.out)):
        p.error("all three pinned inputs and output are required")
    result = audit(read_pinned(a.missing, a.expected_missing_sha), read_pinned(a.source, a.expected_source_sha), read_pinned(a.reserved, a.expected_reserved_sha))
    result["source_sha256"] = a.expected_source_sha
    result["missing_sha256"] = a.expected_missing_sha
    result["reserved_sha256"] = a.expected_reserved_sha
    Path(a.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print("AUDIT_ONLY", len(result["eligible"]), len(result["quarantine"]))

if __name__ == "__main__":
    main()
