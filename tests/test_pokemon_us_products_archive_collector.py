import json

from cardscanr_worldwide.collectors.pokemon_us_products_archive import parse_cdx, parse_product


def test_us_product_archive_parsers() -> None:
    cdx = json.dumps([
        ["timestamp", "original", "digest", "statuscode"],
        ["20260101000000", "https://www.pokemon.com/us/pokemon-tcg/product-gallery/test-box/", "digest", "200"],
        ["20260101000000", "https://www.pokemon.com/us/pokemon-tcg/product-gallery/test-box/decks/", "digest2", "200"],
    ]).encode()
    assert [row["provider_record_id"] for row in parse_cdx(cdx)] == ["test-box"]
    html = """<h1 class="us-title">Pokémon TCG: Test Box</h1>
    <div class="date generic-date">Launch: August 03, 2016</div>
    <div class="full-article-body"><p>Description.</p><p>Inside:</p><ul class="list">
    <li><p>8 booster packs</p></li><li><p>65 card sleeves</p></li></ul></div>
    <img src="/static-assets/content-assets/cms2/img/trading-card-game/test-box.jpg" alt="Test">"""
    result = parse_product(html, "https://www.pokemon.com/us/pokemon-tcg/product-gallery/test-box/", "test-box")
    assert result["local_name"] == "Pokémon TCG: Test Box"
    assert result["release_date_text"] == "August 03, 2016"
    assert result["contents"] == ["8 booster packs", "65 card sleeves"]
    assert result["images"][0]["canonical_url"].endswith("/test-box.jpg")
