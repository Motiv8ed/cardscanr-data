#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import resolve_zh_hans_catalogue_overlap as ov
from cardscanr_global_catalogue.contracts import build_printing_record


def test_base_pokemon_name_strips_ex() -> None:
    assert ov.base_pokemon_name("Budew ex") == "budew"
    assert ov.base_pokemon_name("Pikachu VMAX") == "pikachu"


def test_species_map_budew() -> None:
    names = ov.load_species_ja_en()
    assert names.get("スボミー") == "Budew"


def test_ko_metadata_record_has_no_image_provenance() -> None:
    rec = build_printing_record(
        source_language="ko",
        provider="tcgdex",
        provider_set_id="SV5K",
        provider_card_id="SV5K-1",
        native_set_name="Test",
        native_card_name="피카츄",
        collector_number="1",
        official_set_total=10,
        release_date=None,
        image_url=None,
        serie_id=None,
        serie_name=None,
    )
    assert rec["language"] == "ko"
    assert rec["imageProvenance"] == []
    assert rec["region"] == "KR"


def test_worktree_catalogue_dir_exists_or_creatable() -> None:
    root = Path(__file__).resolve().parent.parent
    target = root / "data" / "global" / "catalogue"
    target.mkdir(parents=True, exist_ok=True)
    assert target.is_dir()
