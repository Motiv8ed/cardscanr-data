#!/usr/bin/env python3
"""Offline evidence gate for newly mined official TH/ID source rows.

Read-only: no network, artwork downloads, vault access or staging.
Requires a current live missing-identity export for *actual* live-positive counts.
"""
import argparse
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

HEX = re.compile(r"[0-9a-f]{64}\Z")
DETAIL = re.compile(r"[0-9]{1,10}\Z")
IMAGE = re.compile(r"/(th|id)/card-img/\\1([0-9]{8})\\.png\Z")
REQUIRED = ("language", "official_detail_id", "official_detail_url",
            "source_image_url_reference", "source_response_sha256",
            "source_name_native", "source_collector_number")

def check(row):
    if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in REQUIRED):
        return "incomplete_native_source_evidence"
    lang, ident = row["language"], row["official_detail_id"]
    if lang not in ("th", "id") or not DETAIL.fullmatch(ident):
        return "invalid_locale_or_detail_id"
    if not HEX.fullmatch(row["source_response_sha256"]):
        return "invalid_response_sha256"
    for key in ("official_detail_url", "source_image_url_reference"):
        try:
            u = urlsplit(row[key])
            if (u.scheme != "https" or u.hostname != "asia.pokemon-card.com"
                    or u.port not in (None, 443) or u.username or u.password
                    or u.query or u.fragment):
                return "untrusted_official_url"
        except ValueError:
            return "untrusted_official_url"
        if key == "official_detail_url":
            if u.path != f"/{lang}/card-search/detail/{ident}/":
                return "detail_identity_url_mismatch"
        else:
            if u.path != f"/{lang}/card-img/{lang}{ident.zfill(8)}.png":
                return "image_identity_url_mismatch"
    if len(row["source_name_native"]) > 400 or len(row["source_collector_number"]) > 80:
        return "native_identity_outside_bounds"
    return None

def audit(paths, completed):
    rows = []
    for path in paths:
        with path.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if line.strip():
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        parsed = None
                    rows.append((str(path), line_no, parsed))
    counts = Counter((r.get("language"), r.get("official_detail_id")) for _, _, r in rows if isinstance(r, dict))
    url_counts = Counter(r.get("source_image_url_reference") for _, _, r in rows if isinstance(r, dict))
    accepted, quarantined = [], []
    for filename, line_no, r in rows:
        reason = "invalid_json_or_nonobject_row" if not isinstance(r, dict) else check(r)
        if not reason and (counts[(r["language"], r["official_detail_id"])] != 1
                           or url_counts[r["source_image_url_reference"]] != 1):
            reason = "duplicate_detail_or_image_across_input"
        if not reason and (not isinstance(r.get("batch_id"), str) or not r["batch_id"].strip()):
            reason = "missing_batch_id_replay_evidence"
        if not reason and r.get("batch_id") in completed:
            reason = "completed_batch_replay"
        record = {"file": filename, "line": line_no,
                  "language": r.get("language") if isinstance(r, dict) else None,
                  "detail_id": r.get("official_detail_id") if isinstance(r, dict) else None,
                  "source_sha256": r.get("source_response_sha256") if isinstance(r, dict) else None}
        (quarantined if reason else accepted).append(dict(record, **({"reason": reason} if reason else {})))
    return {"classification": "OFFLINE_SOURCE_EVIDENCE_ONLY_NOT_LIVE",
            "input_rows": len(rows), "unique_evidence_candidates": len(accepted),
            "quarantined": len(quarantined), "accepted": accepted,
            "quarantine": quarantined, "live_new_links": None,
            "live_preflight_required": True, "artwork_rights_verified": False}

def main():
    p = argparse.ArgumentParser()
    p.add_argument("jsonl", nargs="+", type=Path)
    p.add_argument("--completed-batches", type=Path, required=True,
                   help="JSON array of all previously imported batch IDs")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    completed = json.loads(args.completed_batches.read_text(encoding="utf-8"))
    if not isinstance(completed, list) or any(not isinstance(x, str) for x in completed):
        p.error("completed batch manifest must be JSON array of strings")
    result = audit(args.jsonl, set(completed))
    if args.out.exists():
        p.error("refusing to overwrite existing evidence")
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("input_rows", "unique_evidence_candidates", "quarantined", "live_new_links")}, sort_keys=True))

if __name__ == "__main__":
    main()
