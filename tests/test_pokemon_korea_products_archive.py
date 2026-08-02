from cardscanr_worldwide.collectors.pokemon_korea_products_archive import parse_category, parse_product
from cardscanr_worldwide.pokemon_korea_products_archive_import import (
    classify_product,
    content_normalization,
)


def test_korean_product_category_and_detail_parsing() -> None:
    category = """
    <article class='white-panel'><div class='point' onclick="location.href='/card/758'">
      <img src='https://data1.pokemonkorea.co.kr/product.png' alt='상자'/>
      <h4>포켓몬 카드 게임 스칼렛&amp;바이올렛 「Pikachu Present Box」</h4>
    </div></article>
    """
    assert parse_category(category, "info3") == [{
        "provider_record_id": "758", "category_key": "info3",
        "local_name": "포켓몬 카드 게임 스칼렛&바이올렛 「Pikachu Present Box」",
        "listing_image_url": "https://data1.pokemonkorea.co.kr/product.png",
        "listing_image_alt": "상자",
    }]
    detail = """
    <h3 class='medium-title'>포켓몬 카드 게임 스칼렛&amp;바이올렛 「Pikachu Present Box」</h3>
    <ul><li><b>발매일</b>2025-04-18</li><li><b>가격</b>28,000원</li>
    <li><b>구성물</b>1. 확장팩 「초전브레이커」 --- 10팩<br/>2. 프로모 카드 「피카츄」 --- 1장</li></ul>
    <img src='https://data1.pokemonkorea.co.kr/detail.png'/>
    """
    parsed = parse_product(detail, "https://pokemoncard.co.kr/card/758", "758")
    assert parsed["release_date"] == "2025-04-18"
    assert parsed["price_krw"] == 28000
    assert parsed["contents"] == ["1. 확장팩 「초전브레이커」 --- 10팩", "2. 프로모 카드 「피카츄」 --- 1장"]
    assert parsed["images"][0]["canonical_url"] == "https://data1.pokemonkorea.co.kr/detail.png"


def test_korean_product_classification_and_contents() -> None:
    assert classify_product("포켓몬 카드 게임 카드 실드 「피카츄」", ["info3"]) == ("accessory_product", "sleeves")
    assert classify_product("MEGA 확장팩 「닌자스피너」", ["info1"]) == ("booster_pack", None)
    assert classify_product("스타터 세트 MEGA 「메가팬텀 ex」", ["info2"]) == ("starter_deck", None)
    assert content_normalization("1. 확장팩 --- 10팩") == ("booster_pack", 10)
    assert content_normalization("2. 프로모 카드 --- 1장") == ("promotional_card", 1)

