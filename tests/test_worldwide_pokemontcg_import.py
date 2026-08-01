from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from cardscanr_worldwide.pokemontcg import import_repository
from cardscanr_worldwide.tcgdex import import_jsonl


def _tcgdex_jsonl(path: Path) -> None:
    serie = {"id": "sv", "name": {"en": "Scarlet & Violet"}}
    card_set = {
        "id": "sv-test", "name": {"en": "Test Set"}, "serie": serie,
        "cardCount": {"official": 1}, "releaseDate": "2026-01-01",
    }
    card = {
        "set": card_set, "name": {"en": "Pikachu"}, "category": "Pokemon",
        "rarity": "Common", "dexId": [25], "variants": [{"type": "normal"}],
    }
    rows = []
    for index, (kind, source_path, payload) in enumerate([
        ("series", "data/Scarlet & Violet.ts", serie),
        ("set", "data/Scarlet & Violet/Test Set.ts", card_set),
        ("card", "data/Scarlet & Violet/Test Set/001.ts", card),
    ]):
        rows.append({
            "schema_version": 1, "index": index, "source_path": source_path,
            "source_domain": "international", "record_type": kind,
            "provider_record_id": source_path.removesuffix(".ts"),
            "source_byte_size": 1, "source_sha256": hashlib.sha256(source_path.encode()).hexdigest(),
            "payload": payload,
        })
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _pokemon_repo(root: Path) -> None:
    (root / "sets").mkdir(parents=True)
    (root / "cards/en").mkdir(parents=True)
    (root / "decks/en").mkdir(parents=True)
    sets = [{
        "id": "svtest", "name": "Test Set", "series": "Scarlet & Violet",
        "printedTotal": 1, "total": 1, "releaseDate": "2026/01/01",
        "images": {"symbol": "https://example.invalid/symbol.png"},
    }]
    cards = [{
        "id": "svtest-1", "name": "Pikachu", "supertype": "Pokémon",
        "subtypes": ["Basic"], "hp": "60", "types": ["Lightning"], "number": "001",
        "rarity": "Common", "nationalPokedexNumbers": [25],
        "attacks": [{"name": "Zap", "cost": ["Lightning"], "damage": "10", "text": "Zap."}],
        "images": {"small": "https://example.invalid/1.png", "large": "https://example.invalid/1-hi.png"},
    }]
    decks = [{
        "id": "d-svtest-1", "name": "Pikachu Theme Deck", "types": ["Lightning"],
        "cards": [{"id": "svtest-1", "name": "Pikachu", "rarity": "Common", "count": 2}],
    }]
    (root / "sets/en.json").write_text(json.dumps(sets), encoding="utf-8")
    (root / "cards/en/svtest.json").write_text(json.dumps(cards), encoding="utf-8")
    (root / "decks/en/svtest.json").write_text(json.dumps(decks), encoding="utf-8")


def test_pokemontcg_import_preserves_images_decks_and_crosswalks(tmp_path: Path) -> None:
    database = tmp_path / "catalogue.sqlite"
    tcgdex = tmp_path / "tcgdex.jsonl"
    _tcgdex_jsonl(tcgdex)
    import_jsonl(database, tcgdex, "tcgdex-fixture")
    source = tmp_path / "pokemon-tcg-data"
    _pokemon_repo(source)

    counters = import_repository(database, source, "pokemon-fixture")

    assert counters["cards"] == 1
    assert counters["decks"] == 1
    assert counters["set_crosswalk_candidate"] == 1
    assert counters["card_crosswalk_candidate"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("select count(*) from card_image_candidate").fetchone()[0] == 2
        assert connection.execute("select count(*) from sealed_product").fetchone()[0] == 1
        assert connection.execute("select quantity from product_content").fetchone() == (2,)
        rights = connection.execute("select distinct rights_status from card_image_candidate").fetchall()
        assert rights == [("permission_pending",)]
