from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


class MockProviderTests(unittest.TestCase):
    def test_mock_provider_is_deterministic(self) -> None:
        provider = MockMarketCompsProvider()
        first = provider.fetch_comps(sample_price_key())
        second = provider.fetch_comps(sample_price_key())

        self.assertEqual(first.provider_fingerprint, second.provider_fingerprint)
        self.assertEqual(
            [(item.source_listing_id, item.title, item.total_price) for item in first.comps],
            [(item.source_listing_id, item.title, item.total_price) for item in second.comps],
        )

    def test_mock_provider_changes_with_fingerprint(self) -> None:
        provider = MockMarketCompsProvider()
        first = provider.fetch_comps(sample_price_key(fingerprint="a"))
        second = provider.fetch_comps(sample_price_key(fingerprint="b"))

        self.assertNotEqual(first.provider_fingerprint, second.provider_fingerprint)
        self.assertNotEqual(first.comps[0].total_price, second.comps[0].total_price)


if __name__ == "__main__":
    unittest.main()
