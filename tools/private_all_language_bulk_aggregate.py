#!/usr/bin/env python3
"""Join all independently SHA-verified TCGdex set-gap batch artifacts.

Research-only bulk inventory. No card/image binaries, customer data, production
writes, publishing permissions or implied canonical inserts.
"""
from __future__ import annotations
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import tempfile

SOURCE_CLASS = "PRIVATE_SOURCE_SET_GAP_EVIDENCE_ONLY"
BATCHES = tuple(range(7))
FILES = ("unmatched_source_sets.jsonl", "unmatched_source_card_briefs.jsonl")

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def checked(root: Path, index: int):
    path = root / ("CardScanR-FullSet-Gap-" + str(index))
    receipt = json.loads((path / "receipt.json").read_text(encoding="utf8"))
    if (receipt.get("classification") != SOURCE_CLASS
            or receipt.get("batch_index") != index
            or receipt.get("batch_size") != 40
            or receipt.get("production_writes") != 0
            or receipt.get("image_binaries_downloaded") != 0
            or receipt.get("verified_new_canonical_cards") != 0
            or receipt.get("safe_for_public_use") is not False
            or receipt.get("rights_granted") is not False
            or len(receipt.get("packs_sha256_verified", [])) != 7
            or len(receipt.get("provider_index_sha256_verified", [])) != 7):
        raise ValueError("invalid_source_page_receipt:" + str(index))
    data = {}
    for filename in FILES:
        raw = (path / filename).read_bytes()
        evidence = receipt.get(filename)
        if (not isinstance(evidence, dict)
                or evidence.get("sha256") != digest(raw)
                or evidence.get("bytes") != len(raw)):
            raise ValueError("source_page_sha_drift:" + str(index) + ":" + filename)
        rows = [json.loads(line) for line in raw.splitlines()]
        if len(rows) != evidence.get("records"):
            raise ValueError("source_page_count_drift")
        data[filename] = rows
    if (len(data[FILES[0]]) != receipt["source_sets_succeeded"]
            or len(data[FILES[1]]) != receipt["card_briefs_captured"]
            or receipt["source_requests"] !=
            receipt["source_sets_succeeded"] + receipt["source_sets_failed"]):
        raise ValueError("source_page_internal_mismatch")
    return receipt, data

def aggregate(root: Path, output: Path, pages=BATCHES):
    if output.exists() or output.is_symlink():
        raise FileExistsError("output_requires_new_directory")
    reports, records = [], {name: [] for name in FILES}
    sets_seen, cards_seen = set(), set()
    for index in pages:
        receipt, data = checked(root, index)
        reports.append(receipt)
        for row in data[FILES[0]]:
            k = (row["language"], row["provider_set_id"])
            if k in sets_seen: raise ValueError("duplicated_source_set_across_pages")
            sets_seen.add(k)
            records[FILES[0]].append(row)
        for row in data[FILES[1]]:
            k = (row["language"], row["provider_set_id"], row["provider_card_id"])
            if k in cards_seen: raise ValueError("duplicated_source_card_across_pages")
            if (row.get("canonical_printing_id") is not None
                    or row.get("exact_match_verified") is not False
                    or row.get("artwork_rights_verified") is not False
                    or row.get("image_binary_downloaded") is not False):
                raise ValueError("source_candidate_misrepresented_as_live")
            cards_seen.add(k)
            records[FILES[1]].append(row)
    reference = reports[0]
    for receipt in reports[1:]:
        if (receipt["total_source_unmatched_set_ids"] != reference["total_source_unmatched_set_ids"]
                or receipt["total_batches"] != reference["total_batches"]
                or receipt["packs_sha256_verified"] != reference["packs_sha256_verified"]
                or receipt["provider_index_sha256_verified"] != reference["provider_index_sha256_verified"]):
            raise ValueError("provider_or_pack_changed_during_parallel_scan")
    if reference["total_batches"] > len(pages):
        raise ValueError("more_source_batches_required")
    if sum(x["source_sets_succeeded"] for x in reports) > 280:
        raise ValueError("unbounded_source_batch_count")
    totals = Counter()
    language = {}
    for row in records[FILES[1]]:
        l = row["language"]
        counts = language.setdefault(l, Counter())
        counts["card_briefs"] += 1
        if row.get("source_image_ref_present") is True:
            counts["image_base_refs"] += 1
    for row in records[FILES[0]]:
        counts = language.setdefault(row["language"], Counter())
        counts["source_sets"] += 1
        if row.get("candidate_status") == "provider_set_id_not_in_pack":
            counts["potential_new_set_code"] += 1
        else:
            counts["possible_alias_set_code"] += 1
    successes = sum(x["source_sets_succeeded"] for x in reports)
    failures = sum(x["source_sets_failed"] for x in reports)
    checked_source_total = reference["total_source_unmatched_set_ids"]
    missing_sources = checked_source_total - successes - failures
    if missing_sources < 0: raise ValueError("source_index_drift")
    all_complete = missing_sources == 0 and failures == 0 and not any(x["stopped_for_429"] for x in reports)
    output.mkdir(parents=True)
    hashes = {}
    for filename, rows in records.items():
        packed_name = filename + ".gz"
        data = "".join(json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                       for x in sorted(rows, key=lambda row: (row["language"], row["provider_set_id"],
                                            row.get("provider_card_id", "")))).encode("utf8")
        gz = gzip.compress(data, mtime=0, compresslevel=9)
        (output / packed_name).write_bytes(gz)
        hashes[packed_name] = {"records": len(rows), "raw_sha256": digest(data),
                              "gzip_sha256": digest(gz), "gzip_bytes": len(gz)}
    report = {
        "classification": "PRIVATE_MULTILINGUAL_BULK_SOURCE_BACKLOG_NOT_LIVE",
        "pages_examined": list(pages),
        "source_sets_unmatched_in_provider_index": checked_source_total,
        "provider_reported_card_positions_unmatched": reference["source_reported_card_positions_unmatched"],
        "source_sets_succeeded": successes,
        "source_sets_failed": failures,
        "source_sets_unvisited_due_to_source_index_growth_or_bound": missing_sources,
        "source_card_briefs_collected": len(records[FILES[1]]),
        "source_image_url_base_refs_observed": sum(bool(row.get("source_image_ref_present"))
                                                   for row in records[FILES[1]]),
        "by_language": {k: dict(sorted(v.items())) for k, v in sorted(language.items())},
        "artifacts": hashes,
        "complete_without_source_errors": all_complete,
        "new_canonical_cards_verified": 0,
        "exact_live_card_matches_verified": 0,
        "new_live_image_links": 0,
        "source_artwork_rights_granted": False,
        "public_artwork_approved": False,
        "image_binaries_downloaded": 0,
        "production_writes": 0,
        "note": "Provider-only set ID != missing release; source card brief != confirmed new canonical printing. Resolve aliases and identities before import.",
    }
    (output / "receipt.json").write_text(json.dumps(report, ensure_ascii=False,
        sort_keys=True, indent=2) + "\n", encoding="utf8")
    return report

def self_test():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        for i in range(2):
            folder = root / f"CardScanR-FullSet-Gap-{i}"
            folder.mkdir()
            sets = [{"language": "th", "provider_set_id": f"set{i}",
                     "candidate_status": "provider_set_id_not_in_pack"}]
            cards = [{"language": "th", "provider_set_id": f"set{i}",
                      "provider_card_id": f"set{i}-1",
                      "canonical_printing_id": None,
                      "exact_match_verified": False,
                      "artwork_rights_verified": False,
                      "image_binary_downloaded": False,
                      "source_image_ref_present": True}]
            receipt = {"classification":SOURCE_CLASS, "batch_index":i, "batch_size":40,
                       "production_writes":0,"image_binaries_downloaded":0,
                       "verified_new_canonical_cards":0,"safe_for_public_use":False,
                       "rights_granted":False,"packs_sha256_verified":[{"x":1}]*7,
                       "provider_index_sha256_verified":[{"x":1}]*7,
                       "source_sets_succeeded":1, "source_sets_failed":0, "source_requests":1,
                       "card_briefs_captured":1,"total_source_unmatched_set_ids":2,
                       "total_batches":1,"source_reported_card_positions_unmatched":2,
                       "stopped_for_429":False}
            for filename, rows in zip(FILES, (sets, cards)):
                raw = "".join(json.dumps(x) + "\n" for x in rows).encode()
                (folder / filename).write_bytes(raw)
                receipt[filename] = {"records":len(rows),"sha256":digest(raw),"bytes":len(raw)}
            (folder / "receipt.json").write_text(json.dumps(receipt))
        out = root / "result"
        doc = aggregate(root, out, pages=(0,1))
        assert doc["source_card_briefs_collected"] == 2
        assert doc["source_image_url_base_refs_observed"] == 2
        assert doc["complete_without_source_errors"] is True
        assert doc["new_live_image_links"] == doc["production_writes"] == 0
        try:
            aggregate(root, out, pages=(0,1))
        except FileExistsError: pass
        else: raise AssertionError("existing_output_overwritten")
        with (root / "CardScanR-FullSet-Gap-1" / FILES[1]).open("ab") as f:
            f.write(b"tamper\n")
        try:
            checked(root, 1)
        except ValueError: pass
        else: raise AssertionError("tampered_source_accepted")
    print("PASS full source batch reconciliation, SHA guard, collision and rights assertions")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source-root", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test: self_test()
    else:
        if not a.source_root or not a.output: p.error("source-root and output required")
        x = aggregate(a.source_root, a.output)
        print("CARD_SCANR_ALL_LANGUAGE_BACKLOG_AGGREGATED",
              json.dumps({"source_sets":x["source_sets_succeeded"],"source_cards":x["source_card_briefs_collected"],
                          "image_refs":x["source_image_url_base_refs_observed"],
                          "complete":x["complete_without_source_errors"],
                          "new_live_cards":0,"new_live_links":0}, sort_keys=True))
