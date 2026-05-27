"""
test_market_price_provider_contracts.py

Tests for market_pricing_provider_contracts.py data structures.
"""

from __future__ import annotations

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from market_pricing_provider_contracts import (
    MarketPriceEvidenceListing,
    MarketPriceProviderCapabilities,
    MarketPriceProviderError,
    MarketPriceProviderResult,
    MarketPriceSearchRequest,
)


class TestMarketPriceSearchRequest(unittest.TestCase):
    def test_required_fields(self) -> None:
        req = MarketPriceSearchRequest(
            market="AU",
            currency="AUD",
            marketplace="EBAY_AU",
            game="pokemon",
            language="en",
            canonical_id="pokemon|en|base1|4|charizard",
            card_name="Charizard",
            set_name="Base Set",
            set_id="base1",
            collector_number="4",
        )
        self.assertEqual(req.market, "AU")
        self.assertEqual(req.currency, "AUD")
        self.assertEqual(req.graded, False)
        self.assertEqual(req.variant, "raw")
        self.assertEqual(req.condition, "near_mint")
        self.assertEqual(req.max_results, 25)

    def test_optional_date_range_defaults_to_none(self) -> None:
        req = MarketPriceSearchRequest(
            market="US",
            currency="USD",
            marketplace="EBAY_US",
            game="pokemon",
            language="en",
            canonical_id="pokemon|en|base1|4|charizard",
            card_name="Charizard",
            set_name="Base",
            set_id="base1",
            collector_number="4",
        )
        self.assertIsNone(req.date_range_from)
        self.assertIsNone(req.date_range_to)

    def test_exclusion_terms_are_a_tuple(self) -> None:
        req = MarketPriceSearchRequest(
            market="GB",
            currency="GBP",
            marketplace="EBAY_GB",
            game="pokemon",
            language="en",
            canonical_id="pokemon|en|base1|4|charizard",
            card_name="Charizard",
            set_name="Base",
            set_id="base1",
            collector_number="4",
            exclusion_terms=("-proxy", "-fake"),
        )
        self.assertIsInstance(req.exclusion_terms, tuple)
        self.assertIn("-proxy", req.exclusion_terms)

    def test_frozen_immutability(self) -> None:
        req = MarketPriceSearchRequest(
            market="CA",
            currency="CAD",
            marketplace="EBAY_CA",
            game="pokemon",
            language="en",
            canonical_id="pokemon|en|base1|4|charizard",
            card_name="Charizard",
            set_name="Base",
            set_id="base1",
            collector_number="4",
        )
        with self.assertRaises((AttributeError, TypeError)):
            req.market = "US"  # type: ignore[misc]


class TestMarketPriceEvidenceListing(unittest.TestCase):
    def test_basic_fields(self) -> None:
        listing = MarketPriceEvidenceListing(
            title="Charizard Base Set 4 raw near_mint",
            sold_price=80.0,
            shipping_price=5.0,
            total_price=85.0,
            currency="AUD",
            sold_date="2024-03-15T10:00:00Z",
            listing_url="https://www.ebay.com.au/itm/123",
            marketplace="EBAY_AU",
        )
        self.assertEqual(listing.total_price, 85.0)
        self.assertEqual(listing.currency, "AUD")
        self.assertIsNone(listing.seller_location)

    def test_optional_raw_data(self) -> None:
        listing = MarketPriceEvidenceListing(
            title="Pikachu",
            sold_price=5.0,
            shipping_price=0.0,
            total_price=5.0,
            currency="USD",
            sold_date="2024-01-01T00:00:00Z",
            listing_url="https://www.ebay.com/itm/999",
            marketplace="EBAY_US",
            raw_data={"title": "Pikachu", "price": 5.0},
        )
        self.assertIsNotNone(listing.raw_data)
        self.assertNotIn("apiKey", str(listing.raw_data))


class TestMarketPriceProviderResult(unittest.TestCase):
    def test_empty_listings(self) -> None:
        result = MarketPriceProviderResult(
            provider_name="mock",
            source="ebay_sold_listings",
        )
        self.assertEqual(result.listings, [])
        self.assertEqual(result.notes, "")

    def test_with_listings(self) -> None:
        listing = MarketPriceEvidenceListing(
            title="Mewtwo",
            sold_price=100.0,
            shipping_price=0.0,
            total_price=100.0,
            currency="USD",
            sold_date="2024-02-01T00:00:00Z",
            listing_url="https://www.ebay.com/itm/1",
            marketplace="EBAY_US",
        )
        result = MarketPriceProviderResult(
            provider_name="mock",
            source="ebay_sold_listings",
            listings=[listing],
            notes="test",
        )
        self.assertEqual(len(result.listings), 1)


class TestMarketPriceProviderError(unittest.TestCase):
    def test_disabled_provider_error(self) -> None:
        err = MarketPriceProviderError(
            provider_name="ebay_disabled",
            error_code="provider_disabled",
            message="Live eBay access is disabled.",
            live_network_attempted=False,
            safe_for_cloud=True,
        )
        self.assertEqual(err.error_code, "provider_disabled")
        self.assertFalse(err.live_network_attempted)
        self.assertTrue(err.safe_for_cloud)


class TestMarketPriceProviderCapabilities(unittest.TestCase):
    def test_mock_capabilities_shape(self) -> None:
        caps = MarketPriceProviderCapabilities(
            provider_name="mock",
            enabled=True,
            live_network_required=False,
            requires_credentials=False,
            supported_markets=("AU", "US", "GB", "CA"),
            supported_languages=("en",),
            supported_currencies=("AUD", "USD"),
            returns_evidence_listings=True,
            returns_confidence_score=True,
            safe_for_cloud=True,
            next_implementation_step="Already functional.",
        )
        self.assertTrue(caps.enabled)
        self.assertFalse(caps.live_network_required)
        self.assertFalse(caps.requires_credentials)
        self.assertTrue(caps.safe_for_cloud)
        self.assertIn("AU", caps.supported_markets)

    def test_no_secrets_in_capabilities(self) -> None:
        caps = MarketPriceProviderCapabilities(
            provider_name="mock",
            enabled=True,
            live_network_required=False,
            requires_credentials=False,
            supported_markets=("AU",),
            supported_languages=("en",),
            supported_currencies=("AUD",),
            returns_evidence_listings=True,
            returns_confidence_score=True,
            safe_for_cloud=True,
            next_implementation_step="n/a",
            notes="no sensitive data here",
        )
        # Only the free-text fields (notes, next_step) should not contain secret values
        combined = f"{caps.notes} {caps.next_implementation_step}"
        self.assertNotIn("api_key=", combined.lower())
        self.assertNotIn("bearer ", combined.lower())
        self.assertNotIn("password=", combined.lower())


if __name__ == "__main__":
    unittest.main()
