from cardscanr_worldwide.collectors.pokemon_asia import parse_card_detail, parse_card_list, parse_product_index


def test_product_index_parser_keeps_codes_and_local_names() -> None:
    html = """
    <a href="/id/card-search/list/?expansionCodes=SV8a">Festival Terastal ex</a>
    <a href="/id/card-search/?pageNo=2">Next</a>
    """
    products, has_next = parse_product_index(html, "id")
    assert products == [{"code": "SV8a", "local_name": "Festival Terastal ex",
                         "href": "/id/card-search/list/?expansionCodes=SV8a"}]
    assert has_next


def test_card_list_parser_deduplicates_details() -> None:
    html = """
    <a href="/th/card-search/detail/123/"><img></a>
    <a href="/th/card-search/detail/123/">duplicate</a>
    <a href="/th/card-search/detail/124/">card</a>
    <a href="/th/card-search/list/?pageNo=2&amp;expansionCodes=MA5">Next</a>
    """
    cards, has_next = parse_card_list(html, "th")
    assert cards == ["123", "124"]
    assert has_next


def test_card_detail_parser_extracts_exact_locale_image() -> None:
    html = """
    <html><head><title>Bulbasaur | Trainer</title></head><body>
    <h1>basic <span>Bulbasaur</span></h1>
    <img src="/id/card-img/id00016614.png"><img src="/id/card-img/mark/PROMO.MARK.png">
    </body></html>
    """
    value = parse_card_detail(html, "https://asia.pokemon-card.com/id/card-search/detail/16614/")
    assert value["local_name"] == "basic Bulbasaur"
    assert value["image_url"] == "https://asia.pokemon-card.com/id/card-img/id00016614.png"
