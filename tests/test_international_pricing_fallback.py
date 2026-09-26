"""Tests for international pricing fallback policy and presentation."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from cardscanr_market_engine.international.display_price_resolver import resolve_price_presentation
from cardscanr_market_engine.international.fallback_eligibility import evaluate_international_fallback_eligibility
from cardscanr_market_engine.international.fx_freshness import evaluate_fx_freshness
from cardscanr_market_engine.international.market_fallback_policy import (
    LOW_CONFIDENCE,
    NO_RESULTS,
    SUFFICIENT_FOREIGN_COMPS,
    classify_foreign_market_attempt,
    fallback_markets_for_key,
)
from cardscanr_market_engine.models import MarketPriceKey
from cardscanr_market_engine.currency_conversion import resolve_currency_conversion


def _key(**overrides) -> MarketPriceKey:
    base = {
        "id": "key-1",
        "game": "pokemon",
        "card_name": "Charizard ex",
        "normalized_card_name": "charizard_ex",
        "set_name": "Obsidian Flames",
        "set_code": "sv03",
        "collector_number": "125/197",
        "language": "en",
        "variant": "raw",
        "condition": "raw",
        "market_country": "au",
        "currency": "aud",
        "fingerprint": "pokemon|en|sv03|125/197|charizard_ex|raw|raw|au|aud",
    }
    base.update(overrides)
    return MarketPriceKey.from_row(base)


class InternationalFallbackPolicyTests(unittest.TestCase):
    def test_au_english_fallback_order(self) -> None:
        markets = fallback_markets_for_key(_key())
        self.assertEqual(markets, ("US", "GB", "CA", "DE", "FR"))

    def test_japanese_card_may_search_same_printing_on_en_markets(self) -> None:
        """JA printings: home AU first (separate job); intl may use EN-domain eBay
        for the same Japanese identity — never an English printing substitute."""
        markets = fallback_markets_for_key(_key(language="ja", market_country="au", currency="aud"))
        self.assertEqual(markets, ("US", "GB", "CA", "DE", "FR"))

    def test_korean_card_has_no_browser_fallback_yet(self) -> None:
        markets = fallback_markets_for_key(_key(language="ko", market_country="au", currency="aud"))
        self.assertEqual(markets, ())

    def test_us_fallback_order(self) -> None:
        markets = fallback_markets_for_key(_key(market_country="us", currency="usd"))
        self.assertEqual(markets[0], "CA")

    def test_nz_has_no_native_route_and_falls_back_foreign(self) -> None:
        from cardscanr_market_engine.international.market_fallback_policy import (
            NO_NATIVE_SOLD_ROUTE_MARKETS,
            has_native_sold_route,
        )

        self.assertFalse(has_native_sold_route("NZ"))
        self.assertIn("NZ", NO_NATIVE_SOLD_ROUTE_MARKETS)
        markets = fallback_markets_for_key(_key(market_country="nz", currency="nzd"))
        self.assertEqual(markets[0], "AU")
        self.assertNotIn("NZ", markets)

    def test_jp_has_no_native_route(self) -> None:
        from cardscanr_market_engine.international.market_fallback_policy import has_native_sold_route

        self.assertFalse(has_native_sold_route("JP"))
        markets = fallback_markets_for_key(_key(market_country="jp", currency="jpy", language="ja"))
        self.assertEqual(markets[0], "US")
        self.assertNotIn("JP", markets)

    def test_classify_foreign_market_attempt(self) -> None:
        self.assertEqual(
            classify_foreign_market_attempt(
                included_count=0, recommended_price=None, confidence="low"
            ),
            NO_RESULTS,
        )
        self.assertEqual(
            classify_foreign_market_attempt(
                included_count=1,
                recommended_price=10.0,
                confidence="low",
                evidence_outcome="unavailable",
            ),
            LOW_CONFIDENCE,
        )
        self.assertEqual(
            classify_foreign_market_attempt(
                included_count=8,
                recommended_price=12.0,
                confidence="high",
                evidence_outcome="numeric_estimate",
            ),
            SUFFICIENT_FOREIGN_COMPS,
        )


class InternationalFallbackEligibilityTests(unittest.TestCase):
    def test_reference_price_blocks_fallback(self) -> None:
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        result = evaluate_international_fallback_eligibility(
            price_key=_key(),
            cache={
                "current_market_price": 12.5,
                "display_price_source": "reference",
                "verification_required": False,
                "provider": "static_reference",
            },
            now=now,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "local_or_reference_sufficient")

    def test_unresolved_au_key_is_eligible(self) -> None:
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        result = evaluate_international_fallback_eligibility(
            price_key=_key(),
            cache={"current_market_price": None, "display_price_source": None},
            now=now,
        )
        self.assertTrue(result.eligible)
        self.assertEqual(result.reason, "eligible")


class DisplayPriceResolverTests(unittest.TestCase):
    def test_international_estimate_presentation(self) -> None:
        presentation = resolve_price_presentation(
            cache={
                "current_market_price": 26.40,
                "low_price": 22.0,
                "high_price": 29.0,
                "currency": "AUD",
                "market_country": "AU",
                "display_price_source": "international_estimate",
                "source_market_country": "US",
                "source_currency": "USD",
                "source_price": 17.35,
                "fx_rate": 1.55,
                "fx_rate_timestamp": "2026-05-07T00:00:00Z",
                "confidence": "high",
            },
            key_row={"market_country": "AU", "currency": "AUD"},
        )
        self.assertEqual(presentation.price_class, "international_estimate")
        self.assertEqual(presentation.label, "International estimate")
        self.assertIn("United States", presentation.explanation or "")
        self.assertIn("Shipping", presentation.disclaimer or "")

    def test_local_verified_presentation(self) -> None:
        presentation = resolve_price_presentation(
            cache={
                "current_market_price": 31.2,
                "currency": "AUD",
                "market_country": "AU",
                "display_price_source": "verified_au",
                "provider": "ebay_browser",
            }
        )
        self.assertEqual(presentation.price_class, "local_verified")
        self.assertEqual(presentation.label, "Local market price")


class FxConversionTests(unittest.TestCase):
    def test_usd_to_aud_conversion(self) -> None:
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        conversion = resolve_currency_conversion(
            source_currency="USD",
            target_currency="AUD",
            rates={"USD:AUD": 1.55},
            rate_source="configured_static_rates",
            now=now,
        )
        self.assertAlmostEqual(conversion.amount(17.35) or 0, 26.89, places=2)

    def test_same_currency_no_conversion(self) -> None:
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        conversion = resolve_currency_conversion(
            source_currency="AUD",
            target_currency="AUD",
            rates={},
            rate_source="configured_static_rates",
            now=now,
        )
        self.assertEqual(conversion.rate, 1.0)
        self.assertEqual(conversion.amount(10.0), 10.0)

    def test_missing_rate_raises(self) -> None:
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        with self.assertRaises(ValueError):
            resolve_currency_conversion(
                source_currency="CHF",
                target_currency="AUD",
                rates={"USD:AUD": 1.55},
                rate_source="configured_static_rates",
                now=now,
            )

    def test_static_rate_staleness_is_reported(self) -> None:
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        freshness = evaluate_fx_freshness(
            rate_source="configured_static_rates",
            rate_timestamp=None,
            now=now,
            cache={"source": "configured_static_rates"},
        )
        self.assertTrue(freshness.stale)
        self.assertFalse(freshness.allows_conversion)
        self.assertEqual(freshness.health, "STALE")
        self.assertEqual(freshness.block_reason, "FX_RATE_STALE_NO_SAFE_CONVERSION")

    def test_same_currency_conversion_always_allowed(self) -> None:
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        freshness = evaluate_fx_freshness(
            rate_source="same_currency",
            rate_timestamp=None,
            now=now,
            same_currency=True,
        )
        self.assertTrue(freshness.allows_conversion)
        self.assertEqual(freshness.health, "HEALTHY")

    def test_missing_ecb_cache_blocks_conversion(self) -> None:
        now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
        freshness = evaluate_fx_freshness(
            rate_source="ECB",
            rate_timestamp=None,
            now=now,
            cache={},
        )
        self.assertTrue(freshness.stale)
        self.assertFalse(freshness.allows_conversion)
        self.assertEqual(freshness.health, "STALE")


if __name__ == "__main__":
    unittest.main()
