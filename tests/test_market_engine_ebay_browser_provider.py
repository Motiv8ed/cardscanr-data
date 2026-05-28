from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.marketplaces import resolve_marketplace_config  # noqa: E402
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest  # noqa: E402
from cardscanr_market_engine.providers import MockMarketCompsProvider, create_market_comps_provider  # noqa: E402
from cardscanr_market_engine.providers.ebay_browser_provider import (  # noqa: E402
    EbayBrowserSoldCompsProvider,
    contains_block_marker,
    parse_price_text,
    parse_shipping_text,
    parse_sold_date_text,
)
from cardscanr_market_engine.providers.errors import ProviderDisabledError, sanitize_provider_diagnostics  # noqa: E402
from cardscanr_market_engine.providers.errors import ProviderUnsupportedMarketError  # noqa: E402
from cardscanr_market_engine.providers.query_builder import build_provider_search_query  # noqa: E402


def sample_request(
    *,
    country: str = "AU",
    currency: str = "AUD",
    condition: str = "raw",
    variant: str = "raw",
) -> ProviderRequest:
    market = resolve_marketplace_config(market_country=country, currency=currency, marketplace="ebay")
    key = MarketPriceKey(
        id="key-1",
        game="pokemon",
        card_name="Charizard ex",
        normalized_card_name="charizard ex",
        set_name="Obsidian Flames",
        set_code="sv03",
        collector_number="125/197",
        language="en",
        variant=variant,
        condition=condition,
        market_country=country.lower(),
        currency=currency.lower(),
        fingerprint=f"pokemon|en|sv03|125-197|charizard-ex|{variant}|{condition}|{country.lower()}|{currency.lower()}",
    )
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


class ProviderFactoryTests(unittest.TestCase):
    def test_provider_factory_default_is_mock(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            provider = create_market_comps_provider()
        self.assertIsInstance(provider, MockMarketCompsProvider)

    def test_ebay_browser_disabled_without_enable_flag(self) -> None:
        with patch.dict(os.environ, {"MARKET_LOOKUP_PROVIDER": "ebay_browser"}, clear=True):
            with self.assertRaises(ProviderDisabledError):
                create_market_comps_provider()

    def test_ebay_browser_enabled_with_explicit_flag(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MARKET_LOOKUP_PROVIDER": "ebay_browser",
                "ENABLE_EBAY_REAL_LOOKUP": "true",
                "EBAY_BROWSER_COOLDOWN_SECONDS": "1",
                "EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS": "1",
            },
            clear=True,
        ):
            provider = create_market_comps_provider()
        self.assertIsInstance(provider, EbayBrowserSoldCompsProvider)

    def test_ebay_browser_rejects_unsupported_market_before_network(self) -> None:
        provider = EbayBrowserSoldCompsProvider()
        with self.assertRaises(ProviderUnsupportedMarketError):
            provider.fetch_comps(sample_request(country="DE", currency="EUR"))


class QueryBuilderTests(unittest.TestCase):
    def test_query_builder_au_uses_ebay_com_au(self) -> None:
        query = build_provider_search_query(sample_request(country="AU", currency="AUD"))
        self.assertEqual(query.provider_domain, "ebay.com.au")
        self.assertIn("www.ebay.com.au", query.search_url)

    def test_query_builder_us_uses_ebay_com(self) -> None:
        query = build_provider_search_query(sample_request(country="US", currency="USD"))
        self.assertEqual(query.provider_domain, "ebay.com")
        self.assertIn("www.ebay.com/sch/i.html", query.search_url)

    def test_query_builder_gb_uses_ebay_co_uk(self) -> None:
        query = build_provider_search_query(sample_request(country="GB", currency="GBP"))
        self.assertEqual(query.provider_domain, "ebay.co.uk")
        self.assertIn("www.ebay.co.uk", query.search_url)

    def test_query_builder_ca_uses_ebay_ca(self) -> None:
        query = build_provider_search_query(sample_request(country="CA", currency="CAD"))
        self.assertEqual(query.provider_domain, "ebay.ca")
        self.assertIn("www.ebay.ca", query.search_url)

    def test_query_builder_includes_sold_completed_params(self) -> None:
        query = build_provider_search_query(sample_request())
        self.assertIn("LH_Sold=1", query.search_url)
        self.assertIn("LH_Complete=1", query.search_url)

    def test_query_builder_excludes_raw_bad_terms(self) -> None:
        query = build_provider_search_query(sample_request())
        for term in ("proxy", "custom", "digital", "code", "jumbo", "lot", "bundle", "pack", "booster", "sealed", "psa", "cgc", "bgs", "graded"):
            self.assertIn(f"-{term}", query.query_text)

    def test_query_builder_handles_graded_condition(self) -> None:
        query = build_provider_search_query(sample_request(condition="psa_10", variant="graded"))
        self.assertNotIn("-psa", query.query_text)
        self.assertNotIn("-graded", query.query_text)
        self.assertIn("-proxy", query.query_text)


class ParserTests(unittest.TestCase):
    def test_price_parser_handles_aud_usd_gbp_cad_examples(self) -> None:
        examples = [
            ("A$12.34", "AUD", 12.34),
            ("US $56.78", "USD", 56.78),
            ("£9.99", "GBP", 9.99),
            ("C $101.50", "CAD", 101.50),
        ]
        for text, currency, expected in examples:
            amount, detected, _diagnostics = parse_price_text(text, expected_currency=currency)
            self.assertEqual(amount, expected)
            self.assertEqual(detected, currency)

    def test_shipping_parser_handles_free_and_paid_shipping(self) -> None:
        free, free_diag = parse_shipping_text("Free postage", expected_currency="AUD")
        paid, paid_diag = parse_shipping_text("+ A$4.99 shipping", expected_currency="AUD")
        self.assertEqual(free, 0.0)
        self.assertTrue(free_diag["freeShipping"])
        self.assertEqual(paid, 4.99)
        self.assertEqual(paid_diag["detectedCurrency"], "AUD")

    def test_sold_date_parser_handles_common_formats(self) -> None:
        self.assertEqual(parse_sold_date_text("Sold May 20, 2026").year, 2026)
        self.assertEqual(parse_sold_date_text("20 May 2026").month, 5)

    def test_block_detection_text_detection(self) -> None:
        self.assertTrue(contains_block_marker(title="Verify yourself", body_text="Are you a robot?"))
        self.assertTrue(contains_block_marker(title="", body_text="Access denied"))
        self.assertFalse(contains_block_marker(title="Charizard listings", body_text="Sold results"))

    def test_provider_diagnostics_redacts_secrets(self) -> None:
        clean = sanitize_provider_diagnostics(
            {
                "apiKey": "abc",
                "Authorization": "Bearer token",
                "nested": {"cookie": "session=secret", "providerDomain": "ebay.com.au"},
            }
        )
        self.assertEqual(clean["apiKey"], "***REDACTED***")
        self.assertEqual(clean["Authorization"], "***REDACTED***")
        self.assertEqual(clean["nested"]["cookie"], "***REDACTED***")
        self.assertEqual(clean["nested"]["providerDomain"], "ebay.com.au")


@unittest.skipUnless(
    os.getenv("ENABLE_EBAY_REAL_LOOKUP", "").lower() == "true"
    and os.getenv("RUN_LIVE_EBAY_PROVIDER_TEST", "").lower() == "true",
    "Live eBay provider test requires ENABLE_EBAY_REAL_LOOKUP=true and RUN_LIVE_EBAY_PROVIDER_TEST=true",
)
class LiveEbayProviderTests(unittest.TestCase):
    def test_live_ebay_provider_fetches_without_writing(self) -> None:
        provider = EbayBrowserSoldCompsProvider()
        result = provider.fetch_comps(sample_request(country="AU", currency="AUD"))
        self.assertEqual(result.provider_name, "ebay_browser")


if __name__ == "__main__":
    unittest.main()
