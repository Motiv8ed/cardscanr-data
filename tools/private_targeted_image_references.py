#!/usr/bin/env python3
"""Private, bounded exact-set research for gaps found in CardScanR pack canary.

Only TCGdex JSON is requested. Images are represented by provider-supplied URLs;
no image bytes are fetched, mirrored or approved for public use.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError

from tools.tcgdex_metadata_batch import API, CARD_ID, fetch_json

# From the 19-Nov-2026 SHA-pinned seven-pack gap audit and the 808-entry
# TCGdex roster. Exact ID+language matching; no Chinese transliteration aliases.
PRIORITY = (
    {"language": "zh-tw", "set": "SI", "gap": 422, "set_name": "初階牌組100"},
    {"language": "id", "set": "SV4a", "gap": 314, "set_name": "Harta Berkilau ex"},
    {"language": "th", "set": "SV7s", "gap": 223, "set_name": "แสงนำทางแห่งสเตลลาร์"},
    {"language": "ko", "set": "SV4M", "gap": 92, "set_name": "미래의 일섬"},
)
MAX_DETAIL_PER_SET = 6
MAX_BODY = 2_000_000


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def research(
    *,
    max_details_per_set: int = 4,
    pace_seconds: float = 1.2,
    get: Callable[[str], tuple[Any, bytes]] = fetch_json,
) -> tuple[list[dict], list[dict], dict]:
    if not 0 <= max_details_per_set <= MAX_DETAIL_PER_SET:
        raise ValueError("card_detail_budget_exceeded")
    if not 0 <= pace_seconds <= 2.5:
        raise ValueError("pace_out_of_range")
    briefs: list[dict] = []
    full_details: list[dict] = []
    sets: list[dict] = []
    warnings: list[dict] = []
    receipts: list[dict] = []
    requests = 0
    language_summary: dict[str, dict] = {}

    def read(url: str, *, language: str, stage: str) -> tuple[Any, str]:
        nonlocal requests
        if not url.startswith(API + "/" + language + "/"):
            raise ValueError("unexpected_provider_origin")
        if requests and pace_seconds:
            time.sleep(pace_seconds)
        requests += 1
        obj, raw = get(url)
        if not isinstance(raw, bytes) or len(raw) > MAX_BODY:
            raise ValueError("response_over_budget")
        digest = _sha(raw)
        receipts.append({"language": language, "stage": stage, "url": url, "sha256": digest, "bytes": len(raw)})
        return obj, digest

    for target in PRIORITY:
        lang, sid = target["language"], target["set"]
        summary = {
            "provider_set_id": sid,
            "verified_pack_image_url_gap": target["gap"],
            "source_set_brief_cards": 0, "source_brief_image_url_refs": 0,
            "full_details": 0, "full_detail_image_url_refs": 0,
            "status": "unavailable",
        }
        language_summary[lang] = summary
        url = f"{API}/{lang}/sets/{sid}"
        try:
            obj, sha = read(url, language=lang, stage="set_details")
            if not isinstance(obj, dict) or obj.get("id") != sid:
                raise ValueError("wrong_provider_set_id")
            source_cards = obj.get("cards")
            if not isinstance(source_cards, list):
                raise ValueError("no_card_index")
            # The same source ID must never represent different cards in a set.
            seen_ids: set[str] = set()
            accepted: list[dict] = []
            for card in source_cards:
                if not isinstance(card, dict):
                    continue
                cid = card.get("id")
                if not isinstance(cid, str) or not CARD_ID.fullmatch(cid):
                    continue
                if cid in seen_ids:
                    raise ValueError("duplicate_provider_card_identity")
                seen_ids.add(cid)
                row = {
                    "language": lang, "provider_set_id": sid,
                    "provider_card_id": cid, "local_id": card.get("localId"),
                    "source_card_name": card.get("name"),
                    "source_image_url_reference": card.get("image") if isinstance(card.get("image"), str) else None,
                    "source_set_detail_sha256": sha,
                    "canonical_cardscanr_id": None,
                    "image_downloaded": False,
                    "public_artwork_permission": False,
                }
                accepted.append(row)
            briefs.extend(accepted)
            sets.append({
                "language": lang, "provider_set_id": sid,
                "source_url": url, "source_sha256": sha,
                "source_details": obj,
                "original_gap_count": target["gap"],
                "image_downloaded": False, "public_artwork_permission": False,
            })
            summary["source_set_brief_cards"] = len(accepted)
            summary["source_brief_image_url_refs"] = sum(bool(x["source_image_url_reference"]) for x in accepted)
            summary["status"] = "set_complete" if accepted else "empty_set"

            # Card-detail queries target real image references first; this is
            # a bounded inspection, NOT a full-card bulk mirror.
            selected = sorted(accepted, key=lambda x: (not bool(x["source_image_url_reference"]), x["provider_card_id"]))[:max_details_per_set]
            for card in selected:
                cid = card["provider_card_id"]
                card_url = f"{API}/{lang}/cards/{cid}"
                try:
                    detail, card_sha = read(card_url, language=lang, stage="card_details")
                    if not isinstance(detail, dict) or detail.get("id") != cid:
                        raise ValueError("wrong_provider_card_id")
                    card_set = detail.get("set")
                    if isinstance(card_set, dict) and card_set.get("id") not in (None, sid):
                        raise ValueError("cross_set_card")
                    full_details.append({
                        "language": lang, "provider_set_id": sid,
                        "provider_card_id": cid, "source_url": card_url,
                        "source_sha256": card_sha, "source_details": detail,
                        "source_image_url_reference": detail.get("image") if isinstance(detail.get("image"), str) else None,
                        "canonical_cardscanr_id": None,
                        "image_downloaded": False, "public_artwork_permission": False,
                    })
                    summary["full_details"] += 1
                    summary["full_detail_image_url_refs"] += int(isinstance(detail.get("image"), str))
                except (OSError, ValueError, TypeError, HTTPError, URLError) as exc:
                    warnings.append({"language": lang, "set_id": sid, "stage": "card_details", "error": type(exc).__name__})
        except (OSError, ValueError, TypeError, HTTPError, URLError) as exc:
            warnings.append({"language": lang, "set_id": sid, "stage": "set_details", "error": type(exc).__name__})

    report = {
        "schema_version": 1, "status": "pass" if len(sets) == len(PRIORITY) and not warnings else "partial",
        "classification": "REVIEW_ONLY_PROVIDER_IMAGE_REFERENCE_CANDIDATES",
        "exact_language_set_targets": [{"language": x["language"], "set": x["set"], "verified_image_url_gap": x["gap"]} for x in PRIORITY],
        "total_target_gap_rows": sum(x["gap"] for x in PRIORITY),
        "requests": requests,
        "max_requests": len(PRIORITY) * (1 + max_details_per_set),
        "set_details": len(sets), "brief_card_candidates": len(briefs), "full_card_details": len(full_details),
        "provider_image_url_candidates": sum(bool(x["source_image_url_reference"]) for x in briefs),
        "by_language": language_summary, "warnings": warnings, "source_sha_receipts": receipts,
        "image_binaries_downloaded": 0, "production_writes": 0,
        "artwork_legal_rights_cleared": False, "canonical_identity_assigned": False,
        "public_release_allowed": False,
    }
    if requests > report["max_requests"]:
        raise AssertionError("request_budget_exceeded")
    return sets, briefs, full_details, report


def save_private_archive(out: Path, sets: list[dict], briefs: list[dict], details: list[dict], report: dict) -> dict:
    if out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
        raise FileExistsError("output_directory_must_be_new_and_empty")
    out.mkdir(parents=True, exist_ok=True)
    final = dict(report)
    for filename, rows in [
        ("source_sets.jsonl", sets), ("candidate_card_image_references.jsonl", briefs),
        ("source_full_cards.jsonl", details),
    ]:
        content = "".join(json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n" for x in rows).encode("utf-8")
        (out / filename).write_bytes(content)
        final[filename] = {"count": len(rows), "sha256": _sha(content), "bytes": len(content)}
    (out / "receipt.json").write_text(json.dumps(final, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return final


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--max-details-per-set", type=int, default=4)
    args = ap.parse_args()
    sets, briefs, full_cards, report = research(max_details_per_set=args.max_details_per_set)
    save_private_archive(args.output, sets, briefs, full_cards, report)
    print(json.dumps({
        "status": report["status"], "targets": len(PRIORITY),
        "set_details": report["set_details"], "brief_card_candidates": report["brief_card_candidates"],
        "source_image_url_candidates": report["provider_image_url_candidates"],
        "full_card_details": report["full_card_details"],
        "requests": report["requests"], "image_binaries_downloaded": 0,
        "warnings": len(report["warnings"]),
    }, sort_keys=True))
    return 0 if sets else 2


if __name__ == "__main__":
    raise SystemExit(main())
