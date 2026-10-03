"""Regression tests for eBay-primary source precedence."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from cardscanr_market_engine.bulk.display_price_policy import decide_display_price
from cardscanr_market_engine.bulk.price_semantics import ReferencePriceObservation
from cardscanr_market_engine.bulk.reference_refresh import BulkReferenceRefreshRunner, BulkRefreshConfig
from cardscanr_market_engine.cache_writer import build_cache_payload
from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.models import MarketPriceKey, PricingStats, ProviderResult
from cardscanr_market_engine.price_source_precedence import (
    PriceObservation,
    can_proposed_replace_selected,
    select_customer_market_price,
    TIER_FRESH_EBAY,
    TIER_REFERENCE,
    TIER_STALE_EBAY,
)
from cardscanr_market_engine.providers.ebay_browser_provider import verify_sold_result_state


NOW = datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc)


def _ref_obs(provider: str = "tcgdex_tcgplayer", price: float = 0.13) -> ReferencePriceObservation:
    return ReferencePriceObservation(
        provider=provider,
        source_market="us",
        source_currency="USD",
        market_price=price,
        low_price=None,
        high_price=None,
        confidence="medium",
        mapping_status="exact",
    )


class SourcePrecedenceUnitTests(unittest.TestCase):
    def test_fresh_ebay_beats_tcgdex(self) -> None:
        selected = select_customer_market_price(
            [
                PriceObservation(
                    provider="ebay_browser",
                    price=1.84,
                    observed_at=NOW - timedelta(hours=12),
                    display_source="verified_au",
                    confidence="medium",
                    sample_size=2,
                ),
                PriceObservation(
                    provider="tcgdex_tcgplayer",
                    price=0.13,
                    observed_at=NOW - timedelta(minutes=1),
                    display_source="reference",
                ),
            ],
            now=NOW,
            market_country="au",
        )
        self.assertEqual(selected.tier, TIER_FRESH_EBAY)
        self.assertEqual(selected.price, 1.84)
        self.assertEqual(selected.provider, "ebay_browser")

    def test_stale_but_valid_ebay_survives_reference(self) -> None:
        selected = select_customer_market_price(
            [
                PriceObservation(
                    provider="ebay_browser",
                    price=1.84,
                    observed_at=NOW - timedelta(hours=36),
                    display_source="market",
                ),
                PriceObservation(
                    provider="static_reference",
                    price=0.2,
                    observed_at=NOW,
                    display_source="reference",
                ),
            ],
            now=NOW,
        )
        self.assertEqual(selected.tier, TIER_STALE_EBAY)
        self.assertEqual(selected.price, 1.84)
        self.assertEqual(selected.freshness, "stale")

    def test_never_ebay_uses_fallback(self) -> None:
        selected = select_customer_market_price(
            [
                PriceObservation(
                    provider="tcgdex_tcgplayer",
                    price=0.13,
                    observed_at=NOW,
                    display_source="reference",
                )
            ],
            now=NOW,
        )
        self.assertEqual(selected.tier, TIER_REFERENCE)
        self.assertEqual(selected.price, 0.13)
        self.assertIn("reference estimate", selected.source_disclosure or "")

    def test_write_guard_blocks_bulk_reference_over_ebay(self) -> None:
        ok, reason = can_proposed_replace_selected(
            current_provider="ebay_browser",
            current_price=1.84,
            current_display_source="market",
            current_observed_at=NOW - timedelta(hours=12),
            proposed_provider="tcgdex_tcgplayer",
            proposed_price=0.13,
            proposed_display_source="reference",
            now=NOW,
        )
        self.assertFalse(ok)
        self.assertIn("blocked_by_tier", reason)

    def test_write_guard_allows_reference_when_no_ebay(self) -> None:
        ok, reason = can_proposed_replace_selected(
            current_provider=None,
            current_price=None,
            current_display_source=None,
            current_observed_at=None,
            proposed_provider="static_reference",
            proposed_price=0.4,
            proposed_display_source="reference",
            now=NOW,
        )
        self.assertTrue(ok)

    def test_invalidated_ebay_can_fall_back(self) -> None:
        selected = select_customer_market_price(
            [
                PriceObservation(
                    provider="ebay_browser",
                    price=1.84,
                    observed_at=NOW - timedelta(hours=2),
                    display_source="verified_au",
                    is_invalidated=True,
                ),
                PriceObservation(
                    provider="tcgdex_tcgplayer",
                    price=0.13,
                    observed_at=NOW,
                    display_source="reference",
                ),
            ],
            now=NOW,
        )
        self.assertEqual(selected.provider, "tcgdex_tcgplayer")
        self.assertEqual(selected.price, 0.13)


class DisplayPolicyPrecedenceTests(unittest.TestCase):
    def test_display_policy_preserves_ebay_over_tcgdex(self) -> None:
        decision = decide_display_price(
            prior_cache={
                "current_market_price": 1.84,
                "provider": "ebay_browser",
                "display_price_source": "market",
                "marketplace": "EBAY_AU",
                "market_country": "AU",
                "confidence": "low",
                "last_updated_at": (NOW - timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
            },
            observation=_ref_obs(),
            converted_price=0.13,
            target_currency="AUD",
            now=NOW,
        )
        self.assertEqual(decision.action, "preserve_verified")
        self.assertEqual(decision.display_price, 1.84)
        self.assertEqual(decision.provider, "ebay_browser")
        self.assertEqual(decision.reference_price, 0.13)
        self.assertEqual(decision.reference_provider, "tcgdex_tcgplayer")

    def test_display_policy_preserves_ebay_over_static_reference(self) -> None:
        decision = decide_display_price(
            prior_cache={
                "current_market_price": 5.0,
                "provider": "ebay_browser",
                "display_price_source": "verified_au",
                "marketplace": "EBAY_AU",
                "last_updated_at": (NOW - timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
            },
            observation=_ref_obs(provider="static_reference", price=0.5),
            converted_price=0.75,
            target_currency="AUD",
            now=NOW,
        )
        self.assertEqual(decision.action, "preserve_verified")
        self.assertEqual(decision.display_price, 5.0)

    def test_display_policy_applies_reference_when_never_ebay(self) -> None:
        decision = decide_display_price(
            prior_cache=None,
            observation=_ref_obs(),
            converted_price=0.13,
            target_currency="AUD",
            now=NOW,
        )
        self.assertEqual(decision.action, "apply_reference")
        self.assertEqual(decision.display_price, 0.13)


class BulkReferenceWriteGuardTests(unittest.TestCase):
    def test_bulk_runner_preserves_selected_ebay_and_stores_reference(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.snapshots: list[dict] = []
                self.cache_writes: list[dict] = []

            def insert_snapshot(self, payload):
                self.snapshots.append(payload)
                return payload

            def upsert_cache(self, payload):
                self.cache_writes.append(payload)
                return payload

            def enqueue_refresh_job(self, **kwargs):
                return {"id": "j1", "status": "queued"}

        client = FakeClient()
        # Minimal engine config via existing bulk test helper shape.
        from tests.test_bulk_reference_pricing import BulkRunnerSimulationTests

        engine_config = BulkRunnerSimulationTests()._engine_config()
        runner = BulkReferenceRefreshRunner(
            client=client,
            engine_config=engine_config,
            refresh_config=BulkRefreshConfig(
                dry_run=False,
                max_keys=10,
                enable_live_tcgdex=False,
                verification_budget_per_run=0,
                high_value_threshold=50.0,
                reference_fresh_hours=12,
            ),
            now_func=lambda: NOW,
            logger=lambda *_a, **_k: None,
        )
        key = MarketPriceKey.from_row(
            {
                "id": "weedle",
                "game": "pokemon",
                "card_name": "Weedle",
                "normalized_card_name": "weedle",
                "set_name": "Chaos Rising",
                "set_code": "me4",
                "collector_number": "1",
                "language": "en",
                "variant": "raw",
                "condition": "raw",
                "market_country": "au",
                "currency": "aud",
                "fingerprint": "pokemon|en|me4|1|weedle|raw|raw|au|aud",
            }
        )
        prior = {
            "current_market_price": 1.84,
            "provider": "ebay_browser",
            "display_price_source": "market",
            "marketplace": "EBAY_AU",
            "market_country": "AU",
            "confidence": "low",
            "sample_size": 2,
            "last_updated_at": (NOW - timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
            "latest_snapshot_id": "ebay-snap",
        }
        runner._lookup_reference = MagicMock(return_value=_ref_obs())  # type: ignore[method-assign]
        runner._convert_price = MagicMock(return_value=0.13)  # type: ignore[method-assign]
        from cardscanr_market_engine.bulk.reference_refresh import BulkRefreshCounters

        counters = BulkRefreshCounters()
        result = runner.process_key(key=key, prior_cache=prior, counters=counters, verification_budget=[0])
        self.assertEqual(result["status"], "preserve_verified")
        self.assertEqual(len(client.snapshots), 1)
        self.assertEqual(client.snapshots[0]["provider"], "tcgdex_tcgplayer")
        self.assertEqual(len(client.cache_writes), 1)
        write = client.cache_writes[0]
        self.assertEqual(write["current_market_price"], 1.84)
        self.assertEqual(write["provider"], "ebay_browser")
        self.assertEqual(write["reference_price"], 0.13)
        self.assertEqual(write["reference_provider"], "tcgdex_tcgplayer")
        self.assertNotIn("latest_snapshot_id", write)


class EbayCacheWriterTests(unittest.TestCase):
    def test_ebay_cache_payload_sets_verified_display_source(self) -> None:
        key = MarketPriceKey.from_row(
            {
                "id": "k1",
                "game": "pokemon",
                "card_name": "Weedle",
                "normalized_card_name": "weedle",
                "set_name": "Chaos Rising",
                "set_code": "me4",
                "collector_number": "1",
                "language": "en",
                "variant": "raw",
                "condition": "raw",
                "market_country": "au",
                "currency": "aud",
                "fingerprint": "fp",
            }
        )
        stats = PricingStats(
            recommended_price=1.84,
            median_price=1.84,
            low_price=1.39,
            average_price=1.84,
            high_price=2.29,
            sample_size=2,
            included_count=2,
            rejected_count=0,
            confidence="low",
            stale_after=NOW + timedelta(hours=6),
        )
        payload = build_cache_payload(
            price_key=key,
            provider_result=ProviderResult(
                provider_name="ebay_browser",
                marketplace="EBAY_AU",
                provider_fingerprint="x",
                query_used="Weedle",
                comps=[],
                raw_metadata={"marketCountry": "AU", "displayCurrency": "AUD"},
            ),
            pricing_stats=stats,
            snapshot_id="snap1",
            refreshed_at=NOW,
        )
        self.assertEqual(payload["display_price_source"], "verified_au")
        self.assertEqual(payload["current_market_price"], 1.84)


class SoldStateVerificationTests(unittest.TestCase):
    def test_sold_state_requires_url_and_result_evidence(self) -> None:
        ok = verify_sold_result_state(
            url="https://www.ebay.com.au/sch/i.html?_nkw=Weedle&LH_Sold=1",
            title="Weedle",
            body_text="Results\nSold 27 Sep 2026\nA$1.50",
        )
        self.assertTrue(ok["SOLD_STATE_VERIFIED"])
        self.assertGreaterEqual(ok["soldDateLines"], 1)

    def test_active_listing_page_not_verified(self) -> None:
        bad = verify_sold_result_state(
            url="https://www.ebay.com.au/sch/i.html?_nkw=Weedle",
            title="Weedle for sale",
            body_text="Buy it now\nAdd to cart\nA$2.00",
        )
        self.assertFalse(bad["SOLD_STATE_VERIFIED"])
        self.assertTrue(bad["activeListingContaminationPossible"])


if __name__ == "__main__":
    unittest.main()
