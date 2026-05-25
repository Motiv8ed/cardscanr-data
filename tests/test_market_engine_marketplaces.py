from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.marketplaces import UnsupportedMarketError, resolve_marketplace_config
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


if __name__ == "__main__":
    unittest.main()
