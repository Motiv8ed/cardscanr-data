from cardscanr_worldwide.pokemon_us_products_archive_import import product_type, release_date


def test_us_product_archive_normalization() -> None:
    assert release_date("August 03, 2016") == "2016-08-03"
    assert product_type("Pokémon TCG: XY Elite Trainer Box") == "elite_trainer_box"
    assert product_type("Pokémon TCG: Zacian V League Battle Deck") == "league_battle_deck"
    assert product_type("Pokémon TCG: Collector Chest") == "collector_chest"
