import json

from cardscanr_worldwide.collectors.pokemon_korea_archive import canonical_url, parse_card, parse_cdx


def test_korea_archive_cdx_and_card_parser() -> None:
    payload = json.dumps([
        ["timestamp", "original", "digest", "statuscode"],
        ["20260107185537", "https://pokemoncard.co.kr/cards/detail/SVP000000203", "digest", "200"],
    ]).encode()
    rows = parse_cdx(payload)
    assert rows[0]["provider_record_id"] == "SVP000000203"
    assert rows[0]["replay_url"].endswith("id_/https://pokemoncard.co.kr/cards/detail/SVP000000203")
    html = """<div id="heaer_top"><img class="feature_image"
    src="https://cards.image.pokemonkorea.co.kr/data/wmimages/SV/SV-P/SV-P_203.png?w=512">
    <div class="pre_info_wrap"><img src="symbol/promo.png"><img src="symbol/I.png">
    <span class="p_num">203/SV-P<span id="no_wrap_by_admin"> AR </span></span></div>
    <p class="illustrator">일러스트<br>Amelicart</p>
    <div class="header"><span class="card-hp title">비크티니</span><span class="hp_num">HP80</span>
    <img title="불꽃"></div><div class="pokemon-info">카드 종류 : 기본 포켓몬</div>
    <div class="pokemon-abilities"><div class="ability"><div class="area-parent"><img title="불꽃">
    <h4 class="label"><span class="skil_name">V포스</span><span class="plus">120</span></h4></div>
    <p>기술 설명</p></div></div><div class="pokemon-stats"><div class="stat"><h4>약점</h4>
    <img title="물"><span>×2</span></div><div class="stat" title="후퇴 : 1"><h4>후퇴</h4></div></div>
    <div class="pokemon-detail"><a class="search_href">블랙볼트</a></div>
    <div class="pokemon-detail"><div class="col-md-4">No. 494</div><div class="colsit"><p>설명</p></div></div></div>"""
    card = parse_card(html, "https://pokemoncard.co.kr/cards/detail/SVP000000203", "SVP000000203")
    assert card["set_code"] == "SV-P"
    assert card["collector_number"] == "203"
    assert card["rarity"] == "AR"
    assert card["local_name"] == "비크티니"
    assert card["hp"] == 80
    assert card["attacks"][0] == {"name": "V포스", "cost": ["불꽃"], "damage": "120", "effect": "기술 설명"}
    assert card["national_pokedex_numbers"] == [494]
    assert card["regulation_mark"] == "I"
    assert card["image_url"].endswith("/SV-P_203.png")
    assert canonical_url("//cards.example/x.png?w=512") == "https://cards.example/x.png"


def test_korea_archive_cdx_excludes_logout_navigation_capture() -> None:
    payload = json.dumps([
        ["timestamp", "original", "digest", "statuscode"],
        ["20260101000000", "https://pokemoncard.co.kr/cards/detail/logout", "d", "200"],
    ]).encode()
    assert parse_cdx(payload) == []
