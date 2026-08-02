import json
import sqlite3

from cardscanr_worldwide.pokemon_korea_archive_import import import_checkpoint
from cardscanr_worldwide.schema import connect


def test_korea_archive_import_creates_official_printing_and_image(tmp_path) -> None:
    checkpoint = tmp_path / "checkpoint.sqlite"
    source = sqlite3.connect(checkpoint)
    source.executescript("""
    create table collector_runs(id text,status text,started_at text,completed_at text,counters_json text,error text);
    create table cards(provider_record_id text primary key,source_url text,replay_url text,
      archive_timestamp text,archive_digest text,parsed_json text,raw_sha256 text,status text,error text,updated_at text);
    insert into collector_runs values ('run','completed','now','now','{}',null);
    """)
    parsed = {
        "provider_record_id": "SVP000000203", "source_url": "https://pokemoncard.co.kr/cards/detail/SVP000000203",
        "local_name": "비크티니", "set_code": "SV-P", "set_names": ["프로모 카드"],
        "collector_number": "203", "printed_total": "SV-P", "rarity": "AR", "illustrator": "Amelicart",
        "stage": "기본 포켓몬", "hp": 80, "types": ["불꽃"],
        "attacks": [{"name": "V포스", "cost": ["불꽃"], "damage": "120", "effect": "기술 설명"}],
        "weaknesses": [], "resistances": [], "retreat_cost": ["무색"], "regulation_mark": "I",
        "national_pokedex_numbers": [494], "description": "설명",
        "image_url": "https://cards.image.pokemonkorea.co.kr/data/wmimages/SV/SV-P/SV-P_203.png",
    }
    source.execute(
        "insert into cards values (?,?,?,?,?,?,?,?,?,?)",
        ("SVP000000203", parsed["source_url"], "https://web.archive/replay", "20260107185537", "digest",
         json.dumps(parsed, ensure_ascii=False), "sha", "parsed", None, "now"),
    )
    source.commit()
    source.close()
    database = tmp_path / "catalogue.sqlite"
    connect(str(database)).close()
    result = import_checkpoint(database, checkpoint)
    assert result["created_set_releases"] == 1
    assert result["created_printings"] == 1
    assert result["image_candidates"] == 1
    connection = sqlite3.connect(database)
    assert connection.execute(
        "select l.name,p.collector_number,p.hp,p.verification_status from card_printing p "
        "join card_localisation l on l.card_printing_id=p.id"
    ).fetchone() == ("비크티니", "203", 80, "verified")
    assert connection.execute(
        "select source_url,rights_status from card_image_candidate"
    ).fetchone() == (parsed["image_url"], "link_only")
    assert connection.execute(
        "select count(*) from provider_entity_mapping where provider_id='pokemon-korea-official-archive'"
    ).fetchone()[0] == 3
