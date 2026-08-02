from cardscanr_worldwide.collectors.pokemon_asia_products import parse_index, parse_product_page, product_type


def test_parse_product_gallery_index() -> None:
    html = '<a href="/id/archives/8863/">Product</a><a href="/id/card-search/">Cards</a>'
    assert parse_index(html, "id", "https://asia.pokemon-card.com/id/products/") == [
        "https://asia.pokemon-card.com/id/archives/8863/"
    ]


def test_parse_article_product_cards() -> None:
    html = """
    <div class="card wide"><div class="left"><img src="/pkg.png"></div><div class="right">
      <h4 class="mb-24px">Booster Pack Test</h4><table>
      <tr><th>Price</th><td>20,000 IDR</td></tr>
      <tr><th>Contents</th><td><ul><li>5 random cards</li></ul></td></tr></table></div></div>
    """
    rows = parse_product_page(html, "https://asia.pokemon-card.com/id/archives/1/")
    assert len(rows) == 1
    assert rows[0]["image_url"] == "https://asia.pokemon-card.com/pkg.png"
    assert rows[0]["product_type"] == "booster_pack"
    assert rows[0]["metadata"]["contents"] == ["5 random cards"]


def test_parse_special_product_column() -> None:
    html = """
    <div class="lyt-column--product"><img src="./assets/product.png">
      <div class="text-product">Special Collection</div>
      <ul class="product-list"><li>Booster Pack x2</li></ul></div>
    """
    rows = parse_product_page(html, "https://asia.pokemon-card.com/hk/archive/special/card/m1/")
    assert rows[0]["product_type"] == "collection_box"
    assert rows[0]["metadata"]["contents"] == ["Booster Pack x2"]
    assert product_type("Deck Box") == "deck_box"


def test_parse_legacy_product_group() -> None:
    html = """
    <div class="lyt-group lyt-group--product"><div class="lyt-group-content">
      <h3 class="lyt-group-text">Booster Pack Alpha</h3><p>Contents: 5 cards</p></div>
      <div class="lyt-group-image"><img src="./assets/product.png"></div></div>
    """
    rows = parse_product_page(html, "https://asia.pokemon-card.com/id/archive/special/card/s10/")
    assert rows[0]["local_name"] == "Booster Pack Alpha"
    assert rows[0]["metadata"]["template"] == "legacy_product_group"
