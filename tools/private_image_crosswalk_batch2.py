#!/usr/bin/env python3
"""Private exact-printing image-reference and set-metadata evidence, batch 2.

Only six exact source set IDs from PR #70's verified gap audit and PR #71's
independently archived provider roster. Uses PR #77's strict identity join.
No artwork bytes, customer data, live writes, permissions, or public output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from tools.preflight_multilingual_crossrelease import CANARY, MAX_GZIP, get, inspect_pack
from tools.preflight_legacy_korean_pack import pick
from tools.tcgdex_metadata_batch import API, fetch_json
from tools.private_image_identity_crosswalk import crosswalk_rows

# Exact source ID+language matches independently checked against the saved
# 808-row TCGdex set roster. The gap value is a CARD-count, not image rights.
TARGETS = (
    ("zh-tw", "SV4a", 314),
    ("zh-tw", "S12a", 250),
    ("th", "SV2a", 207),
    ("th", "SV8a", 187),
    ("id", "SV8s", 244),
    ("id", "SV8a", 237),
)
EXPECTED_TOTAL_GAP = 1439
LANGUAGES = ("zh-tw", "th", "id")


def checked_source_set(payload: object, language: str, set_id: str) -> list[dict]:
    if not isinstance(payload, dict) or payload.get("id") != set_id:
        raise ValueError("provider_set_identity_mismatch")
    rows = payload.get("cards")
    if not isinstance(rows, list) or not rows:
        raise ValueError("source_set_has_no_cards")
    ids = [row.get("id") for row in rows if isinstance(row, dict)]
    if len(ids) != len(rows) or len(ids) != len(set(ids)):
        raise ValueError("duplicate_or_malformed_source_cards")
    return rows


def run(*, fetch=get, source_fetch=fetch_json) -> tuple[list[dict], list[dict], dict]:
    manifest = json.loads(fetch(CANARY, 2_000_000))
    sources: list[dict] = []
    matches: list[dict] = []
    audits: list[dict] = []
    fingerprints: list[dict] = []
    if sum(gap for _, _, gap in TARGETS) != EXPECTED_TOTAL_GAP:
        raise AssertionError("audit_target_gap_sum_drift")

    with tempfile.TemporaryDirectory(prefix="cardscanr-image-batch2-") as td:
        root = Path(td)
        for language in LANGUAGES:
            desc = pick(manifest, language)
            raw = fetch(desc["compressedDatabaseUrl"], MAX_GZIP)
            dest = root / (language + ".sqlite")
            inspect_pack(desc, raw, dest)  # gzip, raw SHA, schema, count, integrity
            uri = "file:" + quote(dest.resolve().as_posix(), safe="/:") + "?mode=ro&immutable=1"
            with sqlite3.connect(uri, uri=True) as db:
                db.execute("PRAGMA query_only=ON")
                columns = (
                    "canonical_base_id", "canonical_printing_id", "set_id", "language",
                    "printed_collector_number", "native_card_name",
                    "image_thumbnail_url", "image_display_url",
                )
                for lang, sid, expected_gap in TARGETS:
                    if lang != language:
                        continue
                    canonical_set = "tcgdex:asia:set:" + sid
                    pack_rows = [
                        dict(zip(columns, row))
                        for row in db.execute(
                            "SELECT " + ",".join(columns) + " FROM cards "
                            "WHERE set_id=? AND language=?",
                            (canonical_set, language),
                        )
                    ]
                    if not pack_rows:
                        raise ValueError("verified_target_set_missing:" + lang + "/" + sid)
                    actual_gap = sum(
                        not (row["image_thumbnail_url"] or row["image_display_url"])
                        for row in pack_rows
                    )
                    if actual_gap != expected_gap:
                        raise ValueError("verified_image_gap_changed:" + lang + "/" + sid)
                    url = API + "/" + lang + "/sets/" + sid
                    response, body = source_fetch(url)
                    if not isinstance(body, bytes) or len(body) > 2_000_000:
                        raise ValueError("source_json_over_budget")
                    source_rows = checked_source_set(response, lang, sid)
                    projected, summary = crosswalk_rows(
                        pack_rows, source_rows, language=lang, source_set_id=sid
                    )
                    summary["expected_pack_missing_image_ref_rows"] = expected_gap
                    summary["actual_pack_missing_image_ref_rows"] = actual_gap
                    summary["pack_sha256_verified"] = True
                    summary["source_response_sha256"] = hashlib.sha256(body).hexdigest()
                    audits.append(summary)
                    matches.extend(projected)
                    sources.append({
                        "language": lang, "source_set_id": sid,
                        "source_url": url, "source_set_details": response,
                        "source_response_sha256": hashlib.sha256(body).hexdigest(),
                        "artwork_downloaded": False, "rights_cleared": False,
                    })
                    fingerprints.append({
                        "language": lang, "pack_sha256": desc["sha256"],
                        "source_set_id": sid, "source_response_sha256": hashlib.sha256(body).hexdigest(),
                    })

    distinct = {
        (m["language"], m["provider_card_id"])
        for m in matches
    }
    if len(distinct) != len(matches):
        raise ValueError("duplicate_cross_set_source_card_id")
    status = Counter(m["status"] for m in matches)
    totals = {
        "schema_version": 1,
        "classification": "PRIVATE_IMAGE_REFERENCE_IDENTITY_CROSSWALK_BATCH_2",
        "pack_languages_sha256_verified": len(LANGUAGES),
        "source_set_count": len(TARGETS),
        "target_pack_missing_image_reference_rows": EXPECTED_TOTAL_GAP,
        "source_card_count": len(matches),
        "matching_status_counts": dict(sorted(status.items())),
        "exact_unique_printing_matches": status["exact_unique"],
        "source_image_reference_candidates": sum(bool(m["source_image_base_url"]) for m in matches),
        "exact_matches_missing_image_with_source_reference": sum(
            m["candidate_for_missing_pack_image_ref"] for m in matches
        ),
        "set_evidence": audits,
        "source_fingerprints": fingerprints,
        "provider_set_requests": len(TARGETS),
        "image_bodies_downloaded": 0, "production_writes": 0,
        "customer_data_read": False, "artwork_rights_cleared": False,
        "canonical_identity_mutated": False, "safe_to_publish": False,
    }
    return matches, sources, totals


def save(out: Path, rows: list[dict], sources: list[dict], report: dict) -> None:
    if out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
        raise FileExistsError("private_report_destination_must_be_new_empty")
    out.mkdir(parents=True, exist_ok=True)
    for name, records in (
        ("printing_matches.jsonl", rows),
        ("source_set_details.jsonl", sources),
    ):
        body = "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        ).encode("utf-8")
        (out / name).write_bytes(body)
        report[name] = {
            "records": len(records), "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    (out / "receipt.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows, sets, report = run()
    save(args.output, rows, sets, report)
    print(json.dumps({
        "sets": report["source_set_count"],
        "source_cards": report["source_card_count"],
        "exact_unique": report["exact_unique_printing_matches"],
        "missing_image_with_source_candidate":
            report["exact_matches_missing_image_with_source_reference"],
        "by_set": report["set_evidence"],
        "image_bodies_downloaded": 0,
        "safe_to_publish": False,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
