#!/usr/bin/env python3
"""Pin and archive 10,000 distinct CardCorpus source-card briefs for reconciliation.

Source metadata only, licensed CC BY 4.0 with attribution. This script does not
assert that any record is missing from CardScanR, download pictures, or write
to the canonical artwork vault, R2, website or customer tables.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SOURCE_REPO = "cardcorpus/pokemon-card-dataset"
SOURCE_COMMIT = "c4f0b415d1dd3d4fbaadb8c32a0a591126746ed6"
SOURCE_BLOB_SHA1 = "923ec1121aa95a91b775c5f1828810ee37bee43f"
SOURCE_BYTES = 16789547
SOURCE_CARD_COUNT = 67243
SOURCE_URL = (
    "https://raw.githubusercontent.com/" + SOURCE_REPO + "/" + SOURCE_COMMIT
    + "/v1/cards.json"
)
MAX_BYTES = 20_000_000
GOAL = 10000
LANG_ORDER = ("ja", "zh-Hans", "en")
TARGETS = {"ja": 5000, "zh-Hans": 2500, "en": 2500}
PICTURE_ID = re.compile(r"^[0-9a-f]{32}$")


def retrieve_source() -> bytes:
    """Exactly one allowlisted immutable source document, no image requests."""
    for attempt in range(3):
        try:
            req = Request(SOURCE_URL, headers={
                "Accept": "application/json",
                "User-Agent": "CardScanR-private-source-metadata/1.0",
            })
            with urlopen(req, timeout=45) as resp:
                if resp.status != 200:
                    raise ValueError("unexpected_source_http_status")
                raw = resp.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError("source_larger_than_bound")
                return raw
        except HTTPError as exc:
            if exc.code not in (500, 502, 503, 504) or attempt == 2:
                raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
        time.sleep((3, 6)[attempt])
    raise RuntimeError("exhausted_source_retries")


def verify_source(raw: bytes, *, byte_count=SOURCE_BYTES, blob=SOURCE_BLOB_SHA1,
                  expected_cards=SOURCE_CARD_COUNT) -> dict:
    if not isinstance(raw, bytes) or len(raw) != byte_count:
        raise ValueError("source_size_drift")
    git_blob = hashlib.sha1(
        ("blob " + str(len(raw)) + "\0").encode() + raw
    ).hexdigest()
    if git_blob != blob:
        raise ValueError("source_git_blob_sha1_drift")
    document = json.loads(raw)
    if not isinstance(document, dict) or type(document.get("cardCount")) is not int:
        raise ValueError("invalid_card_list_document")
    rows = document.get("cards")
    if (not isinstance(rows, list) or
        document["cardCount"] != expected_cards or len(rows) != expected_cards):
        raise ValueError("published_source_card_count_drift")
    return document


def brief(record: dict) -> dict | None:
    if not isinstance(record, dict) or record.get("$kind") != "card":
        return None
    lang, sid, cid = record.get("language"), record.get("set"), record.get("card")
    name = record.get("name")
    if lang not in LANG_ORDER:
        return None
    if any(not isinstance(x, str) or len(x) < 1 or len(x) > 160
           or any(ord(c) < 32 for c in x) for x in (sid, cid)):
        return None
    if not isinstance(name, str) or not 1 <= len(name) <= 350:
        return None
    number = record.get("number") or {}
    if not isinstance(number, dict):
        return None
    printed = number.get("printed")
    numerator = number.get("numerator")
    if printed is not None and (not isinstance(printed, str) or len(printed) > 80):
        return None
    if numerator is not None and (not isinstance(numerator, str) or len(numerator) > 80):
        return None
    picture = record.get("picture") or {}
    pic = None
    if isinstance(picture, dict) and isinstance(picture.get("front"), dict):
        possible = picture["front"].get("image")
        if isinstance(possible, str) and PICTURE_ID.fullmatch(possible):
            pic = possible
    return {
        "provider": "cardcorpus",
        "provider_set_id": sid,
        "provider_card_id": cid,
        "language": lang,
        "name": name,
        "printed_number": printed,
        "number_numerator": numerator,
        "picture_id_only": pic,
        "source_dataset_commit": SOURCE_COMMIT,
        "canonical_printing_id": None,
        "exact_match_verified": False,
        "verified_new_card": False,
        "artwork_rights_verified": False,
        "image_binary_downloaded": False,
        "public_release_allowed": False,
    }


def select_records(document: dict, goal: int = GOAL) -> tuple[list[dict], dict]:
    if type(goal) is not int or goal != GOAL:
        raise ValueError("goal_requires_exact_ten_thousand")
    eligible = {lang: [] for lang in LANG_ORDER}
    seen = set()
    rejected = 0
    for record in document["cards"]:
        item = brief(record)
        if item is None:
            rejected += 1
            continue
        key = (item["language"], item["provider_set_id"], item["provider_card_id"])
        if key in seen:
            raise ValueError("duplicate_provider_printing_key")
        seen.add(key)
        eligible[item["language"]].append(item)
    selected = []
    selected_keys = set()
    for lang in LANG_ORDER:
        for item in eligible[lang][:TARGETS[lang]]:
            selected.append(item)
            selected_keys.add((lang, item["provider_set_id"], item["provider_card_id"]))
    # Fill any language quota shortfall with remaining eligible preferred records.
    for lang in LANG_ORDER:
        if len(selected) >= goal:
            break
        for item in eligible[lang]:
            key = (lang, item["provider_set_id"], item["provider_card_id"])
            if key not in selected_keys:
                selected.append(item)
                selected_keys.add(key)
            if len(selected) == goal:
                break
    if len(selected) != goal or len(selected_keys) != goal:
        raise ValueError("source_fewer_than_10000_distinct_valid_records")
    return selected, {
        "eligible_by_language": {lang: len(eligible[lang]) for lang in LANG_ORDER},
        "selected_by_language": dict(sorted(Counter(x["language"] for x in selected).items())),
        "skipped_or_unselected_languages": rejected,
        "records_with_picture_id_only": sum(bool(x["picture_id_only"]) for x in selected),
    }


def export(output: Path, records: list[dict], counts: dict, raw: bytes) -> dict:
    if output.is_symlink() or output.exists():
        raise FileExistsError("source_evidence_output_must_not_exist")
    output.mkdir(parents=True)
    data = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                   for row in records).encode("utf-8")
    (output / "source_card_briefs_10000.jsonl").write_bytes(data)
    receipt = {
        "classification": "PRIVATE_EXTERNAL_SOURCE_CARD_EVIDENCE_ONLY",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source": SOURCE_REPO,
        "source_commit": SOURCE_COMMIT,
        "source_url": SOURCE_URL,
        "source_git_blob_sha1_verified": SOURCE_BLOB_SHA1,
        "source_json_sha256": hashlib.sha256(raw).hexdigest(),
        "source_json_bytes": len(raw),
        "source_published_card_count": SOURCE_CARD_COUNT,
        "provider_card_briefs_collected": len(records),
        "briefs_sha256": hashlib.sha256(data).hexdigest(),
        "briefs_bytes": len(data),
        "license": "CC BY 4.0; metadata only; CardCorpus attribution required",
        "attribution_url": "https://github.com/cardcorpus/pokemon-card-dataset",
        "artwork_copyright": "Third-party, not covered by dataset license",
        "exact_new_card_additions": 0,
        "image_binaries_downloaded": 0,
        "vault_writes": 0,
        "production_writes": 0,
        "rights_granted_for_artwork": False,
        "safe_for_public_release": False,
        "existing_CardScanR_overlap_not_yet_determined": True,
        **counts,
    }
    (output / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    (output / "NOTICE.txt").write_text(
        "CardCorpus dataset © 2026 The CardCorpus Authors. "
        "Dataset metadata: CC BY 4.0. "
        "https://github.com/cardcorpus/pokemon-card-dataset\n"
        "Card pictures and text © The Pokémon Company and other relevant "
        "rights holders. No artwork rights are granted by the metadata license.\n"
        "No confirmed new CardScanR cards or artwork links are asserted here.\n",
        encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = retrieve_source()
    source = verify_source(raw)
    rows, counts = select_records(source)
    receipt = export(args.output, rows, counts, raw)
    print(json.dumps({
        "result": "PASS_SOURCE_ONLY_NOT_CANONICAL",
        "source_cards": receipt["provider_card_briefs_collected"],
        "by_language": receipt["selected_by_language"],
        "picture_ids": receipt["records_with_picture_id_only"],
        "source_sha256": receipt["source_json_sha256"],
        "new_verified_CardScanR_cards": 0,
        "artwork_files_downloaded": 0,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
