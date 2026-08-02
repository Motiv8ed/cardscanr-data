from cardscanr_worldwide.collectors.pokemon_china_products import parse_index, parse_product, unsigned_image_url


def test_china_product_parsers_preserve_spec_and_unsigned_image_path() -> None:
    index = '<a href="https://www.pokemon.cn/tcg/product/22630.html">Product</a>'
    products, articles = parse_index(index)
    assert products == ["https://www.pokemon.cn/tcg/product/22630.html"]
    assert articles == products
    legacy, _ = parse_index('<a href="https://www.pokemon.cn/tcg/17555.html">Legacy</a>', product_category=True)
    assert legacy == ["https://www.pokemon.cn/tcg/17555.html"]
    detail = """<html><head><title>公告 | The official Pokémon Website in China</title></head>
    <body><article>商品名：宝可梦卡牌 测试礼盒 发售日 2026年7月16日10时起
    建议零售价 88元 商品内容 卡组（60张）1套 购买渠道 官方店
    <img src="https://image.pokemon.com.cn/wp-content/uploads/2026/06/a.png?auth_key=temporary"></article></body></html>"""
    parsed = parse_product(detail, "https://www.pokemon.cn/tcg/product/22630.html")
    assert parsed["local_name"] == "宝可梦卡牌 测试礼盒"
    assert parsed["release_date"] == "2026-07-16"
    assert parsed["msrp_text"] == "88元"
    assert parsed["provider_record_id"] == "22630"
    assert parsed["images"][0]["canonical_url"].endswith("/a.png")
    assert unsigned_image_url(parsed["images"][0]["source_url"]).endswith("/a.png")
