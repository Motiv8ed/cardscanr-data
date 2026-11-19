#!/usr/bin/env python3
"""Bounded TCGdex card+set *detail* acquisition for private source review.

Only fetch public provider JSON. Image/series/logo links are retained as source
references but image files are NEVER downloaded, mirrored or advertised.
No canonical CardScanR identity assignment, active manifest, or DB writes.

This extends (does not replace) tcgdex_metadata_batch.py's card-brief audit:
full card-detail and full set-detail responses, hash receipts and resumable
set/card offsets. Artifacts are private and short-lived pending staged import.
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

from tools.tcgdex_metadata_batch import (
    API, LANGUAGES, CARD_ID, SET_ID, _choose_sets, fetch_json,
)

MAX_SETS = 2
MAX_CARDS_PER_SET = 6
MAX_SET_OFFSET = 2000
MAX_CARD_OFFSET = 1000


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_provider_id(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def gather(
    languages: list[str],
    *,
    max_sets: int = 1,
    cards_per_set: int = 4,
    set_offset: int = 0,
    card_offset: int = 0,
    get: Callable[[str], tuple[Any, bytes]] = fetch_json,
    pace_seconds: float = 0.8,
) -> tuple[list[dict], list[dict], dict]:
    if not languages or len(set(languages)) != len(languages) or any(lang not in LANGUAGES for lang in languages):
        raise ValueError("unsupported_or_duplicate_languages")
    if not 1 <= max_sets <= MAX_SETS or not 1 <= cards_per_set <= MAX_CARDS_PER_SET:
        raise ValueError("request_budget_exceeded")
    if not 0 <= set_offset <= MAX_SET_OFFSET or not 0 <= card_offset <= MAX_CARD_OFFSET:
        raise ValueError("offset_out_of_range")
    if not 0 <= pace_seconds <= 2:
        raise ValueError("pace_out_of_range")

    sets: list[dict] = []
    cards: list[dict] = []
    errors: list[dict] = []
    receipts: list[dict] = []
    set_rosters: dict[str, list[dict]] = {}
    by_language: dict[str, dict] = {
        lang: {"sets": 0, "full_cards": 0, "card_image_references": 0, "set_logo_references": 0}
        for lang in languages
    }
    requests = 0

    def read(url: str, *, language: str, stage: str) -> tuple[Any, str]:
        nonlocal requests
        if not url.startswith(API + "/" + language + "/"):
            raise ValueError("outside_allowlisted_tcgdex_api")
        # Bounded request pacing and no hidden retry bursts.
        if requests and pace_seconds:
            time.sleep(pace_seconds)
        requests += 1
        payload, raw = get(url)
        if not isinstance(raw, bytes) or len(raw) > 2_000_000:
            raise ValueError("source_response_size_invalid")
        sha = _digest(raw)
        receipts.append({
            "language": language, "stage": stage, "url": url,
            "sha256": sha, "bytes": len(raw),
        })
        return payload, sha

    for lang in languages:
        try:
            source_index, _ = read(API + "/" + lang + "/sets", language=lang, stage="set_index")
            if not isinstance(source_index, list):
                raise ValueError("invalid_set_index")
            set_rosters[lang] = [item for item in source_index
                                 if isinstance(item, dict) and _safe_provider_id(item.get("id"), SET_ID)]
            selected = _choose_sets(source_index, set_offset + max_sets)[set_offset:]
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            errors.append({"language": lang, "stage": "set_index", "error_type": type(exc).__name__})
            continue
        if not selected:
            errors.append({"language": lang, "stage": "set_index", "error_type": "NoSetsAtOffset"})
        for sid in selected:
            if not _safe_provider_id(sid, SET_ID):
                continue
            try:
                url = f"{API}/{lang}/sets/{sid}"
                source_set, sha = read(url, language=lang, stage="set_detail")
                if not isinstance(source_set, dict) or source_set.get("id") != sid:
                    raise ValueError("invalid_set_detail_id")
                summaries = source_set.get("cards")
                if not isinstance(summaries, list):
                    raise ValueError("set_missing_cards_index")
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                errors.append({"language": lang, "set_id": sid, "stage": "set_detail", "error_type": type(exc).__name__})
                continue

            # Preserve full public JSON for enrichment, including names, series,
            # release dates, official/total counts, and source image references.
            sets.append({
                "provider": "tcgdex", "language": lang, "provider_set_id": sid,
                "source_url": url, "source_sha256": sha, "source_set_details": source_set,
                "logo_reference_present": isinstance(source_set.get("logo"), str),
                "symbol_reference_present": isinstance(source_set.get("symbol"), str),
                "image_file_downloaded": False, "cardscanr_public_rights_granted": False,
                "canonical_identity_assigned": False,
            })
            by_language[lang]["sets"] += 1
            by_language[lang]["set_logo_references"] += int(isinstance(source_set.get("logo"), str))

            selected_cards = summaries[card_offset:card_offset + cards_per_set]
            seen_card_ids = set()
            for brief in selected_cards:
                if not isinstance(brief, dict):
                    continue
                cid = _safe_provider_id(brief.get("id"), CARD_ID)
                if not cid or cid in seen_card_ids:
                    continue
                seen_card_ids.add(cid)
                try:
                    card_url = f"{API}/{lang}/cards/{cid}"
                    source_card, card_sha = read(card_url, language=lang, stage="card_detail")
                    if not isinstance(source_card, dict) or source_card.get("id") != cid:
                        raise ValueError("invalid_card_detail_id")
                    card_set = source_card.get("set") or {}
                    if isinstance(card_set, dict) and card_set.get("id") not in (None, sid):
                        raise ValueError("cross_set_card_response")
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    errors.append({"language": lang, "set_id": sid, "card_id": cid,
                                   "stage": "card_detail", "error_type": type(exc).__name__})
                    continue
                # We store full source card details for later reconciliation.
                # image is a provider string reference only; no GET of asset host.
                cards.append({
                    "provider": "tcgdex", "language": lang,
                    "provider_set_id": sid, "provider_card_id": cid,
                    "source_url": card_url, "source_sha256": card_sha,
                    "source_card_details": source_card,
                    "image_reference_present": isinstance(source_card.get("image"), str),
                    "image_file_downloaded": False,
                    "cardscanr_public_rights_granted": False,
                    "canonical_identity_assigned": False,
                })
                by_language[lang]["full_cards"] += 1
                by_language[lang]["card_image_references"] += int(
                    isinstance(source_card.get("image"), str)
                )

    report = {
        "schema_version": 1,
        "classification": "PRIVATE_SOURCE_METADATA_RESEARCH_ONLY",
        "created_at_utc": _iso_utc(),
        "languages": languages,
        "set_offset": set_offset,
        "card_offset": card_offset,
        "max_sets_per_language": max_sets,
        "max_cards_per_set": cards_per_set,
        "requests": requests,
        "request_budget": len(languages) * (1 + max_sets * (1 + cards_per_set)),
        "sets_saved": len(sets), "card_details_saved": len(cards),
        "by_language": by_language,
        "set_roster_entries": sum(len(items) for items in set_rosters.values()),
        "source_set_rosters": set_rosters,
        "response_receipts": receipts, "warnings": errors,
        "image_files_downloaded": 0,
        "production_writes": 0, "canonical_identity_assigned": False,
        "artwork_rights_cleared": False, "safe_to_publish": False,
        "status": "pass" if cards and not errors else ("partial" if sets or cards else "no_results"),
    }
    if requests > report["request_budget"]:
        raise AssertionError("source_request_budget_breached")
    return sets, cards, report


def save_snapshot(out: Path, sets: list[dict], cards: list[dict], report: dict) -> dict:
    if out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
        raise FileExistsError("output_must_be_new_or_empty")
    out.mkdir(parents=True, exist_ok=True)
    # Retain full public set index listings so future runs can target precise
    # unvisited set IDs rather than repeatedly probing an empty set shell.
    receipt = dict(report)
    rosters = receipt.pop("source_set_rosters", {})
    roster_bytes = (
        json.dumps(rosters, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    (out / "set_index_rosters.json").write_bytes(roster_bytes)
    receipt["set_index_rosters.json"] = {
        "sha256": _digest(roster_bytes), "bytes": len(roster_bytes),
        "languages": sorted(rosters),
        "entries": sum(len(v) for v in rosters.values()),
    }
    for key, rows in (("set_details.jsonl", sets), ("card_details.jsonl", cards)):
        data = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                       for row in rows).encode("utf-8")
        (out / key).write_bytes(data)
        receipt[key] = {"sha256": _digest(data), "bytes": len(data), "records": len(rows)}
    (out / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--languages", default="ja,zh-tw,ko,en,th,id,zh-cn")
    parser.add_argument("--max-sets", type=int, default=1)
    parser.add_argument("--cards-per-set", type=int, default=4)
    parser.add_argument("--set-offset", type=int, default=0)
    parser.add_argument("--card-offset", type=int, default=0)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sets, cards, report = gather(
        args.languages.split(","), max_sets=args.max_sets,
        cards_per_set=args.cards_per_set, set_offset=args.set_offset,
        card_offset=args.card_offset,
    )
    save_snapshot(args.output, sets, cards, report)
    print(json.dumps({
        "classification": report["classification"], "status": report["status"],
        "sets": len(sets), "full_card_details": len(cards),
        "image_references": sum(v["card_image_references"] for v in report["by_language"].values()),
        "set_roster_entries": report["set_roster_entries"],
        "image_files_downloaded": 0,
    }, sort_keys=True))
    return 0 if sets or cards else 2


if __name__ == "__main__":
    raise SystemExit(main())
