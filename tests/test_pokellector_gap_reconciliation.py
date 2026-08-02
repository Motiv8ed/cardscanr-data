from cardscanr_worldwide.pokellector_gap_reconciliation import normalized, parse_card_page, parse_set_links


def test_exact_set_and_card_page_parsers() -> None:
    links=parse_set_links('<a href="/McDonalds-Collection-2014-Expansion/Bunnelby-Card-10">Bunnelby</a>',
                          '/McDonalds-Collection-2014-Expansion/')
    assert links[10][0] == 'Bunnelby'
    title,image=parse_card_page('<meta property="og:title" content="Bunnelby - McDonalds #10">'
                                '<meta property="og:image" content="https://images.test/bunnelby.png">')
    assert normalized('Bunnelby') in normalized(title)
    assert image == 'https://images.test/bunnelby.png'
