from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.marketplaces import (
    UnsupportedMarketError,
    browser_supported_market_routes,
    ebay_host_matches_provider_domain,
    ebay_marketplace_fallback_order,
    is_browser_supported_market,
    resolve_marketplace_config,
)
from cardscanr_market_engine.fingerprints import build_market_price_fingerprint


class MarketplacesTests(unittest.TestCase):
    def test_resolves_ebay_au_route(self) -> None:
        resolved = resolve_marketplace_config(
            market_country="au",
            currency="aud",
            marketplace="EBAY",
        )
        self.assertEqual(resolved.market_country, "AU")
        self.assertEqual(resolved.currency, "AUD")
        self.assertEqual(resolved.provider_marketplace_id, "EBAY_AU")
        self.assertEqual(resolved.provider_domain, "ebay.com.au")

    def test_uk_alias_maps_to_gb(self) -> None:
        resolved = resolve_marketplace_config(
            market_country="uk",
            currency="gbp",
            marketplace="ebay",
        )
        self.assertEqual(resolved.market_country, "GB")
        self.assertEqual(resolved.provider_marketplace_id, "EBAY_GB")

    def test_unsupported_market_raises(self) -> None:
        with self.assertRaises(UnsupportedMarketError):
            resolve_marketplace_config(
                market_country="NZ",
                currency="NZD",
                marketplace="ebay",
            )

    def test_smoke_card_market_fingerprints_and_configs_differ(self) -> None:
        base = {
            "game": "pokemon",
            "language": "en",
            "set_code": "smoke-test",
            "set_name": "Smoke Test Set",
            "collector_number": "001/999",
            "card_name": "Smoke Test Charizard ex",
            "variant": "raw",
            "condition": "raw",
        }
        markets = [("AU", "AUD"), ("US", "USD"), ("GB", "GBP")]
        fingerprints = [
            build_market_price_fingerprint(**base, market_country=country, currency=currency)
            for country, currency in markets
        ]
        configs = [
            resolve_marketplace_config(market_country=country, currency=currency, marketplace="ebay")
            for country, currency in markets
        ]
        self.assertEqual(len(set(fingerprints)), 3)
        self.assertEqual(
            {config.provider_marketplace_id for config in configs},
            {"EBAY_AU", "EBAY_US", "EBAY_GB"},
        )

    def test_home_market_is_attempted_before_configured_fallbacks(self) -> None:
        order = ebay_marketplace_fallback_order(
            requested_market_country="AU",
            requested_currency="AUD",
            marketplace="ebay",
            configured_order=("EBAY_US", "EBAY_GB", "EBAY_CA"),
        )

        self.assertEqual(
            [config.provider_marketplace_id for config in order],
            ["EBAY_AU", "EBAY_US", "EBAY_GB", "EBAY_CA"],
        )

    def test_empty_configured_order_is_home_market_only(self) -> None:
        order = ebay_marketplace_fallback_order(
            requested_market_country="US",
            requested_currency="USD",
            marketplace="ebay",
            configured_order=(),
        )
        self.assertEqual(
            [config.provider_marketplace_id for config in order],
            ["EBAY_US"],
        )

    def test_ebay_host_match_rejects_cross_market_redirect(self) -> None:
        self.assertTrue(
            ebay_host_matches_provider_domain(
                final_url_or_host="https://www.ebay.com/sch/i.html?LH_Sold=1",
                provider_domain="ebay.com",
            )
        )
        self.assertFalse(
            ebay_host_matches_provider_domain(
                final_url_or_host="https://www.ebay.com.au/sch/i.html?LH_Sold=1",
                provider_domain="ebay.com",
            )
        )

    def test_configurable_fallback_order_is_preserved_after_home(self) -> None:
        order = ebay_marketplace_fallback_order(
            requested_market_country="AU",
            requested_currency="AUD",
            marketplace="ebay",
            configured_order=("EBAY_GB", "EBAY_CA", "EBAY_US"),
        )

        self.assertEqual(
            [config.provider_marketplace_id for config in order],
            ["EBAY_AU", "EBAY_GB", "EBAY_CA", "EBAY_US"],
        )

    def test_browser_supported_routes_are_au_us_gb_ca(self) -> None:
        self.assertEqual(
            browser_supported_market_routes(),
            (("AU", "AUD"), ("US", "USD"), ("GB", "GBP"), ("CA", "CAD")),
        )
        self.assertTrue(is_browser_supported_market(market_country="au", currency="aud"))
        self.assertFalse(is_browser_supported_market(market_country="de", currency="eur"))
        self.assertFalse(is_browser_supported_market(market_country="jp", currency="jpy"))


if __name__ == "__main__":
    unittest.main()
