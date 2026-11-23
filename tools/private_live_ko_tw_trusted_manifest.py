#!/usr/bin/env python3
"""Offline, fail-closed provenance gate for KO/TW live identity chunks.

The trusted manifest SHA-256 MUST be obtained out of band from an authenticated
live Workbench export receipt. Hashing a manifest supplied by the same caller
without comparing it to that trusted digest is NOT authentication.
No network, production writes, or artwork downloads.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

from tools.private_live_ko_tw_snapshot_adapter import adapt

HEX64 = re.compile(r"^[0-9a-f]{64}$")
SCHEMA = "cardscanr-live-ko-tw-identity-manifest-v1"


def _unique_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate_manifest_key")
        out[k] = v
    return out


def verify(manifest_path, trusted_sha256, chunks):
    if not isinstance(trusted_sha256, str) or not HEX64.fullmatch(trusted_sha256):
        raise ValueError("invalid_trusted_digest")
    raw = Path(manifest_path).read_bytes()
    if len(raw) > 65536:
        raise ValueError("oversize_manifest")
    if hashlib.sha256(raw).hexdigest() != trusted_sha256:
        raise ValueError("untrusted_manifest")
    doc = json.loads(raw, object_pairs_hook=_unique_pairs)
    if not isinstance(doc, dict) or set(doc) != {"schema", "counts", "chunks"} or doc["schema"] != SCHEMA:
        raise ValueError("invalid_manifest_schema")
    counts = doc["counts"]
    if not isinstance(counts, dict) or set(counts) != {"ko", "zh-tw"} or any(type(v) is not int or not 0 <= v <= 100000 for v in counts.values()):
        raise ValueError("invalid_manifest_counts")
    expected = doc["chunks"]
    if not isinstance(expected, dict) or not 1 <= len(expected) <= 100:
        raise ValueError("invalid_manifest_chunks")
    if any(not isinstance(name, str) or name in (".", "..") or Path(name).name != name or not name.endswith(".jsonl") or not isinstance(digest, str) or not HEX64.fullmatch(digest) for name, digest in expected.items()):
        raise ValueError("invalid_manifest_chunk_entry")
    paths = [Path(p) for p in chunks]
    if len(paths) != len(expected) or len(set(p.name for p in paths)) != len(paths) or set(p.name for p in paths) != set(expected):
        raise ValueError("chunk_inventory_mismatch")
    for path in paths:
        raw_chunk = path.read_bytes()
        if len(raw_chunk) > 2_000_000 or hashlib.sha256(raw_chunk).hexdigest() != expected[path.name]:
            raise ValueError("chunk_digest_mismatch")
    return counts


def verified_adapt(manifest_path, trusted_sha256, chunks, output):
    counts = verify(manifest_path, trusted_sha256, chunks)
    return adapt([Path(p) for p in chunks], Path(output), expected_counts=counts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--trusted-manifest-sha256", required=True,
                        help="Independently pinned SHA256 from authenticated Workbench receipt")
    parser.add_argument("--live", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verified_adapt(args.manifest, args.trusted_manifest_sha256, args.live, args.output)
    print(json.dumps({"rows": result["rows"], "classification": result["classification"],
                      "production_writes": False}, sort_keys=True))
