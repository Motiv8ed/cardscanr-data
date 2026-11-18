#!/usr/bin/env python3
"""Bounded, private TCGdex catalogue discovery (metadata only, no artwork downloads).

Outputs review-only card/set candidates with provenance and SHA256. It deliberately
never assigns canonical CardScanR identities or enables a production release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPRedirectHandler, build_opener

API = "https://api.tcgdex.net/v2"
LANGUAGES = ("ja", "zh-tw", "ko", "en", "th", "id", "zh-cn")
REGIONS = {"ja": "JP", "zh-tw": "TW", "ko": "KR", "en": "INTL", "th": "TH", "id": "ID", "zh-cn": "CN"}
SET_ID = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
CARD_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
MAX_RESPONSE_BYTES = 2_000_000
USER_AGENT = "CardScanR-private-metadata-discovery/1.0 (metadata-only)"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("redirect_denied")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_json(url: str) -> tuple[Any, bytes]:
    if not url.startswith(API + "/"):
        raise ValueError("unexpected_provider_url")
    req = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    opener = build_opener(NoRedirect())
    with opener.open(req, timeout=15) as response:
        if response.status != 200:
            raise ValueError("unexpected_http_status")
        ctype = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if ctype not in ("application/json", "application/problem+json"):
            raise ValueError("unexpected_content_type")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("response_too_large")
    return json.loads(raw.decode("utf-8")), raw


def _short(value: Any, limit: int = 250) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= limit:
        return value
    return None


def _choose_sets(payload: Any, max_sets: int) -> list[str]:
    if not isinstance(payload, list):
        raise ValueError("set_index_not_array")
    candidates = []
    seen = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        sid = item.get("id")
        if not isinstance(sid, str) or not SET_ID.fullmatch(sid) or sid in seen:
            continue
        seen.add(sid)
        date = item.get("releaseDate")
        sort_date = date if isinstance(date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else "0000-00-00"
        candidates.append((sort_date, sid))
    candidates.sort(reverse=True)
    return [sid for _, sid in candidates[:max_sets]]


def _records(language: str, set_id: str, payload: Any, source_sha: str, max_cards: int) -> tuple[list[dict], int]:
    if not isinstance(payload, dict) or payload.get("id") != set_id or not isinstance(payload.get("cards"), list):
        raise ValueError("invalid_set_details")
    records: list[dict] = []
    invalid = 0
    seen = set()
    for item in payload["cards"]:
        if len(records) >= max_cards:
            break
        if not isinstance(item, dict):
            invalid += 1
            continue
        cid = item.get("id")
        name = _short(item.get("name"), 300)
        local_id = _short(item.get("localId"), 80)
        if not isinstance(cid, str) or not CARD_ID.fullmatch(cid) or not name or not local_id:
            invalid += 1
            continue
        if cid in seen:
            invalid += 1
            continue
        seen.add(cid)
        records.append({
            "source_provider": "tcgdex", "source_language": language,
            "source_region_hint": REGIONS[language], "source_set_id": set_id,
            "source_card_id": cid, "local_id": local_id, "name": name,
            "source_url": f"{API}/{language}/sets/{set_id}",
            "source_response_sha256": source_sha,
            "source_image_reference_present": bool(_short(item.get("image"), 2000)),
            "identity_status": "unmatched_candidate", "variant_status": "unverified",
            "artwork_downloaded": False, "artwork_publication_allowed": False,
            "production_published": False,
        })
    return records, invalid


def collect(
    languages: list[str], *, max_sets: int, max_cards: int,
    get: Callable[[str], tuple[Any, bytes]] = fetch_json,
    pace_seconds: float = 0.8,
) -> tuple[list[dict], dict[str, Any]]:
    if not languages or len(set(languages)) != len(languages) or any(lang not in LANGUAGES for lang in languages):
        raise ValueError("unsupported_or_duplicate_languages")
    if not 1 <= max_sets <= 3 or not 1 <= max_cards <= 25:
        raise ValueError("batch_budget_exceeded")
    records: list[dict] = []
    responses: list[dict] = []
    warnings: list[dict] = []
    request_count = 0
    language_counts = {lang: 0 for lang in languages}
    for lang in languages:
        url = f"{API}/{lang}/sets"
        try:
            request_count += 1
            index, raw = get(url)
            selected = _choose_sets(index, max_sets)
            if not selected:
                warnings.append({"language": lang, "stage": "index", "reason": "no_eligible_sets"})
            responses.append({"language": lang, "kind": "set_index", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        except (ValueError, HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            warnings.append({"language": lang, "stage": "index", "reason": type(exc).__name__})
            continue
        for sid in selected:
            if pace_seconds:
                time.sleep(pace_seconds)
            try:
                request_count += 1
                detail, raw = get(f"{API}/{lang}/sets/{sid}")
                checksum = hashlib.sha256(raw).hexdigest()
                batch, invalid = _records(lang, sid, detail, checksum, max_cards)
                records.extend(batch)
                language_counts[lang] += len(batch)
                responses.append({"language": lang, "kind": "set_details", "set_id": sid, "sha256": checksum, "bytes": len(raw), "accepted_card_candidates": len(batch), "rejected_rows": invalid})
            except (ValueError, HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                warnings.append({"language": lang, "stage": "set_details", "set_id": sid, "reason": type(exc).__name__})
    for lang in languages:
        if language_counts[lang] == 0:
            warnings.append({"language": lang, "stage": "coverage", "reason": "no_valid_card_candidates"})
    records.sort(key=lambda row: (LANGUAGES.index(row["source_language"]), row["source_set_id"], row["source_card_id"]))
    report = {
        "schema_version": 1, "provider": "tcgdex", "languages_requested": languages,
        "requests_completed": request_count, "request_budget": len(languages) * (1 + max_sets),
        "set_limit_per_language": max_sets, "card_limit_per_set": max_cards,
        "candidate_cards": len(records), "candidate_cards_by_language": language_counts,
        "set_selection": "release_date_when_in_index_else_provider_id_desc_unverified_recency",
        "response_receipts": responses,
        "warnings": warnings, "status": "pass" if records and not warnings else ("partial" if records else "no_candidates"),
        "metadata_only": True, "image_files_downloaded": 0,
        "provider_ids_are_canonical": False, "public_artwork_rights_cleared": False,
        "app_release_allowed": False, "production_written": False,
    }
    return records, report


def write_snapshot(output: Path, records: list[dict], manifest: dict[str, Any]) -> dict[str, Any]:
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise FileExistsError("output_must_be_a_new_empty_directory")
    output.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for r in records).encode("utf-8")
    (output / "card_candidates.jsonl").write_bytes(data)
    receipt = {**manifest, "generated_at_utc": utc_now(), "candidate_file": "card_candidates.jsonl", "candidate_bytes": len(data), "candidate_sha256": hashlib.sha256(data).hexdigest()}
    (output / "manifest.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--languages", default="ja,zh-tw,ko,en,th,id", help="comma-separated fixed source language codes")
    parser.add_argument("--max-sets", type=int, default=1)
    parser.add_argument("--max-cards", type=int, default=20)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    langs = args.languages.split(",")
    if not langs or len(set(langs)) != len(langs) or any(lang not in LANGUAGES for lang in langs):
        parser.error("languages must be unique supported codes")
    if not 1 <= args.max_sets <= 3 or not 1 <= args.max_cards <= 25:
        parser.error("max-sets must be 1..3; max-cards must be 1..25")
    rows, report = collect(langs, max_sets=args.max_sets, max_cards=args.max_cards)
    manifest = write_snapshot(args.output, rows, report)
    print(json.dumps({"status": manifest["status"], "candidate_cards": len(rows), "requests_completed": manifest["requests_completed"], "image_files_downloaded": 0, "manifest_sha256": manifest["candidate_sha256"]}, sort_keys=True))
    return 0 if rows else 2


if __name__ == "__main__":
    raise SystemExit(main())
