#!/usr/bin/env python3
"""Validate exported Workbench identity-page envelopes, without inferring variants.

Research-only: emits no importable artwork or canonical records.
"""
import hashlib
import json
from pathlib import Path

LANG_COUNTS = {"ko": 1131, "zh-tw": 397}
REQUIRED = ("canonical_printing_id", "language", "set_id",
            "native_card_name", "printed_collector_number")
HEX = set("0123456789abcdef")

def _sha(value):
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX

def normalize_pages(paths, output):
    paths = [Path(p) for p in paths]
    output = Path(output)
    if output.exists():
        raise FileExistsError("output_exists")
    if not paths or len(paths) > 100 or len(set(map(str, paths))) != len(paths):
        raise ValueError("invalid_page_paths")
    seen = set()
    per_language = {}
    rows = []
    input_hashes = {}
    for path in paths:
        if path.name in input_hashes:
            raise ValueError("duplicate_page_basename")
        raw = path.read_bytes()
        if len(raw) > 65536:
            raise ValueError("oversize_page")
        input_hashes[path.name] = hashlib.sha256(raw).hexdigest()
        page = json.loads(raw)
        if not isinstance(page, dict) or page.get("classification") != "LIVE_MISSING_CARD_IDENTITY_READ_ONLY_V1":
            raise ValueError("invalid_page_envelope")
        lang = page.get("language")
        if lang not in LANG_COUNTS or not _sha(page.get("snapshot_sha256")) or not _sha(page.get("page_sha256")):
            raise ValueError("invalid_page_provenance")
        chunk = page.get("rows")
        if not isinstance(chunk, list) or not 1 <= len(chunk) <= 30 or page.get("page_size") != len(chunk):
            raise ValueError("invalid_page_size")
        if page.get("page_limit") != 30 or page.get("missing_cards_in_snapshot") != LANG_COUNTS[lang]:
            raise ValueError("snapshot_count_drift")
        if (type(page.get("production_writes")) is not int or page["production_writes"] != 0
                or page.get("artwork_rights_granted") is not False
                or page.get("image_urls_exposed") is not False
                or page.get("customer_data_exposed") is not False
                or page.get("variant_identity_verified") is not False
                or page.get("snapshot_consistency_must_match_across_pages") is not True):
            raise ValueError("unsafe_page_flags")
        calculated = hashlib.sha256(json.dumps(chunk, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if calculated != page["page_sha256"]:
            raise ValueError("page_hash_mismatch")
        previous = per_language.get(lang)
        if previous:
            if previous["snapshot"] != page["snapshot_sha256"] or previous["cursor"] is None:
                raise ValueError("snapshot_or_chain_drift")
            if not isinstance(chunk[0], dict) or not isinstance(chunk[0].get("canonical_printing_id"), str) or chunk[0]["canonical_printing_id"] <= previous["cursor"]:
                raise ValueError("nonmonotonic_page")
        else:
            previous = {"snapshot": page["snapshot_sha256"], "cursor": None, "count": 0}
        for row in chunk:
            if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in REQUIRED):
                raise ValueError("incomplete_identity")
            cid = row["canonical_printing_id"]
            if row["language"] != lang or row.get("has_image_link") is not False or cid in seen:
                raise ValueError("identity_drift_or_duplicate")
            seen.add(cid)
            # Never fabricate denominator or finish; page snapshot hash is provenance only.
            rows.append({**row, "snapshot_sha256": page["snapshot_sha256"]})
        # The Workbench cursor is ordered by canonical ID. Reject a shuffled
        # or repeated identity inside one page even when its hash is valid.
        page_ids = [row["canonical_printing_id"] for row in chunk]
        if page_ids != sorted(page_ids) or len(set(page_ids)) != len(page_ids):
            raise ValueError("nonmonotonic_page")
        cursor = page.get("next_cursor")
        if cursor is not None and len(chunk) != 30:
            raise ValueError("short_nonterminal_page")
        if cursor is not None and (not isinstance(cursor, str) or cursor != chunk[-1]["canonical_printing_id"]):
            raise ValueError("invalid_next_cursor")
        per_language[lang] = {"snapshot": page["snapshot_sha256"], "cursor": cursor,
                              "count": previous["count"] + len(chunk)}
    if set(per_language) != set(LANG_COUNTS) or any(per_language[k]["count"] != v or per_language[k]["cursor"] is not None for k, v in LANG_COUNTS.items()):
        raise ValueError("incomplete_live_snapshot")
    result = {"classification": "READ_ONLY_IDENTITY_PAGE_EXPORT_RESEARCH",
              "counts": LANG_COUNTS, "input_sha256": input_hashes,
              "records": rows, "new_live_image_links": 0,
              "new_canonical_cards": 0, "rights_granted": False,
              "production_writes": False}
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return result
