import json

from cardscanr_worldwide.collectors.pokemon_japan_products import parse_page, product_identity


def test_japan_product_feed_parser_and_identity() -> None:
    product = {
        "productTitle": "拡張パック「テスト」", "productType": "拡張パック",
        "tumbsImg": "/products/2026/images/test.jpg", "releaseDate": "2026年 7月31日（金）",
        "priceTxt": "200円（税込）", "description": "カード5枚入り",
        "link_cardList": "/card-search/index.php?pg=1", "link_detailPage": "/ex/test/",
    }
    data = parse_page(json.dumps({
        "result": 1, "thisPage": 1, "maxPage": 1, "hitCnt": 1, "products": [product],
    }, ensure_ascii=False).encode())
    assert data["products"][0]["priceTxt"] == "200円（税込）"
    assert len(product_identity(product)) == 24
    assert product_identity(product) == product_identity(dict(reversed(list(product.items()))))
