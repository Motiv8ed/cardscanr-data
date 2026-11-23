#!/usr/bin/env python3
"""Fail-closed metadata proof for the S-P and SM-P Chinese promo printings.

No image bytes, licence assertions, Vault writes or speculative finish joins.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import unicodedata

SOURCE_COMMIT = "6fabdd3411c14d2d4c4ab84f4f24af79e1814ddc"
SPECS = {
    "cn-promo-s-p": {
        "source": "zh-hans/swsh-chinese-promos", "code": "S-P",
        "src_sha": "dcbe0e077a8f56b4fce0c87e3f25043104b8af27878e9981b9f4278e3749d820",
        "snapshot": "f7003c452d312a5bc42722ba7b3eaf55bcc0d09244214baec426b1d675c06d8d",
        "live": 242, "source_cards": 220, "source_variants": 240,
        "counts": {"EXACT_SINGLE_SOURCE_VARIANT": 198,
                   "AMBIGUOUS_SOURCE_VARIANTS": 20, "NO_EXACT_NAME_NUMBER": 24},
    },
    "cn-promo-sm-p": {
        "source": "zh-hans/sm-chinese-promos", "code": "SM-P",
        "src_sha": "1cb2a13c57697c8790c2ce41f646ad2f947691486281cc43f4eab222f3bb479f",
        "snapshot": "0e675d47406d3dfcd1936902cf6c80afcad1121ba990b4670b605e8bae69b51b",
        "live": 61, "source_cards": 55, "source_variants": 74,
        "counts": {"EXACT_SINGLE_SOURCE_VARIANT": 37,
                   "AMBIGUOUS_SOURCE_VARIANTS": 10, "NO_EXACT_NAME_NUMBER": 14},
    },
}
PICTURE = re.compile(r"^[0-9a-f]{32}$")
NUMBER = re.compile(r"^[0-9]{3}$")


def norm(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def audit(code: str, live: dict, source: dict, source_bytes: bytes) -> dict:
    s = SPECS[code]
    sid = "pokemon-tcg-kb-pikaqian:set:" + code
    if (live.get("classification") != "PINNED_LIVE_ZHCN_PIKAQIAN_PROMO_PRINTINGS_20261123"
            or live.get("set_id") != sid or live.get("source_set_id") != s["source"]
            or live.get("source_commit") != SOURCE_COMMIT
            or live.get("source_response_sha256") != s["src_sha"]
            or live.get("snapshot_sha256") != s["snapshot"]
            or live.get("expected_cards") != s["live"]
            or not isinstance(live.get("rows"), list)
            or len(live["rows"]) != s["live"]):
        raise ValueError("live_identity_or_snapshot_drift:" + code)
    if (hashlib.sha256(source_bytes).hexdigest() != s["src_sha"]
            or source.get("id") != s["source"]
            or source.get("catalog") != "zh-hans"
            or source.get("language") != "zh-Hans"
            or source.get("code") != s["code"]
            or not isinstance(source.get("cards"), list)
            or len(source["cards"]) != s["source_cards"]):
        raise ValueError("immutable_cardcorpus_source_mismatch:" + code)
    lookup: dict[tuple[str,str], list[dict]] = defaultdict(list)
    seen_variants: set[tuple[str,str]] = set()
    for card in source["cards"]:
        num = card.get("number", {}).get("short")
        name = norm(card.get("name"))
        if not isinstance(num, str) or not num or not name:
            raise ValueError("invalid_source_name_number")
        variants = card.get("variants")
        if not isinstance(variants, list) or not variants:
            raise ValueError("missing_source_variants")
        for variant in variants:
            vid = variant.get("id")
            pic = variant.get("picture", {}).get("front", {}).get("image")
            key = (card.get("id"), vid)
            if (not isinstance(vid, str) or not vid or key in seen_variants
                    or not isinstance(pic, str) or not PICTURE.fullmatch(pic)):
                raise ValueError("source_variant_or_picture_drift")
            seen_variants.add(key)
            if NUMBER.fullmatch(num):
                lookup[(num, name)].append({
                    "source_card_id": card["id"],
                    "source_variant_id": vid, "source_picture_id": pic,
                })
    if len(seen_variants) != s["source_variants"]:
        raise ValueError("source_variant_total_drift:" + code)
    seen_live: set[str] = set()
    counts: Counter[str] = Counter()
    rows = []
    pattern = re.compile(r"^pokemon-tcg-kb-pikaqian:printing:" + re.escape(code)
                         + r"-[0-9]{3}-[0-9a-f]{8}:zh-cn:CN$")
    for row in live["rows"]:
        cid = row.get("canonical_printing_id")
        name = norm(row.get("native_card_name"))
        num = row.get("printed_collector_number")
        if (not isinstance(cid, str) or not pattern.fullmatch(cid)
                or cid in seen_live or row.get("language") != "zh-cn"
                or row.get("set_id") != sid or not isinstance(num, str)
                or not NUMBER.fullmatch(num) or not name):
            raise ValueError("invalid_live_printing_identity:" + code)
        seen_live.add(cid)
        candidates = lookup.get((num, name), [])
        classification = ("EXACT_SINGLE_SOURCE_VARIANT" if len(candidates) == 1
                          else "AMBIGUOUS_SOURCE_VARIANTS" if candidates
                          else "NO_EXACT_NAME_NUMBER")
        counts[classification] += 1
        match = candidates[0] if len(candidates) == 1 else {}
        rows.append({
            "canonical_printing_id": cid, "status": classification,
            "source_card_id": match.get("source_card_id"),
            "source_variant_id": match.get("source_variant_id"),
            "source_picture_id": match.get("source_picture_id"),
            "artwork_rights_verified": False, "safe_for_vault_apply": False,
        })
    if dict(counts) != s["counts"]:
        raise ValueError("unique_source_variant_counts_drift:" + code)
    return {
        "classification": "PRIVATE_PROMO_EXACT_SOURCE_METADATA_ONLY",
        "set_id": sid, "source_id": s["source"],
        "source_commit": SOURCE_COMMIT, "source_sha256": s["src_sha"],
        "live_snapshot_sha256": s["snapshot"],
        "counts": dict(sorted(counts.items())),
        "source_variants": len(seen_variants), "candidates": rows,
        "new_live_links": 0, "images_downloaded": 0, "production_writes": 0,
        "artwork_rights_verified": False, "live_import_allowed": False,
        "public_artwork_enabled": False,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--s-p-live", type=Path, required=True)
    p.add_argument("--sm-p-live", type=Path, required=True)
    p.add_argument("--source-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists() or a.output.is_symlink():
        raise FileExistsError("output_must_not_exist")
    out = {}
    for code, proof in (
        ("cn-promo-s-p", a.s_p_live),
        ("cn-promo-sm-p", a.sm_p_live),
    ):
        spec = SPECS[code]
        source_path = a.source_dir / (spec["source"] + ".json")
        source_bytes = source_path.read_bytes()
        out[code] = audit(code, json.loads(proof.read_text("utf8")),
                          json.loads(source_bytes), source_bytes)
    a.output.mkdir(parents=True)
    for code, result in out.items():
        (a.output / (code + ".json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        print("PROMO_EXACT_SOURCE_PASS", code, json.dumps(result["counts"], sort_keys=True))
    exact = sum(x["counts"]["EXACT_SINGLE_SOURCE_VARIANT"] for x in out.values())
    excluded = sum(x["counts"]["AMBIGUOUS_SOURCE_VARIANTS"]
                   + x["counts"]["NO_EXACT_NAME_NUMBER"] for x in out.values())
    assert exact == 235 and excluded == 68
    print("PASS: 235 exact metadata candidates, 68 quarantined, zero imports")


if __name__ == "__main__":
    main()
