"""Unit tests for eBay listing title extraction (US chrome contamination)."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.providers.ebay_browser_provider import (
    clean_candidate_title,
    extract_title_from_lines,
    is_chrome_only_title,
    parse_candidate_dict,
)
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest


def _request() -> ProviderRequest:
    key = MarketPriceKey(
        id="k1",
        game="pokemon",
        card_name="Pikachu",
        normalized_card_name="pikachu",
        set_name="Base Set",
        set_code="base1",
        collector_number="58/102",
        language="en",
        variant="raw",
        condition="raw",
        market_country="us",
        currency="usd",
        fingerprint="x",
    )
    config = resolve_marketplace_config(market_country="US", currency="USD", marketplace="ebay")
    return ProviderRequest(
        price_key=key,
        market_country=config.market_country,
        currency=config.currency,
        marketplace=config.marketplace,
        provider_marketplace_id=config.provider_marketplace_id,
        provider_domain=config.provider_domain,
        search_locale=config.search_locale,
        display_name=config.display_name,
        market_config=config,
    )


class TitleExtractionTests(unittest.TestCase):
    def test_chrome_only_titles_detected(self) -> None:
        for title in ("Buy It Now", "or", "Best Offer", "Best offer accepted", "Watch", "1 bid"):
            self.assertTrue(is_chrome_only_title(title), title)
            self.assertEqual(clean_candidate_title(title), "")

    def test_real_title_kept(self) -> None:
        title = "Pikachu 58/102 Unlimited - Base Set - Pokemon TCG 1999 Vintage"
        self.assertFalse(is_chrome_only_title(title))
        self.assertEqual(clean_candidate_title(title), title)

    def test_opens_in_new_window_suffix_stripped(self) -> None:
        raw = "Pokemon TCG Base Set Charizard 4/102 Holo Rare English 1999 Opens in a new window or tab"
        cleaned = clean_candidate_title(raw)
        self.assertEqual(cleaned, "Pokemon TCG Base Set Charizard 4/102 Holo Rare English 1999")
        self.assertNotIn("opens in a new window", cleaned.lower())

    def test_extract_skips_chrome_lines_and_picks_listing_title(self) -> None:
        lines = [
            "Buy It Now",
            "or",
            "Best Offer",
            "Pikachu 58/102 Base Set Pokemon TCG",
            "US $29.00",
            "Free shipping",
        ]
        title = extract_title_from_lines(lines, href_text="Buy It Now", expected_currency="USD")
        self.assertIn("Pikachu", title)
        self.assertIn("58/102", title)

    def test_parse_candidate_prefers_structured_title_over_chrome_anchor(self) -> None:
        request = _request()

        class _Q:
            query_index = 0
            query_text = "Pikachu 58"
            query_source = "test"
            search_url = "https://www.ebay.com/sch/i.html?LH_Sold=1&LH_Complete=1"
            market_country = "US"
            currency = "USD"
            provider_marketplace_id = "EBAY_US"
            provider_domain = "ebay.com"
            diagnostics = {"queryStyle": "test"}

        candidate = {
            "href": "https://www.ebay.com/itm/123456789012",
            "title": "Pikachu 58/102 Base Set Unlimited Pokemon",
            "titleSource": "s-item__title",
            "anchorText": "Buy It Now",
            "priceText": "US $29.00",
            "shippingText": "Free shipping",
            "soldDateText": "Sold Sep 25, 2026",
            "conditionText": "Pre-Owned",
            "text": "Buy It Now\nor\nBest Offer\nPikachu 58/102 Base Set Unlimited Pokemon\nUS $29.00\n",
            "source": "li.s-item",
        }
        parsed = parse_candidate_dict(candidate, request=request, search_query=_Q(), index=0)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIn("Pikachu", parsed.title)
        self.assertNotIn(parsed.title.lower(), {"buy it now", "or", "best offer"})
        self.assertEqual(parsed.raw_metadata.get("titleSource"), "s-item__title")

    def test_parse_candidate_drops_chrome_only_cards(self) -> None:
        request = _request()

        class _Q:
            query_index = 0
            query_text = "Pikachu 58"
            query_source = "test"
            search_url = "https://www.ebay.com/sch/i.html?LH_Sold=1&LH_Complete=1"
            market_country = "US"
            currency = "USD"
            provider_marketplace_id = "EBAY_US"
            provider_domain = "ebay.com"
            diagnostics = {"queryStyle": "test"}

        candidate = {
            "href": "https://www.ebay.com/itm/123456789012",
            "title": "Buy It Now",
            "anchorText": "or",
            "priceText": "US $29.00",
            "text": "Buy It Now\nor\nBest Offer\nUS $29.00\n",
            "source": "li.s-item",
        }
        parsed = parse_candidate_dict(candidate, request=request, search_query=_Q(), index=0)
        self.assertIsNone(parsed)


if __name__ == "__main__":
    unittest.main()
