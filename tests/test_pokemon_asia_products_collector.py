from cardscanr_worldwide.collectors.pokemon_asia_products import parse_index, parse_product_page, product_type


def test_parse_product_gallery_index() -> None:
    html = (
        '<a href="/id/archives/8863/">Product</a>'
        '<a href="/id/archive/special/card/s10/">Special</a>'
        '<a href="/id/archive/card/sun_moon_series/gx_starter_deck.html">Legacy</a>'
        '<a href="/id/card-search/">Cards</a>'
        '<a href="https://tcg.pokemon.com/en-us/expansions/151/">US</a>'
    )
    assert parse_index(html, "id", "https://asia.pokemon-card.com/id/products/") == [
        "https://asia.pokemon-card.com/id/archive/card/sun_moon_series/gx_starter_deck.html",
        "https://asia.pokemon-card.com/id/archive/special/card/s10/",
        "https://asia.pokemon-card.com/id/archives/8863/",
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


def test_parse_named_product_info_and_image_label_box() -> None:
    html = """
    <section><img src="./product-main.png"><div class="product-info">
      <h3 class="product-name">Booster Pack Alpha</h3><ul class="product-detail"><li>5 cards</li></ul>
    </div></section>
    <div class="box-product"><h3 class="box-product-head"><img alt="Special Collection"></h3>
      <div class="lyt-block2-pack"><img src="./box.png"></div>
      <div class="lyt-block2-content"><img alt="Contents: Booster Pack x4"></div></div>
    """
    rows = parse_product_page(html, "https://example.test/products/")
    assert [row["local_name"] for row in rows] == ["Booster Pack Alpha", "Special Collection"]
    assert rows[1]["metadata"]["template"] == "image_label_product_box"


def test_parse_product_information_and_legacy_lyt_product() -> None:
    html = """
    <div class="eyecatch"><img src="/hero.png"></div>
    <div class="product-information"><h2>Product</h2><p>Battle Deck Alpha</p>
      <table><tr><th>Contents</th><td><ul><li>60 cards</li></ul></td></tr></table></div>
    <div class="lyt-product"><div class="lyt-product-image"><img src="/classic.png"></div>
      <div class="lyt-product-content">●商品名稱：寶可夢集換式卡牌遊戲 Classic ●建議零售價：2000元</div></div>
    """
    rows = parse_product_page(html, "https://example.test/product/")
    assert rows[0]["metadata"]["contents"] == ["60 cards"]
    assert rows[1]["local_name"].endswith("Classic")


def test_parse_article_detail_card_archive_page() -> None:
    html = """
    <article class="article-detail article-detail--card">
      <h1 class="article-detail__title">Booster Pack Seri Pertama (Set A)</h1>
      <figure class="article-detail__mv"><img src="/id/archive/pack.png"></figure>
      <div class="article-detail__content"><p>Booster pack for advanced players.</p>
        <table class="article-detail__information-table">
          <tr><th>Seri:</th><td>Seri Sun &amp; Moon</td></tr>
          <tr><th>Jumlah Kartu:</th><td>Lebih dari 150</td></tr>
        </table>
      </div>
    </article>
    """
    rows = parse_product_page(html, "https://asia.pokemon-card.com/id/archive/card/sun_moon_series/1st_booster_pack_seta.html")
    assert len(rows) == 1
    assert rows[0]["local_name"] == "Booster Pack Seri Pertama (Set A)"
    assert rows[0]["product_type"] == "booster_pack"
    assert rows[0]["image_url"].endswith("/id/archive/pack.png")
    assert rows[0]["metadata"]["template"] == "article_detail_card"
    assert rows[0]["metadata"]["fields"]["Seri:"] == "Seri Sun & Moon"
