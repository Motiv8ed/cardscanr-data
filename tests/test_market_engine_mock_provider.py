from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.models import MarketPriceKey
from cardscanr_market_engine.providers.mock_provider import MockMarketCompsProvider


def sample_price_key(*, fingerprint: str = "pokemon|en|base1|4|charizard|raw|near_mint|us|usd") -> MarketPriceKey:
    return MarketPriceKey(
        id="key-1",
        game="pokemon",
        card_name="Charizard",
        normalized_card_name="charizard",
        set_name="Base Set",
        set_code="base1",
        collector_number="4",
        language="en",
        variant="raw",
        condition="near_mint",
        market_country="us",
        currency="usd",
        fingerprint=fingerprint,
    )


def request_for(*, country: str, currency: str, fingerprint: str) -> object:
    from cardscanr_market_engine.models import ProviderRequest

    key = sample_price_key(fingerprint=fingerprint)
    market = resolve_marketplace_config(market_country=country, currency=currency, marketplace="ebay")
    return ProviderRequest(
        price_key=key,
        market_country=market.market_country,
        currency=market.currency,
        marketplace=market.marketplace,
        provider_marketplace_id=market.provider_marketplace_id,
        provider_domain=market.provider_domain,
        search_locale=market.search_locale,
        display_name=market.display_name,
        market_config=market,
    )


class MockProviderTests(unittest.TestCase):
    def test_mock_provider_is_deterministic(self) -> None:
        provider = MockMarketCompsProvider()
        request = request_for(country="us", currency="usd", fingerprint="pokemon|en|base1|4|charizard|raw|near_mint|us|usd")
        first = provider.fetch_comps(request)
        second = provider.fetch_comps(request)

        self.assertEqual(first.provider_fingerprint, second.provider_fingerprint)
        self.assertEqual(
            [(item.source_listing_id, item.title, item.total_price) for item in first.comps],
            [(item.source_listing_id, item.title, item.total_price) for item in second.comps],
        )

    def test_mock_provider_changes_with_fingerprint(self) -> None:
        provider = MockMarketCompsProvider()
        first = provider.fetch_comps(request_for(country="us", currency="usd", fingerprint="a"))
        second = provider.fetch_comps(request_for(country="us", currency="usd", fingerprint="b"))

        self.assertNotEqual(first.provider_fingerprint, second.provider_fingerprint)
        self.assertNotEqual(first.comps[0].total_price, second.comps[0].total_price)

    def test_mock_provider_uses_requested_currency_and_domain(self) -> None:
        provider = MockMarketCompsProvider()
        au = provider.fetch_comps(
            request_for(
                country="au",
                currency="aud",
                fingerprint="pokemon|en|base1|4|charizard|raw|near_mint|au|aud",
            )
        )
        self.assertEqual(au.marketplace, "EBAY_AU")
        self.assertEqual(au.comps[0].currency, "AUD")
        self.assertIn("https://www.ebay.com.au/itm/mock-", au.comps[0].listing_url)
        self.assertEqual(au.raw_metadata["providerDomain"], "ebay.com.au")
        self.assertEqual(au.raw_metadata["displayName"], "Australia")

    def test_same_card_differs_between_markets(self) -> None:
        provider = MockMarketCompsProvider()
        au = provider.fetch_comps(
            request_for(
                country="au",
                currency="aud",
                fingerprint="pokemon|en|base1|4|charizard|raw|near_mint|au|aud",
            )
        )
        us = provider.fetch_comps(
            request_for(
                country="us",
                currency="usd",
                fingerprint="pokemon|en|base1|4|charizard|raw|near_mint|us|usd",
            )
        )
        self.assertNotEqual(au.provider_fingerprint, us.provider_fingerprint)
        self.assertNotEqual(au.comps[0].total_price, us.comps[0].total_price)
        self.assertNotEqual(au.comps[0].listing_url, us.comps[0].listing_url)


if __name__ == "__main__":
    unittest.main()
