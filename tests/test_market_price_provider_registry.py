"""
test_market_price_provider_registry.py

Tests for:
- MarketPriceProviderRegistry
- Mock provider
- Manual provider
- DisabledEbayMarketPriceProvider
- Query builder v2 (provider-ready queries)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from market_pricing_provider_contracts import MarketPriceSearchRequest
from market_price_providers.provider_registry import (
    MarketPriceProviderRegistry,
    ProviderNotAllowedError,
    get_default_registry,
)
from market_price_providers.mock_provider import MockMarketPriceProvider
from market_price_providers.manual_provider import ManualMarketPriceProvider
from market_price_providers.disabled_ebay_provider import (
    DisabledEbayMarketPriceProvider,
    DisabledProviderError,
)
from build_market_price_queries import build_provider_queries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_request(market: str = "AU", currency: str = "AUD") -> MarketPriceSearchRequest:
    return MarketPriceSearchRequest(
        market=market,
        currency=currency,
        marketplace=f"EBAY_{market}",
        game="pokemon",
        language="en",
        canonical_id="pokemon|en|base1|4|charizard",
        card_name="Charizard",
        set_name="Base Set",
        set_id="base1",
        collector_number="4",
        variant="raw",
        condition="near_mint",
    )


# ---------------------------------------------------------------------------
# Provider registry — allowed providers
# ---------------------------------------------------------------------------


class TestProviderRegistryAllowedProviders(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MarketPriceProviderRegistry()

    def test_mock_is_allowed(self) -> None:
        self.assertTrue(self.registry.is_allowed("mock"))

    def test_manual_is_allowed(self) -> None:
        self.assertTrue(self.registry.is_allowed("manual"))

    def test_get_mock_returns_mock_provider(self) -> None:
        provider = self.registry.get("mock")
        self.assertIsInstance(provider, MockMarketPriceProvider)

    def test_get_manual_returns_manual_provider(self) -> None:
        provider = self.registry.get("manual")
        self.assertIsInstance(provider, ManualMarketPriceProvider)

    def test_registered_names_includes_mock_and_manual(self) -> None:
        names = self.registry.registered_names()
        self.assertIn("mock", names)
        self.assertIn("manual", names)


# ---------------------------------------------------------------------------
# Provider registry — blocked eBay providers
# ---------------------------------------------------------------------------


class TestProviderRegistryBlocksEbay(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = MarketPriceProviderRegistry()

    def test_ebay_disabled_is_not_allowed(self) -> None:
        self.assertFalse(self.registry.is_allowed("ebay_disabled"))

    def test_ebay_apify_planned_not_allowed(self) -> None:
        self.assertFalse(self.registry.is_allowed("ebay_sold_listings_apify_planned"))

    def test_ebay_api_planned_not_allowed(self) -> None:
        self.assertFalse(self.registry.is_allowed("ebay_sold_listings_api_planned"))

    def test_get_ebay_disabled_raises(self) -> None:
        with self.assertRaises(ProviderNotAllowedError):
            self.registry.get("ebay_disabled")

    def test_live_provider_error_message_mentions_disabled(self) -> None:
        try:
            self.registry.get("ebay_disabled")
        except ProviderNotAllowedError as exc:
            self.assertIn("disabled", str(exc).lower())
        else:
            self.fail("Expected ProviderNotAllowedError")

    def test_unknown_provider_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.registry.get("nonexistent_provider")


# ---------------------------------------------------------------------------
# Disabled eBay provider — does not call network
# ---------------------------------------------------------------------------


class TestDisabledEbayProvider(unittest.TestCase):
    def test_fetch_raises_disabled_error(self) -> None:
        provider = DisabledEbayMarketPriceProvider()
        req = _sample_request()
        with self.assertRaises(DisabledProviderError):
            provider.fetch(req)

    def test_error_mentions_legal_reason(self) -> None:
        provider = DisabledEbayMarketPriceProvider()
        req = _sample_request()
        try:
            provider.fetch(req)
        except DisabledProviderError as exc:
            msg = str(exc)
            self.assertIn("disabled", msg.lower())
        else:
            self.fail("Expected DisabledProviderError")

    def test_error_live_network_attempted_is_false(self) -> None:
        provider = DisabledEbayMarketPriceProvider()
        req = _sample_request()
        try:
            provider.fetch(req)
        except DisabledProviderError as exc:
            self.assertFalse(exc.provider_error.live_network_attempted)

    def test_capabilities_say_disabled(self) -> None:
        self.assertFalse(DisabledEbayMarketPriceProvider.CAPABILITIES.enabled)

    def test_capabilities_say_live_network_required(self) -> None:
        self.assertTrue(DisabledEbayMarketPriceProvider.CAPABILITIES.live_network_required)

    def test_capabilities_say_not_safe_for_cloud(self) -> None:
        self.assertFalse(DisabledEbayMarketPriceProvider.CAPABILITIES.safe_for_cloud)


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------


class TestMockProviderViaRegistry(unittest.TestCase):
    def test_mock_provider_returns_listings(self) -> None:
        registry = MarketPriceProviderRegistry()
        provider = registry.get("mock")
        result = provider.fetch(_sample_request("AU", "AUD"))
        self.assertGreaterEqual(len(result.listings), 3)

    def test_mock_provider_is_deterministic(self) -> None:
        provider = MockMarketPriceProvider()
        req = _sample_request("US", "USD")
        first = provider.fetch(req)
        second = provider.fetch(req)
        self.assertEqual(
            [l.total_price for l in first.listings],
            [l.total_price for l in second.listings],
        )

    def test_mock_provider_currency_matches_request(self) -> None:
        provider = MockMarketPriceProvider()
        result = provider.fetch(_sample_request("GB", "GBP"))
        self.assertTrue(all(l.currency == "GBP" for l in result.listings))

    def test_mock_provider_safe_for_cloud(self) -> None:
        self.assertTrue(MockMarketPriceProvider.CAPABILITIES.safe_for_cloud)

    def test_mock_provider_no_live_network(self) -> None:
        self.assertFalse(MockMarketPriceProvider.CAPABILITIES.live_network_required)


# ---------------------------------------------------------------------------
# Manual provider
# ---------------------------------------------------------------------------


class TestManualProvider(unittest.TestCase):
    def test_manual_returns_no_results_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does_not_exist.json"
            provider = ManualMarketPriceProvider(missing)
            result = provider.fetch(_sample_request())
            self.assertEqual(result.listings, [])
            self.assertIn("not found", result.notes.lower())

    def test_manual_loads_matching_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_file = Path(tmp) / "manual.json"
            rows = [
                {
                    "canonicalCardId": "pokemon|en|base1|4|charizard",
                    "setId": "base1",
                    "market": "AU",
                    "language": "en",
                    "title": "Charizard Base Set 4 near mint pokemon",
                    "soldPrice": 90.0,
                    "shippingPrice": 0.0,
                    "currency": "AUD",
                    "soldDate": "2024-03-10",
                    "listingUrl": "https://www.ebay.com.au/itm/1001",
                }
            ]
            data_file.write_text(json.dumps(rows), encoding="utf-8")
            provider = ManualMarketPriceProvider(data_file)
            result = provider.fetch(_sample_request("AU", "AUD"))
            self.assertEqual(len(result.listings), 1)
            self.assertEqual(result.listings[0].sold_price, 90.0)

    def test_manual_provider_safe_for_cloud(self) -> None:
        self.assertTrue(ManualMarketPriceProvider.CAPABILITIES.safe_for_cloud)


# ---------------------------------------------------------------------------
# Query builder v2 — exclusion terms
# ---------------------------------------------------------------------------


class TestQueryBuilderV2(unittest.TestCase):
    def test_exclusion_terms_present_in_base_query(self) -> None:
        q = build_provider_queries(
            market="AU",
            card_name="Charizard",
            set_name="Base Set",
            collector_number="4",
        )
        base = q["queries"]["base"]
        self.assertIn("-proxy", base)
        self.assertIn("-fake", base)
        self.assertIn("-digital", base)
        self.assertIn("-lot", base)
        self.assertIn("-bundle", base)
        self.assertIn("-damaged", base)

    def test_all_four_markets_have_distinct_domains(self) -> None:
        domains = set()
        for market in ("AU", "US", "GB", "CA"):
            q = build_provider_queries(
                market=market,
                card_name="Pikachu",
                set_name="Base Set",
                collector_number="58",
            )
            domains.add(q["domain"])
        self.assertEqual(len(domains), 4)

    def test_quality_warning_when_card_name_missing(self) -> None:
        q = build_provider_queries(
            market="AU",
            card_name="",
            set_name="Base Set",
            collector_number="4",
        )
        self.assertTrue(len(q["qualityWarnings"]) > 0)

    def test_graded_query_includes_psa_token(self) -> None:
        q = build_provider_queries(
            market="US",
            card_name="Charizard",
            set_name="Base Set",
            collector_number="4",
            graded=True,
        )
        self.assertIn("psa", q["queries"]["base"].lower())

    def test_live_ebay_not_enabled(self) -> None:
        q = build_provider_queries(
            market="AU",
            card_name="Charizard",
            set_name="Base Set",
            collector_number="4",
        )
        self.assertFalse(q["liveEbayEnabled"])

    def test_no_secrets_in_query_output(self) -> None:
        q = build_provider_queries(
            market="AU",
            card_name="Charizard",
            set_name="Base Set",
            collector_number="4",
        )
        serialised = json.dumps(q)
        self.assertNotIn("api_key", serialised.lower())
        self.assertNotIn("token", serialised.lower())
        self.assertNotIn("secret", serialised.lower())


# ---------------------------------------------------------------------------
# Capabilities — no secrets printed
# ---------------------------------------------------------------------------


class TestCapabilitiesNoSecrets(unittest.TestCase):
    def test_no_secrets_in_provider_capabilities(self) -> None:
        registry = MarketPriceProviderRegistry()
        for caps in registry.capabilities():
            serialised = json.dumps(
                {
                    "name": caps.provider_name,
                    "notes": caps.notes,
                    "nextStep": caps.next_implementation_step,
                }
            )
            self.assertNotIn("api_key", serialised.lower(), msg=f"Found secret in {caps.provider_name}")
            self.assertNotIn("bearer", serialised.lower(), msg=f"Found secret in {caps.provider_name}")


if __name__ == "__main__":
    unittest.main()
