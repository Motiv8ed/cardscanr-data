#!/usr/bin/env python3
"""Offline quality gate for an upstream TCGdex source-card *candidate* ledger.

The ledger contains upstream Git object hashes, not approved CardScanR IDs or
image licenses. This module cannot add canonical cards or modify production.
"""
from __future__ import annotations
import argparse
import json
import re
from collections import Counter
from pathlib import Path

SCHEMA = "cardscanr-private-upstream-cardfile-candidate-ledger-v1"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
SOURCE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
FOLDER = re.compile(r"^[A-Za-z0-9 .&'-]{1,100}$")
FILES = (
    "ja_ADV_official_source_cardfiles.jsonl",
    "ja_BW_official_source_cardfiles.jsonl",
    "ja_XY_official_source_cardfiles.jsonl",
    "ja_SM_official_source_cardfiles.jsonl",
    "ja_S_official_source_cardfiles.jsonl",
    "ja_M_official_source_cardfiles.jsonl",
    "ja_SV_official_source_cardfiles.jsonl",
    "en_Scarlet_Violet_official_source_cardfiles.jsonl",
    "en_Mega_Evolution_official_source_cardfiles.jsonl",
)
SERIES = {
    "ADV": "5a0cfb587f4263498d368329e4a51e7493050957",
    "BW": "d87921d9a967229374844b37035aed76393a99d2",
    "XY": "e2b54a894d0abe68dc5707d02bf9589c4744bc23",
    "SM": "83aa10ba48fdeaf0301d857fac410c3dbef751c7",
    "S": "0c2b9afd9b488483c4a3a0a3ae05863b346387bf",
    "M": "1190915fcde6fd4397a2f0750bce84fab30fa2b5",
    "SV": "3fee16a2044ba219f5b12418d53c7e310759f783",
    "Scarlet_Violet": "e9542ff78a325c97e2de83223fcd84835c9148ed",
    "Mega_Evolution": "f35b8f0c05a09d1eb7389cc527c2ee416b9e9639",
}
EXPECTED_ROOT_SHA = "36e2f1af67b2b37d1bbf3d6eb8211e223caaf352"
EXPECTED_ASIA_SHA = "889659c6df1ce39809c225aa8db021998234f42a"
EXPECTED_DATA_SHA = "27883e0aedb96b1b010b8344876af969ef1ec607"
REQUIRED_COUNTS = {"ja": 6744, "en": 4634}
MIN_TARGET = 10_000


def verify(root: Path, min_records: int = MIN_TARGET) -> dict:
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("invalid ledger directory")
    manifest_path = root / "upstream_source_candidate_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("missing manifest")
    m = json.loads(manifest_path.read_text("utf-8"))
    if m.get("schema") != SCHEMA or m.get("classification") != "UNRECONCILED_SOURCE_FILE_EVIDENCE_ONLY":
        raise ValueError("invalid provisional ledger schema")
    if m.get("upstream_repo") != "tcgdex/cards-database":
        raise ValueError("source repo drift")
    for key, expected in (
        ("upstream_git_root_tree_sha", EXPECTED_ROOT_SHA),
        ("upstream_data_asia_tree_sha", EXPECTED_ASIA_SHA),
        ("upstream_data_tree_sha", EXPECTED_DATA_SHA),
    ):
        if m.get(key) != expected:
            raise ValueError("source tree digest drift")
    if (m.get("verified_new_canonical_card_count") != 0 or
            m.get("image_urls_imported") != 0 or
            m.get("production_writes") != 0 or
            m.get("artwork_rights_granted") is not False):
        raise ValueError("candidate ledger falsely claims canonical imports/rights")
    ja_missing = m.get("ja_source_set_ids_missing_terminal_code")
    en_excluded = m.get("english_excluded_existing_set_ids")
    if (not isinstance(ja_missing, list) or
            not all(isinstance(x, str) and SOURCE_ID.fullmatch(x) for x in ja_missing) or
            len(set(ja_missing)) != len(ja_missing) or
            not isinstance(en_excluded, list)):
        raise ValueError("invalid source gap allowlists")
    ja_missing = set(ja_missing)
    en_excluded = set(en_excluded)
    observed: set[tuple[str, str, str]] = set()
    set_counts: dict[str, set[str]] = {"ja": set(), "en": set()}
    counts: Counter[str] = Counter()
    evidence = []
    for filename in FILES:
        lang, series, *_ = filename.split("_")
        series = filename.removeprefix(lang + "_").removesuffix("_official_source_cardfiles.jsonl")
        p = root / filename
        if not p.is_file() or p.is_symlink() or p.stat().st_size > 2_000_000:
            raise ValueError("missing or oversized ledger shard: " + filename)
        records = 0
        for line in p.read_text("utf-8").splitlines():
            if not line:
                raise ValueError("blank ledger entry")
            obj = json.loads(line)
            if not isinstance(obj, dict) or obj.get("language") != lang or obj.get("series") != series:
                raise ValueError("language or series drift")
            sid, local = obj.get("source_set_id"), obj.get("source_local_id")
            blob = obj.get("upstream_blob_sha1")
            if (not isinstance(sid, str) or not SOURCE_ID.fullmatch(sid) or
                    not isinstance(local, str) or not SOURCE_ID.fullmatch(local) or
                    not isinstance(blob, str) or not HEX40.fullmatch(blob)):
                raise ValueError("invalid source card file id or blob SHA")
            expected_keys = {"language", "series", "source_set_id",
                             "source_local_id", "upstream_blob_sha1"}
            if lang == "ja":
                if set(obj) != expected_keys or sid not in ja_missing:
                    raise ValueError("Japanese source set not in candidate allowlist")
            elif lang == "en":
                folder = obj.get("source_set_directory")
                definition = obj.get("upstream_set_definition_sha1")
                if (set(obj) != expected_keys | {"source_set_directory",
                                                 "upstream_set_definition_sha1"} or
                        not isinstance(folder, str) or not FOLDER.fullmatch(folder) or
                        not isinstance(definition, str) or not HEX40.fullmatch(definition) or
                        sid.lower() in en_excluded):
                    raise ValueError("English set definition invalid or already in vault")
            else:
                raise ValueError("unsupported candidate language")
            identity = (lang, sid.lower(), local.lower())
            if identity in observed:
                raise ValueError("duplicate source candidate card")
            observed.add(identity)
            set_counts[lang].add(sid)
            records += 1
        counts[lang] += records
        evidence.append({"filename": filename, "source_series_tree_sha1": SERIES[series],
                         "records": records})
    if sum(counts.values()) < min_records:
        raise ValueError("verified source candidate file count below target")
    if dict(counts) != REQUIRED_COUNTS:
        raise ValueError("pinned source candidate cardinality changed")
    return {"status": "PASS", "classification": "UNRECONCILED_SOURCE_FILE_EVIDENCE_ONLY",
            "candidate_card_file_records": sum(counts.values()),
            "unique_candidate_identities": len(observed),
            "source_set_counts": {k: len(v) for k, v in set_counts.items()},
            "by_language": dict(counts), "shards": evidence,
            "verified_new_canonical_cards": 0, "vault_writes": 0,
            "artwork_binary_files": 0, "rights_granted": False}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--min-records", type=int, default=MIN_TARGET)
    args = p.parse_args()
    print(json.dumps(verify(args.root, args.min_records), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
