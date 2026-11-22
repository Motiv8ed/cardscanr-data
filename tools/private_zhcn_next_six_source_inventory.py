#!/usr/bin/env python3
"""Pinned metadata-only inventory for six further Simplified Chinese artwork sets.

This is NOT a live Vault matching or import tool. Equal set counts never prove
print identity, finish, licensing, or eligibility for production publication.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

PINNED_COMMIT = "6fabdd3411c14d2d4c4ab84f4f24af79e1814ddc"
PICTURE_RE = re.compile(r"^[a-f0-9]{32}$")
NUMBER_RE = re.compile(r"^[0-9]{3}$")
PICTURE_TEMPLATE = "https://img.cardcorpus.com/s1/{width}/{id}.avif"

# Each expected gap is a READ-ONLY observation from the private live Vault on
# 2026-11-23, NOT a snapshot-locked claim about current importable printings.
NEXT_SETS = {
    "start-deck-100": ("CS4DaC", 433, 566),
    "nine-colours-gathering-friend": ("CS4aC", 184, 295),
    "primordial-arts-overgrow": ("CS3aC", 184, 290),
    "nine-colours-gathering-origin": ("CS4bC", 177, 288),
    "primordial-arts-torrent": ("CS3bC", 179, 283),
    "shadow-of-the-blue-sea-banish": ("CS6bC", 172, 282),
}


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def audit_set(raw: bytes, row: dict, slug: str) -> dict:
    code, expected_cards, expected_variants = NEXT_SETS[slug]
    data = json.loads(raw)
    set_id = "zh-hans/" + slug
    require(isinstance(data, dict), "invalid_source_json")
    require(data.get("id") == set_id and data.get("code") == code, "wrong_source_set")
    require(data.get("catalog") == "zh-hans" and data.get("language") == "zh-Hans",
            "wrong_source_language")
    require(row.get("id") == set_id and row.get("code") == code, "source_index_set_drift")
    require(isinstance(data.get("cards"), list), "missing_source_cards")
    require(len(data["cards"]) == expected_cards, "source_card_count_drift")
    seen = set()
    variants = 0
    pictures = 0
    for card in data["cards"]:
        require(isinstance(card, dict) and isinstance(card.get("id"), str),
                "invalid_source_card")
        require(isinstance(card.get("name"), str) and bool(card["name"].strip()),
                "missing_source_card_name")
        number = card.get("number", {}).get("short")
        require(isinstance(number, str) and NUMBER_RE.fullmatch(number) is not None,
                "invalid_source_collector_number")
        printing_variants = card.get("variants")
        require(isinstance(printing_variants, list) and bool(printing_variants),
                "missing_source_variants")
        for variant in printing_variants:
            require(isinstance(variant, dict) and isinstance(variant.get("id"), str),
                    "invalid_source_variant")
            key = (card["id"], variant["id"])
            require(key not in seen, "duplicate_source_variant")
            seen.add(key)
            variants += 1
            pic = variant.get("picture", {}).get("front", {}).get("image")
            require(isinstance(pic, str) and PICTURE_RE.fullmatch(pic) is not None,
                    "source_variant_without_verified_picture_identifier")
            pictures += 1
    require(variants == expected_variants and pictures == expected_variants,
            "source_variant_count_drift")
    return {
        "source_set": set_id,
        "code": code,
        "source_cards": len(data["cards"]),
        "source_variants": variants,
        "variants_with_picture_ids": pictures,
        "source_metadata_sha256": hashlib.sha256(raw).hexdigest(),
        "vault_missing_count_observed_2026_11_23": expected_variants,
        "live_identity_match_count": 0,
        "new_live_links": 0,
        "identity_matching_done": False,
    }


def inventory(root: Path, out: Path) -> dict:
    require(not out.exists() and not out.is_symlink(), "output_must_be_new")
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    require(index.get("pictures", {}).get("sized") == PICTURE_TEMPLATE,
            "provider_image_recipe_changed")
    require(480 in index["pictures"].get("widths", []),
            "provider_image_size_480_missing")
    names = json.loads((root / "sets" / "zh-hans.json").read_text(encoding="utf-8"))
    require(names.get("catalog") == "zh-hans" and isinstance(names.get("sets"), list),
            "source_set_index_invalid")
    meta = {x["id"]: x for x in names["sets"]}
    require(len(meta) == len(names["sets"]), "duplicate_source_set_index")
    rows = []
    for slug in NEXT_SETS:
        path = root / "sets" / "zh-hans" / (slug + ".json")
        rows.append(audit_set(path.read_bytes(), meta.get("zh-hans/" + slug, {}), slug))
    receipt = {
        "classification": "ZHCN_NEXT_SIX_METADATA_ONLY_NEEDS_LIVE_MATCHING",
        "source_repository": "cardcorpus/pokemon-card-dataset",
        "source_commit": PINNED_COMMIT,
        "provider_index_sha256": hashlib.sha256(
            (root / "index.json").read_bytes()).hexdigest(),
        "source_set_index_sha256": hashlib.sha256(
            (root / "sets" / "zh-hans.json").read_bytes()).hexdigest(),
        "picture_url_recipe": PICTURE_TEMPLATE,
        "sets": rows,
        "source_variant_total": sum(x["source_variants"] for x in rows),
        "possible_vault_gaps_observed_2026_11_23": sum(
            x["vault_missing_count_observed_2026_11_23"] for x in rows),
        "verified_current_live_matches": 0,
        "new_live_links": 0,
        "artwork_bytes_downloaded": 0,
        "public_artwork_enabled": False,
        "artwork_rights_verified": False,
        "live_import_allowed": False,
        "production_writes": 0,
        "next_gate": "scoped current live zh-cn identity export; strict variant match; "
                     "guarded private preflight; backed-up queue receipt",
    }
    out.mkdir(parents=True)
    (out / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return receipt


def self_test() -> None:
    # Unit fixture is deliberately synthetic, not represented as real card proof.
    slug = "start-deck-100"
    fixture = {
        "id": "zh-hans/" + slug, "code": "CS4DaC", "catalog": "zh-hans",
        "language": "zh-Hans", "cards": [{
            "id": "CS4DaC-001", "name": "Fixture",
            "number": {"short": "001"},
            "variants": [{"id": "non-holo",
                          "picture": {"front": {"image": "a" * 32}}}],
        }],
    }
    index = {"id": fixture["id"], "code": "CS4DaC"}
    # Isolate shape checks from the real 433/566 count pin.
    original = NEXT_SETS[slug]
    try:
        NEXT_SETS[slug] = ("CS4DaC", 1, 1)
        raw = json.dumps(fixture).encode()
        require(audit_set(raw, index, slug)["source_variants"] == 1, "valid_fixture_rejected")
        for mutation, error in (
            ({"code": "CS9C"}, "wrong_source_set"),
            ({"cards": [{**fixture["cards"][0],
                        "variants": [{"id": "a", "picture": {"front": {"image": "invalid"}}}]}]},
             "source_variant_without_verified_picture_identifier"),
            ({"cards": [{**fixture["cards"][0],
                        "variants": fixture["cards"][0]["variants"] * 2}]},
             "duplicate_source_variant"),
        ):
            altered = {**fixture, **mutation}
            try:
                audit_set(json.dumps(altered).encode(), index, slug)
            except ValueError as exc:
                require(str(exc) == error, "incorrect_fail_closed_reason")
            else:
                raise AssertionError("unsafe_source_change_accepted")
    finally:
        NEXT_SETS[slug] = original
    print("PASS: source identity, finish variants, picture hashes and fail-closed tests")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.root is None or args.out is None:
        parser.error("--root and --out required")
    receipt = inventory(args.root, args.out)
    print("ZHCN_METADATA_PASS", receipt["source_variant_total"],
          "PICTURE_IDs; NOT LIVE LINKS")


if __name__ == "__main__":
    main()
