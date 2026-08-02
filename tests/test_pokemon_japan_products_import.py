from cardscanr_worldwide.pokemon_japan_products_import import (
    accessory_type,
    plain_description,
    price_jpy,
    product_type,
    release_date,
)


def test_japan_product_normalization() -> None:
    assert release_date("2026年 7月31日（金）") == "2026-07-31"
    assert price_jpy("2,420円（税込）") == 2420.0
    assert product_type("拡張パック", "拡張パック「テスト」") == "booster_pack"
    assert product_type("構築デッキ", "スターターセットex") == "starter_deck"
    assert product_type("周辺グッズ", "デッキシールド ピカチュウ") == "accessory_product"
    assert accessory_type("コレクションファイルプレミアム") == "binder"
    assert accessory_type("デッキシールド ピカチュウ") == "sleeves"
    assert plain_description("カード5枚入り<br>ランダム") == "カード5枚入り\nランダム"
