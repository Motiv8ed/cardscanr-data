#!/usr/bin/env python3
"""Fail-closed adapter for real live-vault KO/TW identity JSONL chunks.

This does NOT infer printed denominators, finishes, rights, or image URLs.
It produces a SHA-pinned research manifest and an explicitly non-importable
quarantine of records lacking fields required by the strict crosswalk.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path

EXPECTED = {"ko": 1131, "zh-tw": 397}
REQUIRED = ("canonical_printing_id", "language", "set_id",
            "native_card_name", "printed_collector_number")
UNPROVEN = ("printed_denominator", "finish")
def adapt(paths, output):
    if output.exists():
        raise FileExistsError("output_exists")
    if len(paths) != 6 or len(set(map(str, paths))) != 6:
        raise ValueError("expected_six_distinct_chunks")
    names = [path.name for path in paths]
    if len(set(names)) != len(names):
        raise ValueError("duplicate_chunk_basename")
    counts = collections.Counter()
    ids = set()
    hashes = {}
    quarantine = []
    for path in paths:
        raw = path.read_bytes()
        if len(raw) > 2_000_000:
            raise ValueError("oversize_chunk")
        hashes[path.name] = hashlib.sha256(raw).hexdigest()
        lines = [line for line in raw.splitlines() if line.strip()]
        if not lines or len(lines) > 500:
            raise ValueError("invalid_chunk_size")
        for line in lines:
            if len(line) > 16384:
                raise ValueError("oversize_record")
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("non_object")
            if any(not isinstance(row.get(k), str) or not row[k].strip() for k in REQUIRED):
                raise ValueError("missing_core_identity")
            if row.get("has_image_link") is not False:
                raise ValueError("not_missing_image")
            ident = row["canonical_printing_id"]
            if ident in ids:
                raise ValueError("duplicate_canonical")
            ids.add(ident)
            counts[row["language"]] += 1
            missing = [k for k in UNPROVEN if not isinstance(row.get(k), str) or not row[k].strip()]
            quarantine.append({"canonical_printing_id": ident,
                "language": row["language"], "set_id": row["set_id"],
                "native_card_name": row["native_card_name"],
                "printed_collector_number": row["printed_collector_number"],
                "unproven_fields": missing,
                "classification": "quarantine_unproven_printing" if missing else "identity_only_review",
                "safe_for_import": False})
    if dict(counts) != EXPECTED:
        raise ValueError(f"snapshot_count_drift:{dict(counts)}")
    result = {"classification": "LIVE_KO_TW_SOURCE_ONLY_QUARANTINE",
        "input_sha256": dict(sorted(hashes.items())),
        "counts": dict(sorted(counts.items())),
        "rows": len(quarantine),
        "unproven_printings": sum(bool(x["unproven_fields"]) for x in quarantine),
        "exact_live_matches": 0, "new_source_urls": 0,
        "new_live_image_links": 0, "rights_granted": False,
        "production_writes": False, "records": quarantine}
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)+"\n")
    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", type=Path, nargs=6, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = adapt(args.live, args.output)
    print(json.dumps({k: result[k] for k in ("counts", "rows", "unproven_printings")}, sort_keys=True))
