#!/usr/bin/env python3
"""Read-only, fail-closed crosswalk audit for Chinese SV-P promotion printings.

No image downloads, no publication, no Vault writes, no licence assumptions.
Never infer special finish/stamp from a name and collector number alone.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import unicodedata

COMMIT = "6fabdd3411c14d2d4c4ab84f4f24af79e1814ddc"
SET_ID = "pokemon-tcg-kb-pikaqian:set:cn-promo-sv-p"
SOURCE_ID = "zh-hans/sv-chinese-promos"
SNAPSHOT_SHA = "29e9fd71bd1b167200f13531c57fb0ab9a36a6ef8188321695b3a0d281ed4cbd"
CID = re.compile(r"^pokemon-tcg-kb-pikaqian:printing:cn-promo-sv-p-[0-9]{3}-[0-9a-f]{8}:zh-cn:CN$")
NUM = re.compile(r"^[0-9]{3}$")
PIC = re.compile(r"^[0-9a-f]{32}$")


def normalize(value):
    return unicodedata.normalize("NFC", str(value or "")).strip()


def audit(live: dict, provider: dict) -> tuple[list[dict], dict]:
    if (live.get("classification") != "PINNED_LIVE_ZHCN_PIKAQIAN_PROMO_PRINTINGS_20261123"
            or live.get("set_id") != SET_ID or live.get("source_set_id") != SOURCE_ID
            or live.get("source_commit") != COMMIT
            or live.get("snapshot_sha256") != SNAPSHOT_SHA
            or live.get("expected_cards") != 447
            or not isinstance(live.get("rows"), list) or len(live["rows"]) != 447):
        raise ValueError("live_identity_set_or_snapshot_drift")
    if (provider.get("id") != SOURCE_ID or provider.get("catalog") != "zh-hans"
            or provider.get("language") != "zh-Hans" or provider.get("code") != "SV-P"
            or not isinstance(provider.get("cards"), list)
            or len(provider["cards"]) != 498):
        raise ValueError("pinned_cardcorpus_source_changed")

    source_index = defaultdict(list)
    seen_source_variant = set()
    for card in provider["cards"]:
        number = card.get("number", {}).get("short")
        name = normalize(card.get("name"))
        if not isinstance(number, str) or not NUM.fullmatch(number) or not name:
            raise ValueError("invalid_source_name_or_number")
        variants = card.get("variants")
        if not isinstance(variants, list) or not variants:
            raise ValueError("missing_source_variants")
        for variant in variants:
            variant_id = variant.get("id")
            picture = variant.get("picture", {}).get("front", {}).get("image")
            key = (card.get("id"), variant_id)
            if (not isinstance(variant_id, str) or not variant_id
                    or key in seen_source_variant or not isinstance(picture, str)
                    or not PIC.fullmatch(picture)):
                raise ValueError("duplicate_variant_or_picture_metadata_drift")
            seen_source_variant.add(key)
            source_index[(number, name)].append({
                "source_variant_id": variant_id, "picture_id": picture,
                "source_card_id": card["id"],
            })
    if len(seen_source_variant) != 861:
        raise ValueError("source_variant_total_changed")

    seen_canonical = set()
    counts = Counter()
    matches = []
    for row in live["rows"]:
        cardid = row.get("canonical_printing_id")
        name = normalize(row.get("native_card_name"))
        number = row.get("printed_collector_number")
        if (not isinstance(cardid, str) or not CID.fullmatch(cardid)
                or cardid in seen_canonical or row.get("language") != "zh-cn"
                or row.get("set_id") != SET_ID or not isinstance(number, str)
                or not NUM.fullmatch(number) or not name):
            raise ValueError("invalid_or_duplicate_live_printing_identity")
        seen_canonical.add(cardid)
        candidates = source_index.get((number, name), [])
        state = ("EXACT_SINGLE_SOURCE_VARIANT" if len(candidates) == 1
                 else "AMBIGUOUS_SOURCE_VARIANTS" if candidates
                 else "NO_EXACT_NAME_NUMBER")
        counts[state] += 1
        matches.append({
            "canonical_printing_id": cardid,
            "classification": state,
            "source_card_id": candidates[0]["source_card_id"] if len(candidates) == 1 else None,
            "source_variant_id": candidates[0]["source_variant_id"] if len(candidates) == 1 else None,
            "source_picture_id": candidates[0]["picture_id"] if len(candidates) == 1 else None,
            "artwork_rights_verified": False,
            "safe_for_vault_apply": False,
            "production_writes": 0,
        })
    return matches, dict(sorted(counts.items()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", type=Path, required=True)
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.out.exists() or a.out.is_symlink():
        raise FileExistsError("new_output_required")
    raw = a.source.read_bytes()
    output, counts = audit(json.loads(a.live.read_text("utf8")), json.loads(raw))
    expected = {
        "EXACT_SINGLE_SOURCE_VARIANT": 220,
        "AMBIGUOUS_SOURCE_VARIANTS": 75,
        "NO_EXACT_NAME_NUMBER": 152,
    }
    if counts != expected:
        raise ValueError("SVP_crosswalk_counts_changed")
    a.out.mkdir(parents=True)
    metadata = "".join(json.dumps(x, sort_keys=True) + "\n" for x in output).encode()
    (a.out / "candidate_audit.jsonl").write_bytes(metadata)
    receipt = {
        "classification": "PRIVATE_ZHCN_SVP_PROMO_SOURCE_REFERENCE_ONLY",
        "source_commit": COMMIT, "source_file_sha256": hashlib.sha256(raw).hexdigest(),
        "live_snapshot_sha256": SNAPSHOT_SHA,
        "match_counts": counts, "candidate_metadata_sha256": hashlib.sha256(metadata).hexdigest(),
        "exact_source_candidates_needing_live_preflight": 220,
        "artwork_rights_verified": False, "live_import_allowed": False,
        "new_live_links": 0, "images_downloaded": 0, "production_writes": 0,
        "public_artwork_enabled": False,
    }
    (a.out / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print("SVP_PROMO_AUDIT_PASS", json.dumps(receipt["match_counts"], sort_keys=True))


if __name__ == "__main__":
    main()
