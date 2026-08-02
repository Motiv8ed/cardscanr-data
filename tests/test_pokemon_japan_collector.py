import json

from cardscanr_worldwide.collectors.pokemon_japan import canonical_url, parse_card, parse_result_page


def test_japan_inventory_and_detail_parsers() -> None:
    result = parse_result_page(json.dumps({
        "result": 1, "maxPage": 1, "hitCnt": 1,
        "cardList": [{"cardID": "50452", "cardThumbFile": "/thumb.jpg", "cardNameViewText": "シェイミ"}],
    }).encode())
    assert result["hitCnt"] == 1
    html = """<div class="PopupMain"><section class="Section"><h1 class="Heading1">シェイミ</h1>
    <div class="LeftBox"><img class="fit" src="/assets/images/card_images/large/MEM/card.jpg">
    <div class="subtext"><img class="img-regulation" alt="MEM"><span> 001 / 017 </span></div>
    <div class="card"><h4>No.492</h4><p>height</p><hr><p>flavour</p></div>
    <div class="author"><a>HYOGONOSUKE</a></div></div><div class="RightBox-inner">
    <div class="TopInfo"><span class="type">たね</span><span class="hp-num">80</span>
    <span class="icon-grass icon"></span></div><h2>ワザ</h2><h4><span class="icon-grass icon"></span>
    かけぬける<span class="f_right">20</span></h4><p>effect</p><table><tr><th>弱点</th></tr>
    <tr><td><span class="icon-fire icon"></span>×2</td><td>--</td><td><span class="icon-none icon"></span></td></tr>
    </table></div></section></div><div class="PopupSub"><li class="List_item"><a>商品名</a></li></div>"""
    card = parse_card(html, "50452")
    assert card["local_name"] == "シェイミ"
    assert card["set_code"] == "MEM"
    assert card["collector_number"] == "001"
    assert card["printed_total"] == "017"
    assert card["hp"] == 80
    assert card["attacks"][0] == {"name": "かけぬける", "cost": ["grass"], "damage": "20", "effect": "effect"}
    assert card["weaknesses"][0]["types"] == ["fire"]
    assert card["retreat_cost"] == ["none"]
    assert card["national_pokedex_numbers"] == [492]
    assert card["image_url"] == "https://www.pokemon-card.com/assets/images/card_images/large/MEM/card.jpg"
    assert canonical_url("/x.jpg?size=1").endswith("/x.jpg")
