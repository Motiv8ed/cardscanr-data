#!/usr/bin/env python3
"""Corroborate bounded TCGdex API card IDs with pinned public upstream source files.

No customer data, catalogue writes, card artwork downloads or app publication.
Source-file presence verifies provenance evidence, NOT variant or current card accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPRedirectHandler

from tools.reconcile_tcgdex_metadata_batch import read_candidates

UPSTREAM_SHA = "4218f64dcbf3de6e7b7548530f938fb0b9ebbe2a"
UPSTREAM_API = "https://api.github.com/repos/tcgdex/cards-database/contents"
UPSTREAM_RAW = "https://raw.githubusercontent.com/tcgdex/cards-database"
MAX_BYTES = 1_000_000

# Explicit source paths grounded in the upstream cards-database set file tree.
# Allowlist prevents arbitrary user-provided paths and unexpected network access.
SET_PATHS = {
    "base1": "data/Base/Base Set",
    "base2": "data/Base/Jungle",
    "miscp": "data/Miscellaneous/Miscellaneous Promos",
    **{s: "data-asia/PMCG/" + s for s in ("PMCG1", "PMCG2", "PMCG3")},
    **{s: "data-asia/SV/" + s for s in
       ("SV3s", "SV4s", "SVDs", "SV4K", "SV4M", "SV5K")},
    **{s: "data-asia/S/" + s for s in ("SC2D", "SC2a", "SC2b")},
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("redirect_denied")


def get_bytes(url: str) -> bytes:
    if not (url.startswith(UPSTREAM_API + "/") or
            url.startswith(UPSTREAM_RAW + "/" + UPSTREAM_SHA + "/")):
        raise ValueError("not_allowed_upstream_url")
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "CardScanR-Pinned-Upstream-Source-Proof/1.0"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and url.startswith(UPSTREAM_API + "/"):
        headers["Authorization"] = "Bearer " + token
    with build_opener(NoRedirect()).open(Request(url, headers=headers), timeout=20) as response:
        if response.status != 200:
            raise ValueError("upstream_not_http_200")
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("upstream_response_exceeds_cap")
    return raw


def paths_for_set(set_id: str) -> tuple[str, str]:
    base = SET_PATHS.get(set_id)
    if base is None:
        raise ValueError("unapproved_source_set_id")
    escaped = quote(base, safe="/")
    definition = UPSTREAM_RAW + "/" + UPSTREAM_SHA + "/" + escaped + ".ts"
    directory = UPSTREAM_API + "/" + escaped + "?ref=" + UPSTREAM_SHA
    return definition, directory


def set_file_id(source: str) -> str | None:
    match = re.search(r"\bid\s*:\s*['\"]([A-Za-z0-9_.-]+)['\"]\s*,?", source)
    return match.group(1) if match else None


def inspect_set(set_id: str, read=get_bytes) -> dict:
    url_meta, url_dir = paths_for_set(set_id)
    source = read(url_meta)
    files = json.loads(read(url_dir))
    if not isinstance(files, list):
        raise ValueError("upstream_set_listing_not_array")
    card_files = {x["name"][:-3] for x in files
                  if isinstance(x, dict) and x.get("type") == "file"
                  and isinstance(x.get("name"), str)
                  and re.fullmatch(r"[A-Za-z0-9_.-]+\.ts", x["name"])}
    return {
        "set_id": set_id,
        "upstream_set_file_sha256": hashlib.sha256(source).hexdigest(),
        "upstream_set_file_id": set_file_id(source.decode("utf-8")),
        "source_card_file_stems": card_files,
        "source_card_files_count": len(card_files),
    }


def verify(rows: list[dict], lookup=inspect_set) -> tuple[list[dict], dict]:
    if not rows or len(rows) > 1000:
        raise ValueError("unsupported_candidate_batch_size")
    unique_sets = sorted(set(str(r["source_set_id"]) for r in rows))
    if not set(unique_sets).issubset(SET_PATHS):
        raise ValueError("batch_has_unapproved_set_id")
    evidence = {}
    for sid in unique_sets:
        evidence[sid] = lookup(sid)
    classifications = Counter()
    by_language = {}
    details = []
    for r in rows:
        sid, local, lang, cid = (r["source_set_id"], r["local_id"],
                                r["source_language"], r["source_card_id"])
        info = evidence[sid]
        api_shape_valid = cid == sid + "-" + local
        set_id_verified = info["upstream_set_file_id"] == sid
        file_present = local in info["source_card_file_stems"]
        status = ("source_file_and_set_identity_corrobated" if
                  api_shape_valid and set_id_verified and file_present
                  else "not_corrobated_review_required")
        classifications[status] += 1
        by_language.setdefault(lang, Counter())[status] += 1
        details.append({
            "source_language": lang, "source_card_id": cid, "source_set_id": sid,
            "source_file_path": SET_PATHS[sid] + "/" + local + ".ts",
            "status": status, "source_card_file_present": file_present,
            "upstream_set_id_confirmed": set_id_verified,
            "api_id_shape_matches_set_and_local_id": api_shape_valid,
            "physical_variant_confirmed": False,
            "production_publish_approved": False,
        })
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_repository": "tcgdex/cards-database",
        "pinned_upstream_commit_sha": UPSTREAM_SHA,
        "candidate_cards": len(rows),
        "unique_upstream_sets": len(unique_sets),
        "classifications": dict(sorted(classifications.items())),
        "by_language": {k: dict(sorted(v.items())) for k, v in sorted(by_language.items())},
        "set_metadata": {sid: {
            "set_id": sid, "upstream_set_file_sha256": ev["upstream_set_file_sha256"],
            "upstream_set_file_id": ev["upstream_set_file_id"],
            "source_card_files_count": ev["source_card_files_count"]
        } for sid, ev in evidence.items()},
        "audit_scope": "upstream_pinned_source_file_existence_and_set_id",
        "staging_provider_source_records_rechecked": False,
        "physical_variants_confirmed": 0,
        "artwork_bytes_downloaded": 0,
        "production_database_writes": 0,
        "r2_pointer_modified": False,
        "safe_to_auto_import": False,
    }
    return details, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-zip", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.is_symlink() or (args.output.exists() and (
            not args.output.is_dir() or any(args.output.iterdir()))):
        parser.error("output must be a new empty directory")
    rows, manifest = read_candidates(args.source_zip)
    details, result = verify(rows)
    result["input_candidate_sha256"] = manifest["candidate_sha256"]
    body = "".join(json.dumps(d, sort_keys=True) + "\n" for d in details).encode("utf-8")
    result["detail_file_sha256"] = hashlib.sha256(body).hexdigest()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "source_proof.jsonl").write_bytes(body)
    (args.output / "SAFE-Upstream-Source-Proof.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"candidate_cards": len(rows),
                      "classifications": result["classifications"],
                      "input_sha256": result["input_candidate_sha256"]}, sort_keys=True))
    return 0 if result["classifications"].get("not_corrobated_review_required", 0) == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
