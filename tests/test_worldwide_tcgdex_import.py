from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from cardscanr_worldwide.tcgdex import add_image_candidates, import_jsonl, variant_rows


def _record(index: int, kind: str, path: str, payload: dict) -> dict:
    return {
        "schema_version": 1,
        "index": index,
        "sourcePath": path,
        "sourceDomain": "asia" if path.startswith("data-asia/") else "international",
        "recordType": kind,
        "providerRecordId": path.removesuffix(".ts"),
        "source_byte_size": 10,
        "source_sha256": hashlib.sha256(path.encode()).hexdigest(),
        "payload": payload,
    }


def _snake_case_exporter_record(record: dict) -> dict:
    return {
        "schema_version": record["schema_version"],
        "index": record["index"],
        "source_path": record["sourcePath"],
        "source_domain": record["sourceDomain"],
        "record_type": record["recordType"],
        "provider_record_id": record["providerRecordId"],
        "source_byte_size": record["source_byte_size"],
        "source_sha256": record["source_sha256"],
        "payload": record["payload"],
    }


def test_detailed_variants_are_language_scoped() -> None:
    values = [
        {"type": "normal"},
        {"type": "reverse", "foil": "pokeball", "languages": ["ja"]},
    ]
    assert [row["variant_key"] for row in variant_rows(values, "ja")] == ["normal", "reverse/pokeball"]
    assert [row["variant_key"] for row in variant_rows(values, "en")] == ["normal"]


def test_missing_variant_data_is_not_guessed() -> None:
    row = variant_rows(None, "en")[0]
    assert row["variant_key"] == "unspecified"
    assert row["recognition_status"] == "unknown"


def test_import_preserves_localisations_and_quarantines_known_bad_name(tmp_path: Path) -> None:
    serie = {"id": "VS", "name": {"ja": "VS"}}
    card_set = {
        "id": "VS1", "name": {"ja": "ポケモンカード★VS"}, "serie": serie,
        "cardCount": {"official": 141}, "releaseDate": {"ja": "2001-07-19"},
    }
    card = {
        "set": card_set,
        "name": {"ja": "ジャニーンのおしっこ"},
        "category": "Pokemon", "rarity": "Common", "dexId": [167],
        "hp": 40, "types": ["Grass"], "retreat": 1,
        "attacks": [{"name": {"ja": "たいあたり"}, "cost": ["Grass"], "damage": 10}],
        "variants": [{"type": "normal", "languages": ["ja"]}],
    }
    records = [
        _snake_case_exporter_record(_record(0, "series", "data-asia/VS.ts", serie)),
        _snake_case_exporter_record(_record(1, "set", "data-asia/VS/VS1.ts", card_set)),
        _snake_case_exporter_record(_record(2, "card", "data-asia/VS/VS1/064.ts", card)),
    ]
    jsonl = tmp_path / "source.jsonl"
    jsonl.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    database = tmp_path / "catalogue.sqlite"

    counters = import_jsonl(database, jsonl, "fixture-commit")

    assert counters["rows_card_printing"] == 1
    with sqlite3.connect(database) as connection:
        printing = connection.execute("select verification_status from card_printing").fetchone()
        unresolved = connection.execute("select issue_class, status from unresolved_item").fetchone()
        localisation = connection.execute("select name from card_localisation").fetchone()
    assert printing == ("quarantined",)
    assert unresolved == ("source_text_quality", "needs_review")
    assert localisation == ("ジャニーンのおしっこ",)


def test_reimport_is_idempotent_for_normalized_entities(tmp_path: Path) -> None:
    serie = {"id": "sv", "name": {"en": "Scarlet & Violet"}}
    card_set = {
        "id": "sv-test", "name": {"en": "Test"}, "serie": serie,
        "cardCount": {"official": 1}, "releaseDate": "2026-01-01",
    }
    card = {
        "set": card_set, "name": {"en": "Pikachu"}, "category": "Pokemon",
        "rarity": "Common", "variants": {"normal": True, "reverse": True},
    }
    records = [
        _snake_case_exporter_record(_record(0, "series", "data/Scarlet & Violet.ts", serie)),
        _snake_case_exporter_record(_record(1, "set", "data/Scarlet & Violet/Test.ts", card_set)),
        _snake_case_exporter_record(_record(2, "card", "data/Scarlet & Violet/Test/001.ts", card)),
    ]
    jsonl = tmp_path / "source.jsonl"
    jsonl.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    database = tmp_path / "catalogue.sqlite"
    import_jsonl(database, jsonl, "fixture-commit")
    import_jsonl(database, jsonl, "fixture-commit")
    with sqlite3.connect(database) as connection:
        assert connection.execute("select count(*) from card_printing").fetchone()[0] == 1
        assert connection.execute("select count(*) from card_variant").fetchone()[0] == 2
        assert connection.execute("select count(*) from source_record").fetchone()[0] == 3


def test_image_candidates_do_not_claim_a_physical_finish(tmp_path: Path) -> None:
    serie = {"id": "sv", "name": {"en": "Scarlet & Violet"}}
    card_set = {
        "id": "sv-test", "name": {"en": "Test"}, "serie": serie,
        "cardCount": {"official": 1}, "releaseDate": "2026-01-01",
    }
    card = {
        "set": card_set, "name": {"en": "Pikachu"}, "category": "Pokemon",
        "rarity": "Common", "variants": [{"type": "normal"}, {"type": "reverse"}],
    }
    records = [
        _snake_case_exporter_record(_record(0, "series", "data/Scarlet & Violet.ts", serie)),
        _snake_case_exporter_record(_record(1, "set", "data/Scarlet & Violet/Test.ts", card_set)),
        _snake_case_exporter_record(_record(2, "card", "data/Scarlet & Violet/Test/001.ts", card)),
    ]
    jsonl = tmp_path / "source.jsonl"
    jsonl.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    database = tmp_path / "catalogue.sqlite"
    import_jsonl(database, jsonl, "fixture-commit")

    counters = add_image_candidates(database)

    assert counters["image_candidates_added"] == 2
    assert counters["depiction_variants_added"] == 1
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """select cv.variant_key, cic.source_url, cic.rights_status
            from card_image_candidate cic join card_variant cv on cv.id=cic.card_variant_id
            where cic.image_role='display'"""
        ).fetchone()
    assert row == ("depiction-unspecified", "https://assets.tcgdex.net/en/sv/sv-test/001/high.webp", "permission_pending")
