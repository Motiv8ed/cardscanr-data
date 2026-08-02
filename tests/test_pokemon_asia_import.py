import json
import sqlite3
from pathlib import Path

from cardscanr_worldwide.collectors.pokemon_asia import SCHEMA
from cardscanr_worldwide.pokemon_asia_import import import_checkpoint, product_release_date, product_type


def test_official_product_classification_and_date() -> None:
    name = 'Evolusi Mega Booster Pack "Ancaman Bayangan" Tanggal Penjualan 06-26-2026'
    assert product_type(name) == "booster_pack"
    assert product_release_date(name) == "2026-06-26"
    assert product_type("朱＆紫 ex初階牌組 皮卡丘 發售日 07-18-2025") == "starter_deck"


def test_completed_checkpoint_imports_official_product_card_and_image(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.sqlite"
    with sqlite3.connect(checkpoint) as source:
        source.executescript(SCHEMA)
        source.execute(
            "insert into products values ('id','M-P','Evolusi Mega Kartu Promo Tanggal Penjualan 09-19-2025',?,'now')",
            ("https://asia.pokemon-card.com/id/card-search/",),
        )
        source.execute(
            "insert into product_cards values ('id','M-P','16614',?)",
            ("https://asia.pokemon-card.com/id/card-search/list/?expansionCodes=M-P",),
        )
        parsed = {
            "local_name": "Bulbasaur", "stage": "basic", "hp": 80, "types": ["Grass"],
            "attacks": [{"name": "Menjerat", "cost": ["Grass"], "damage": "10", "effect": "Text"}],
            "weaknesses": [{"type": "Fire", "value": "×2"}], "resistances": [],
            "retreat_cost": ["Colorless"], "regulation_mark": "I", "collector_number": "001",
            "printed_set_code": "M-P", "national_pokedex_numbers": [1], "illustrator": "HYOGONOSUKE",
            "description": "Text", "image_url": "https://asia.pokemon-card.com/id/card-img/id00016614.png",
        }
        source.execute(
            "insert into cards values ('id','16614',?,?,?,?,?,'parsed','now')",
            ("https://asia.pokemon-card.com/id/card-search/detail/16614/", "Bulbasaur",
             parsed["image_url"], json.dumps(parsed), "a" * 64),
        )
        source.execute(
            "insert into collector_runs(id,locale,mode,status,started_at,completed_at,counters_json) values ('run','id','full','completed','now','now','{}')"
        )
    database = tmp_path / "worldwide.sqlite"

    counters = import_checkpoint(database, checkpoint, "id")

    assert counters == {"products": 1, "cards": 1}
    with sqlite3.connect(database) as connection:
        assert connection.execute("select count(*) from sealed_product").fetchone()[0] == 1
        assert connection.execute("select count(*) from card_printing").fetchone()[0] == 1
        assert connection.execute("select rights_status from card_image_candidate").fetchone() == ("link_only",)
        assert json.loads(connection.execute("select checkpoint_json from import_run").fetchone()[0]) == {"complete": True}
        assert connection.execute("pragma foreign_key_check").fetchall() == []
