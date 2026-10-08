"""Unit checks for pass4 collector/alias helpers."""
from __future__ import annotations

import image_independence_multisource_resolver as m


def test_collector_candidates_ignores_denominator() -> None:
    assert m.collector_candidates("056/128") == ["056/128", "056", "56"]
    assert "128" not in m.collector_candidates("056/128")


def test_collector_float_normalization() -> None:
    assert "1" in m.collector_candidates("1.0")


def test_promo_and_stamp_parse() -> None:
    assert m.parse_promo_collector("SVP 175") == ("svp", "175")
    assert m.parse_stamp_collector("BST 006") == ("bst", "006")


def test_ja_alias_and_image_override() -> None:
    assert m.JA_SET_NAME_ALIASES_NORM["baseexpansionpack"] == "E1"
    assert m.JA_SET_NAME_ALIASES_NORM["offenseanddefenseofthefurthestends"] == "PCG9"
    assert m.image_set_id("swsh10.5") == "pgo"
    assert m.image_set_id("2021swsh") == "mcd21"


def test_names_cross_script() -> None:
    assert m.names_compatible("Koffing", "ドガース") is True
    assert m.names_compatible("Bulbasaur", "Ivysaur") is False


def test_scrydex_placeholder_constant() -> None:
    assert len(m.SCRYDEX_MISSING_IMAGE_SHA256) == 64


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all_pass")
