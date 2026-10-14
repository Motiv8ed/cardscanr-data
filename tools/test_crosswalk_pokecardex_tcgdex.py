#!/usr/bin/env python3
from __future__ import annotations

import crosswalk_pokecardex_tcgdex as x


def test_half_set_transform() -> None:
    assert x.half_set_tcgdex_id("CS15C") == "CS1.5C"
    assert x.half_set_tcgdex_id("CSV95C") == "CSV9.5C"
    assert x.half_set_tcgdex_id("CSM15C") == "CSM1.5C"
    assert x.half_set_tcgdex_id("CS51C") is None
    assert x.half_set_tcgdex_id("CSV1C") is None


def test_match_sets_exact_and_half() -> None:
    pc = [
        {"shortName": "CSV1C", "fullName": "Eternal Birth", "block": "SV"},
        {"shortName": "CS15C", "fullName": "Dynamax Tactics", "block": "SWSH"},
        {"shortName": "MP", "fullName": "Mega Evolution Promotional", "block": "Mega"},
    ]
    tcgdex = {
        "zh-cn": [
            {"id": "CSV1C", "name": "亘古开来", "releaseDate": "2025-01-17", "cardCount": {"official": 127}},
            {"id": "CS1.5C", "name": "极巨攻防", "releaseDate": "2020-01-01", "cardCount": {"official": 96}},
        ],
        "en": [
            {"id": "sv3pt5", "name": "151"},
            {"id": "x", "name": "30th Celebration"},
        ],
        "ja": [],
    }
    rows = x.match_sets(pc, tcgdex)
    by = {r["pokecardexSetId"]: r for r in rows}
    assert by["CSV1C"]["matchMethod"] == "exact_set_id"
    assert by["CSV1C"]["tcgdexZhCnSetId"] == "CSV1C"
    assert by["CS15C"]["matchMethod"] == "half_set_transform"
    assert by["CS15C"]["tcgdexZhCnSetId"] == "CS1.5C"
    assert by["MP"]["matchMethod"] == "unmatched"
