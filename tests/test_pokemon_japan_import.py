import json
import sqlite3

from cardscanr_worldwide.pokemon_japan_import import import_checkpoint, normalized_code, normalized_number
from cardscanr_worldwide.schema import connect


def test_japan_official_identity_normalization() -> None:
    assert normalized_code("SV-P") == "SVP"
    assert normalized_code("sv8a") == "SV8A"
    assert normalized_number("001") == "1"
    assert normalized_number("SV-P") == "SV-P"


def test_japan_official_checkpoint_import(tmp_path) -> None:
    checkpoint = tmp_path / "checkpoint.sqlite"
    source = sqlite3.connect(checkpoint)
    source.executescript("""
    create table collector_runs(id text,mode text,status text,started_at text,completed_at text,counters_json text,error text);
    create table cards(provider_record_id text primary key,source_url text,local_name text,thumbnail_url text,
      parsed_json text,raw_sha256 text,status text,error text,updated_at text);
    insert into collector_runs values ('run','full','completed','now','now','{}',null);
    """)
    parsed = {
        "local_name": "シェイミ", "set_code": "MEM", "set_names": ["スタートデッキ100"],
        "collector_number": "001", "printed_total": "017", "rarity": None, "illustrator": "Artist",
        "stage": "たね", "hp": 80, "types": ["grass"],
        "attacks": [{"name": "ワザ", "cost": ["grass"], "damage": "20", "effect": "effect"}],
        "abilities": [{"name": "特性", "effect": "ability"}], "retreat_cost": ["none"],
        "weaknesses": [], "resistances": [], "national_pokedex_numbers": [492], "description": "flavour",
        "image_url": "https://www.pokemon-card.com/assets/images/card_images/large/MEM/card.jpg",
    }
    source.execute("insert into cards values (?,?,?,?,?,?,?,?,?)", (
        "50452", "https://www.pokemon-card.com/card-search/details.php/card/50452/regu/all", "シェイミ",
        "https://www.pokemon-card.com/thumb.jpg", json.dumps(parsed, ensure_ascii=False), "sha", "parsed", None, "now",
    ))
    source.commit(); source.close()
    database = tmp_path / "catalogue.sqlite"
    connect(str(database)).close()
    result = import_checkpoint(database, checkpoint)
    assert result["created_printings"] == 1
    assert result["image_candidates"] == 1
    connection = sqlite3.connect(database)
    assert connection.execute("select count(*) from attack").fetchone()[0] == 1
    assert connection.execute("select count(*) from ability").fetchone()[0] == 1


def test_japan_parsed_card_without_set_code_is_not_reported_as_uncollected(tmp_path) -> None:
    checkpoint = tmp_path / "checkpoint.sqlite"
    source = sqlite3.connect(checkpoint)
    source.executescript("""
    create table collector_runs(id text,mode text,status text,started_at text,completed_at text,counters_json text,error text);
    create table cards(provider_record_id text primary key,source_url text,local_name text,thumbnail_url text,
      parsed_json text,raw_sha256 text,status text,error text,updated_at text);
    insert into collector_runs values ('run','full','completed','now','now','{}',null);
    """)
    parsed = {
        "local_name": "Basic Energy", "set_code": None, "set_names": [],
        "collector_number": "", "printed_total": None, "image_url": None,
    }
    source.execute("insert into cards values (?,?,?,?,?,?,?,?,?)", (
        "37742", "https://www.pokemon-card.com/card-search/details.php/card/37742/regu/all",
        "Basic Energy", None, json.dumps(parsed), "sha", "parsed", None, "now",
    ))
    source.commit(); source.close()
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute(
        "insert into unresolved_item values ('old','source_card','37742','ja','JP',"
        "'official_detail_not_collected','x','{}','open',0)"
    )
    connection.commit(); connection.close()

    result = import_checkpoint(database, checkpoint)

    assert result["cards_without_set_code"] == 1
    connection = sqlite3.connect(database)
    assert connection.execute("select status from unresolved_item where id='old'").fetchone()[0] == "resolved"
    row = connection.execute(
        "select issue_class,status from unresolved_item where issue_class='official_set_membership_unavailable'"
    ).fetchone()
    assert row == ("official_set_membership_unavailable", "documented_exhausted")
