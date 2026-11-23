#!/usr/bin/env python3
"""Read-only immutable CardCorpus source-vs-Vault crosswalk, three scoped holo sets.

Exact name+number+unique holo variant only; all uncertainties quarantined.
No external network, image downloads, production writes or artwork publication.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import unicodedata

PINNED_SOURCE = "6fabdd3411c14d2d4c4ab84f4f24af79e1814ddc"
CONFIG = {
    "cn-cs6ac": ("zh-hans/shadow-of-the-blue-sea-roar", "CS6aC",
                  "0a1494b2d47a4c2fedffc82907a4b9fe7978f22b1f9c84c46eaa6be395a61c52",
                  277, 169, 277, 60, 107, 110,
                  "369266815277eebd64ee2f1fd3a34ebdf0affbc26af7000cf82ee07052d94c38"),
    "cn-cs2ac": ("zh-hans/vivid-portrayals-obsidian", "CS2aC",
                  "e9cda5a0fd4a969807e18097e9be29724cf4af6e7dddfa6906c6cf2ebaa922dc",
                  241, 143, 241, 45, 96, 100,
                  "66bf6045e8391803c5a8d16b3c09e39fd9e87c2dadff07a7274492b4c1634c36"),
    "cn-cs2bc": ("zh-hans/vivid-portrayals-indigo", "CS2bC",
                  "337afd634769096d245044d3d959092eb9d36f44facc19653ae56ef2934fb302",
                  241, 143, 241, 45, 96, 100,
                  "7244ac01c411e75e6ad4a10fdb329091fb625dea203b8bb55130d8f9d8fbc86e"),
}
NUMBER = re.compile(r"^[0-9]{3}$")
PICTURE = re.compile(r"^[0-9a-f]{32}$")


def norm(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def check(code: str, proof: dict, source: dict, source_bytes: bytes) -> dict:
    src, printed, sha, total, src_cards, src_variants, exact, ambiguous, unmatched, snap = CONFIG[code]
    sid = "pokemon-tcg-kb-pikaqian:set:" + code
    if (proof.get("classification") != "PINNED_LIVE_ZHCN_PIKAQIAN_EXACT_HOLO_20261123"
            or proof.get("set_id") != sid or proof.get("source_set_id") != src
            or proof.get("source_commit") != PINNED_SOURCE
            or proof.get("source_response_sha256") != sha
            or proof.get("snapshot_sha256") != snap
            or proof.get("source_card_count") != src_cards
            or proof.get("source_variant_count") != src_variants
            or proof.get("expected_cards") != total
            or proof.get("expected_matches") != {"exact": exact, "ambiguous": ambiguous, "unmatched": unmatched}
            or not isinstance(proof.get("rows"), list) or len(proof["rows"]) != total):
        raise ValueError("live_source_or_snapshot_drift:" + code)
    if (hashlib.sha256(source_bytes).hexdigest() != sha
            or source.get("id") != src or source.get("catalog") != "zh-hans"
            or source.get("language") != "zh-Hans" or source.get("code") != printed
            or not isinstance(source.get("cards"), list) or len(source["cards"]) != src_cards):
        raise ValueError("upstream_source_integrity_mismatch:" + code)
    lookup = defaultdict(list)
    seen_variants = set()
    for card in source["cards"]:
        number = card.get("number", {}).get("short")
        name = norm(card.get("name"))
        variants = card.get("variants")
        if not isinstance(number, str) or not number or not name or not isinstance(variants, list) or not variants:
            raise ValueError("bad_card_identity_or_variants")
        for v in variants:
            key = (card.get("id"), v.get("id"))
            pic = v.get("picture", {}).get("front", {}).get("image")
            if key in seen_variants or not isinstance(pic, str) or not PICTURE.fullmatch(pic):
                raise ValueError("duplicate_source_variant_or_bad_picture")
            seen_variants.add(key)
            if NUMBER.fullmatch(number):
                lookup[(number, name)].append(v)
    if len(seen_variants) != src_variants:
        raise ValueError("source_variant_count_drift")
    counts = Counter()
    seen_live = set()
    candidates = []
    pattern = re.compile(r"^pokemon-tcg-kb-pikaqian:printing:" + re.escape(code)
                         + r"-[0-9]{3}-[0-9a-f]{8}:zh-cn:CN$")
    for row in proof["rows"]:
        cid, name = row.get("canonical_printing_id"), norm(row.get("native_card_name"))
        num = row.get("printed_collector_number")
        if (not isinstance(cid, str) or not pattern.fullmatch(cid) or cid in seen_live
                or row.get("language") != "zh-cn" or row.get("set_id") != sid
                or not isinstance(num, str) or not NUMBER.fullmatch(num) or not name):
            raise ValueError("bad_live_printing_identity")
        seen_live.add(cid)
        hits = lookup.get((num, name), [])
        state = "exact" if len(hits) == 1 else "ambiguous" if hits else "unmatched"
        counts[state] += 1
        if len(hits) == 1:
            if hits[0].get("id") != "holo":
                raise ValueError("unique_variant_is_not_holo")
            candidates.append({"canonical_printing_id": cid, "source_variant_id": "holo",
                               "picture_id": hits[0]["picture"]["front"]["image"],
                               "artwork_rights_verified": False, "safe_for_vault_apply": False})
    if dict(counts) != {"exact": exact, "ambiguous": ambiguous, "unmatched": unmatched}:
        raise ValueError("unexpected_holo_counts:" + code)
    return {"source_set_id": src, "source_commit": PINNED_SOURCE,
            "source_sha256": sha, "live_snapshot_sha256": snap,
            "counts": dict(counts), "exact_private_metadata_candidates": candidates,
            "rights_granted": False, "images_downloaded": 0, "production_writes": 0,
            "public_artwork_enabled": False, "live_import_allowed": False}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.out_dir.exists() or args.out_dir.is_symlink():
        raise FileExistsError("audit_output_must_be_new")
    docs = {}
    for code, config in CONFIG.items():
        proof_path = args.live_dir / ("zhcn_holo_" + code.removeprefix("cn-") + "_20261123.json")
        source_path = args.source_dir / (config[0] + ".json")
        raw = source_path.read_bytes()
        docs[code] = check(code, json.loads(proof_path.read_text("utf-8")), json.loads(raw), raw)
    args.out_dir.mkdir(parents=True)
    for code, doc in docs.items():
        (args.out_dir / (code + ".json")).write_text(json.dumps(doc, indent=2, sort_keys=True)+"\n")
        print("EXACT_HOLO_METADATA_ONLY", code, doc["counts"])
    assert sum(doc["counts"]["exact"] for doc in docs.values()) == 150
    assert sum(doc["counts"]["ambiguous"] + doc["counts"]["unmatched"] for doc in docs.values()) == 609
    print("PASS: 150 exact holo references, 609 quarantined, zero Vault writes")


if __name__ == "__main__":
    main()
