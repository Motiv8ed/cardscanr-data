#!/usr/bin/env python3
"""Offline, fail-closed triage of #95 source-only TCGdex backlog evidence.

No network, artwork downloads, production writes, or canonical insertions.
A provider set absent from a pack is not evidence of a new release.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import gzip
import io
import hashlib
import json
from pathlib import Path
import tempfile

SOURCE_CLASS = "PRIVATE_MULTILINGUAL_BULK_SOURCE_BACKLOG_NOT_LIVE"
CLASS = "PRIVATE_TCGDEX_UNMATCHED_SOURCE_IDENTITY_TRIAGE_NOT_LIVE"
MAX_INPUT = 24_000_000

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def load_verified(root, name, receipt):
    entry = receipt["artifacts"][name]
    path = root / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_INPUT:
        raise ValueError("unsafe_source_artifact")
    raw = path.read_bytes()
    if sha(raw) != entry["gzip_sha256"] or len(raw) != entry["gzip_bytes"]:
        raise ValueError("source_artifact_sha_mismatch")
    # Never expand an untrusted gzip fully before checking the decompressed limit.
    with gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb") as stream:
        decoded = stream.read(MAX_INPUT * 16 + 1)
    if len(decoded) > MAX_INPUT * 16 or sha(decoded) != entry["raw_sha256"]:
        raise ValueError("source_raw_sha_mismatch")
    rows = [json.loads(line) for line in decoded.splitlines()]
    if len(rows) != entry["records"]:
        raise ValueError("source_row_count_mismatch")
    return rows

def triage(source, output):
    if output.exists() or output.is_symlink():
        raise FileExistsError("output_must_be_new")
    receipt = json.loads((source / "receipt.json").read_text("utf8"))
    if (receipt.get("classification") != SOURCE_CLASS or
        receipt.get("new_canonical_cards_verified") != 0 or
        receipt.get("new_live_image_links") != 0 or
        receipt.get("production_writes") != 0 or
        receipt.get("source_artwork_rights_granted") is not False):
        raise ValueError("untrusted_source_receipt")
    sets = load_verified(source, "unmatched_source_sets.jsonl.gz", receipt)
    cards = load_verified(source, "unmatched_source_card_briefs.jsonl.gz", receipt)
    by_set = {}
    for row in sets:
        key = (row["language"], row["provider_set_id"])
        if key in by_set: raise ValueError("duplicate_source_set")
        by_set[key] = row
    identities = defaultdict(list)
    for row in cards:
        key = (row["language"], row["provider_set_id"])
        if key not in by_set: raise ValueError("orphan_source_card")
        if (row.get("canonical_printing_id") is not None or
            row.get("exact_match_verified") is not False or
            row.get("artwork_rights_verified") is not False):
            raise ValueError("untrusted_live_identity_or_rights")
        local = row.get("provider_local_id")
        name = row.get("native_card_name")
        identities[(key, local)].append(row)
    classified = []
    counts = Counter()
    for row in sorted(cards, key=lambda x: (
            x["language"], x["provider_set_id"], x["provider_card_id"])):
        key = (row["language"], row["provider_set_id"])
        set_row = by_set[key]
        local, name = row.get("provider_local_id"), row.get("native_card_name")
        if set_row.get("candidate_status") != "provider_set_id_not_in_pack":
            status = "quarantine_possible_set_alias"
        elif not isinstance(local, str) or not local.strip() or not isinstance(name, str) or not name.strip():
            status = "quarantine_source_identity_incomplete"
        elif len(identities[(key, local)]) != 1:
            status = "quarantine_duplicate_local_printing_number"
        else:
            status = "research_only_unique_source_brief"
        counts[status] += 1
        classified.append({
            "language": row["language"], "provider_set_id": row["provider_set_id"],
            "provider_card_id": row["provider_card_id"], "provider_local_id": local,
            "native_card_name": name, "source_image_base_url": row.get("source_image_base_url"),
            "source_set_response_sha256": row["source_set_response_sha256"],
            "classification": status, "canonical_printing_id": None,
            "verified_new_card": False, "exact_live_match": False,
            "artwork_rights_granted": False, "safe_for_vault_apply": False})
    if len(classified) != receipt["source_card_briefs_collected"]:
        raise ValueError("source_brief_count_mismatch")
    payload = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":"))+"\n"
                      for r in classified).encode("utf8")
    packed = gzip.compress(payload, compresslevel=9, mtime=0)
    report = {
        "classification": CLASS, "source_receipt_sha256": sha((source/"receipt.json").read_bytes()),
        "source_briefs_checked": len(classified), "by_classification": dict(sorted(counts.items())),
        "research_only": True, "new_canonical_cards_verified": 0,
        "new_live_image_links": 0, "image_binaries_downloaded": 0,
        "production_writes": 0, "public_artwork_approved": False,
        "candidate_file": {"name": "identity_triage.jsonl.gz", "rows": len(classified),
                           "raw_sha256": sha(payload), "gzip_sha256": sha(packed)},
        "note": "Unique provider-local number is NOT a verified printing, variant, new release, or commercial image licence."
    }
    output.mkdir(parents=True)
    (output/"identity_triage.jsonl.gz").write_bytes(packed)
    (output/"receipt.json").write_text(json.dumps(report, sort_keys=True, indent=2)+"\n")
    return report

def self_test():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)/"source"
        root.mkdir()
        sets = [
            {"language":"th","provider_set_id":"A","candidate_status":"provider_set_id_not_in_pack"},
            {"language":"ko","provider_set_id":"B","candidate_status":"possible_cross_provider_alias"}]
        cards = [
            {"language":"th","provider_set_id":"A","provider_card_id":"A-1","provider_local_id":"1",
             "native_card_name":"Example","source_set_response_sha256":"a"*64},
            {"language":"th","provider_set_id":"A","provider_card_id":"A-2","provider_local_id":"1",
             "native_card_name":"Example foil","source_set_response_sha256":"a"*64},
            {"language":"ko","provider_set_id":"B","provider_card_id":"B-1","provider_local_id":"2",
             "native_card_name":"Example","source_set_response_sha256":"b"*64}]
        for row in cards:
            row.update(canonical_printing_id=None, exact_match_verified=False,
                       artwork_rights_verified=False)
        artifacts = {}
        for name, rows in (("unmatched_source_sets.jsonl.gz",sets),
                           ("unmatched_source_card_briefs.jsonl.gz",cards)):
            raw = "".join(json.dumps(x)+"\n" for x in rows).encode()
            gz = gzip.compress(raw,mtime=0)
            (root/name).write_bytes(gz)
            artifacts[name] = {"records":len(rows),"raw_sha256":sha(raw),
                               "gzip_sha256":sha(gz),"gzip_bytes":len(gz)}
        receipt = {"classification":SOURCE_CLASS,"artifacts":artifacts,
                   "source_card_briefs_collected":3,"new_canonical_cards_verified":0,
                   "new_live_image_links":0,"production_writes":0,
                   "source_artwork_rights_granted":False}
        (root/"receipt.json").write_text(json.dumps(receipt))
        report = triage(root,Path(tmp)/"out")
        assert report["by_classification"]["quarantine_duplicate_local_printing_number"] == 2
        assert report["by_classification"]["quarantine_possible_set_alias"] == 1
        assert report["new_live_image_links"] == 0
        try: triage(root,Path(tmp)/"out")
        except FileExistsError: pass
        else: raise AssertionError("output_overwritten")
        # A tiny valid gzip can otherwise expand far beyond the memory budget.
        bomb = gzip.compress(b"x" * (MAX_INPUT * 16 + 1), compresslevel=9, mtime=0)
        (root/"unmatched_source_card_briefs.jsonl.gz").write_bytes(bomb)
        receipt["artifacts"]["unmatched_source_card_briefs.jsonl.gz"].update(
            gzip_sha256=sha(bomb), gzip_bytes=len(bomb))
        (root/"receipt.json").write_text(json.dumps(receipt))
        try: triage(root, Path(tmp)/"bomb")
        except ValueError as exc: assert str(exc) == "source_raw_sha_mismatch"
        else: raise AssertionError("oversized_gzip_accepted")
        (root/"unmatched_source_card_briefs.jsonl.gz").write_bytes(b"tampered")
        try: triage(root,Path(tmp)/"out2")
        except ValueError: pass
        else: raise AssertionError("tamper_accepted")
    print("PASS source brief triage, duplicate and alias quarantine, SHA, no writes")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test: self_test()
    else:
        if not a.source or not a.output: p.error("--source and --output required")
        print(json.dumps(triage(a.source,a.output),sort_keys=True))
