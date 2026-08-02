from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from cardscanr_worldwide.regional_skeletons import import_regional_skeletons
from cardscanr_worldwide.tcgdex import import_jsonl


def _record(index: int, kind: str, path: str, payload: dict) -> dict:
    return {
        "schema_version": 1,
        "index": index,
        "source_path": path,
        "source_domain": "international",
        "record_type": kind,
        "provider_record_id": path.removesuffix(".ts"),
        "source_byte_size": 10,
        "source_sha256": hashlib.sha256(path.encode()).hexdigest(),
        "payload": payload,
    }


def test_exact_localized_set_and_equal_english_roster_creates_provisional_rows(tmp_path: Path) -> None:
    series = {"id": "base", "name": {"en": "Base", "nl": "Basis"}}
    card_set = {
        "id": "base1", "name": {"en": "Base Set", "nl": "Basis Set"}, "serie": series,
        "cardCount": {"official": 2}, "releaseDate": {"en": "1999-01-09", "nl": "1999-01-09"},
    }
    cards = [
        {"set": card_set, "name": {"en": name}, "category": "Pokemon", "hp": hp,
         "types": ["Lightning"], "variants": {"normal": True}}
        for name, hp in (("Pikachu", 40), ("Raichu", 80))
    ]
    records = [
        _record(0, "series", "data/Base.ts", series),
        _record(1, "set", "data/Base/Base Set.ts", card_set),
        *[_record(index + 2, "card", f"data/Base/Base Set/{index:03d}.ts", card)
          for index, card in enumerate(cards, 1)],
    ]
    source = tmp_path / "tcgdex.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    database = tmp_path / "catalogue.sqlite"
    import_jsonl(database, source, "fixture")

    counters = import_regional_skeletons(database, ["nl"])

    assert counters["releases"] == 1
    assert counters["printings"] == 2
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """select cp.verification_status,cv.variant_key,cv.recognition_status
                 from card_printing cp join set_release sr on sr.id=cp.set_release_id
                 join card_variant cv on cv.card_printing_id=cp.id where sr.language_code='nl'"""
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("provisional", "regional-variant-unclassified", "unknown"),
            ("provisional", "regional-variant-unclassified", "unknown"),
        ]
        assert connection.execute(
            """select count(*) from card_localisation cl join card_printing cp on cp.id=cl.card_printing_id
               join set_release sr on sr.id=cp.set_release_id where sr.language_code='nl'"""
        ).fetchone()[0] == 0
        assert connection.execute(
            """select count(*) from card_image_candidate ci join card_variant cv on cv.id=ci.card_variant_id
               join card_printing cp on cp.id=cv.card_printing_id join set_release sr on sr.id=cp.set_release_id
               where sr.language_code='nl'"""
        ).fetchone()[0] == 0
        issues = connection.execute(
            "select issue_class,count(*) from unresolved_item where language_code='nl' group by issue_class order by issue_class"
        ).fetchall()
        assert [tuple(row) for row in issues] == [
            ("regional_card_image_missing", 2),
            ("regional_card_metadata_unverified", 2),
            ("regional_variant_unclassified", 2),
        ]


def test_count_mismatch_is_not_derived(tmp_path: Path) -> None:
    series = {"id": "dp", "name": {"en": "DP", "pl": "DP"}}
    card_set = {
        "id": "dp2", "name": {"en": "Mysterious Treasures", "pl": "Tajemne Skarby"},
        "serie": series, "cardCount": {"official": 2},
    }
    card = {"set": card_set, "name": {"en": "Pikachu"}, "category": "Pokemon"}
    records = [
        _record(0, "series", "data/DP.ts", series),
        _record(1, "set", "data/DP/DP2.ts", card_set),
        _record(2, "card", "data/DP/DP2/001.ts", card),
    ]
    source = tmp_path / "tcgdex.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    database = tmp_path / "catalogue.sqlite"
    import_jsonl(database, source, "fixture")

    assert import_regional_skeletons(database, ["pl"]) == {}

