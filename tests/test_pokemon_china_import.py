from cardscanr_worldwide.pokemon_china_import import accessory_type, classify_product, parse_msrp


def test_china_product_normalization_is_conservative() -> None:
    assert classify_product("共逐荣光", "补充包 5张装") == "booster_pack"
    assert classify_product("大师战略卡组构筑套装", None) == "trainer_toolkit"
    assert accessory_type("伊布礼盒", "卡套64张") == "sleeves"
    assert parse_msrp("5张装：10元/包 20张装：50元/包") == 10.0
