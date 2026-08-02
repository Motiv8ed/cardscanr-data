from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cardscanr_worldwide.asia_shared_details import hydrate_shared_details


SCHEMA = """
create table product_cards(locale text,expansion_code text,card_id text,source_url text);
create table cards(locale text,card_id text,source_url text,local_name text,image_url text,parsed_json text,
 raw_sha256 text,status text,updated_at text,primary key(locale,card_id));
create table collector_runs(id text,locale text,mode text,status text,started_at text,completed_at text,
 counters_json text,error text);
"""


def checkpoint(path: Path, locale: str, parsed: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute(
        "insert into collector_runs values ('r',?,'inventory','completed','n','n','{}',null)", (locale,)
    )
    for card_id in ("1", "2"):
        connection.execute(
            "insert into product_cards values (?,'set',?,?)",
            (locale, card_id, f"https://asia.pokemon-card.com/{locale}/card-search/list/"),
        )
        if parsed:
            payload = json.dumps({
                "local_name": f"Card {card_id}",
                "page_url": f"https://asia.pokemon-card.com/{locale}/card-search/detail/{card_id}/",
                "image_url": f"https://asia.pokemon-card.com/{locale}/card-img/default{card_id}.png",
            })
            connection.execute(
                "insert into cards values (?,?,?,?,?,?,?,'parsed','n')",
                (locale, card_id, f"https://asia.pokemon-card.com/{locale}/card-search/detail/{card_id}/",
                 f"Card {card_id}", f"https://asia.pokemon-card.com/{locale}/card-img/default{card_id}.png",
                 payload, f"sha{card_id}"),
            )
    connection.commit()
    connection.close()


def test_hydrates_identical_english_inventory_with_explicit_evidence(tmp_path: Path) -> None:
    source = tmp_path / "ph.sqlite"
    target = tmp_path / "sg.sqlite"
    checkpoint(source, "ph", parsed=True)
    checkpoint(target, "sg")
    result = hydrate_shared_details(source, target, "ph", "sg")
    assert result["copied_cards"] == 2
    connection = sqlite3.connect(target)
    row = connection.execute("select source_url,image_url,parsed_json from cards where card_id='1'").fetchone()
    assert "/sg/" in row[0] and "/sg/" in row[1]
    assert json.loads(row[2])["shared_official_detail_evidence"]["exact_card_id"] == "1"
    assert connection.execute("select count(*) from collector_runs where mode='shared_detail_hydration'").fetchone()[0] == 1


def test_rejects_nonidentical_inventory(tmp_path: Path) -> None:
    source = tmp_path / "ph.sqlite"
    target = tmp_path / "sg.sqlite"
    checkpoint(source, "ph", parsed=True)
    checkpoint(target, "sg")
    connection = sqlite3.connect(target)
    connection.execute("delete from product_cards where card_id='2'")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="inventories differ"):
        hydrate_shared_details(source, target, "ph", "sg")

