from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.models import EvaluatedComp, SoldComp
from cardscanr_market_engine.pricing_stats import calculate_pricing_stats, calculate_stale_after, determine_confidence


def config() -> MarketEngineConfig:
    return MarketEngineConfig.from_env(require_supabase=False)


def evaluated(total_price: float, *, included: bool = True, score: float = 0.9, reason: str | None = None) -> EvaluatedComp:
    return EvaluatedComp(
        comp=SoldComp(
            source_listing_id=f"listing-{total_price}",
            title="sample",
            sold_price=max(total_price - 1, 0),
            shipping_price=1.0 if total_price > 0 else 0.0,
            total_price=total_price,
            currency="USD",
            sold_date=datetime(2026, 5, 20, tzinfo=timezone.utc),
            listing_url="https://example.test/listing",
            condition_text="Raw",
        ),
        included_in_estimate=included,
        rejection_reason=reason,
        match_score=score,
    )


def evaluated_prices(
    sold_price: float,
    shipping_price: float,
    *,
    included: bool = True,
    score: float = 0.9,
    reason: str | None = None,
    raw_metadata: dict | None = None,
    sold_date: datetime | None = None,
) -> EvaluatedComp:
    total_price = round(sold_price + shipping_price, 2)
    return EvaluatedComp(
        comp=SoldComp(
            source_listing_id=f"listing-{sold_price}-{shipping_price}",
            title="sample",
            sold_price=sold_price,
            shipping_price=shipping_price,
            total_price=total_price,
            currency="USD",
            sold_date=sold_date or datetime(2026, 5, 20, tzinfo=timezone.utc),
            listing_url="https://example.test/listing",
            condition_text="Raw",
            raw_metadata=raw_metadata or {},
        ),
        included_in_estimate=included,
        rejection_reason=reason,
        match_score=score,
    )


class PricingStatsTests(unittest.TestCase):
    def test_pricing_stats_include_median_average_low_high(self) -> None:
        stats = calculate_pricing_stats(
            [evaluated(10.0), evaluated(20.0), evaluated(30.0), evaluated(99.0, included=False, reason="lot_or_bundle")],
            now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            config=config(),
        )
        self.assertEqual(stats.median_price, 19.0)
        self.assertEqual(stats.average_price, 19.0)
        self.assertEqual(stats.low_price, 9.0)
        self.assertEqual(stats.high_price, 29.0)
        self.assertEqual(stats.recommended_price, 19.0)
        self.assertEqual(stats.confidence, "medium")

    def test_item_stats_exclude_shipping_and_landed_stats_include_shipping(self) -> None:
        stats = calculate_pricing_stats(
            [
                evaluated_prices(9.0, 15.0),
                evaluated_prices(13.0, 0.0),
                evaluated_prices(20.0, 20.0),
            ],
            now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            config=config(),
        )
        self.assertEqual(stats.item_median_price, 13.0)
        self.assertEqual(stats.item_low_price, 9.0)
        self.assertEqual(stats.item_high_price, 20.0)
        self.assertEqual(stats.item_recommended_price, 13.0)
        self.assertEqual(stats.landed_median_price, 24.0)
        self.assertEqual(stats.landed_low_price, 13.0)
        self.assertEqual(stats.landed_high_price, 40.0)
        self.assertEqual(stats.landed_recommended_price, 24.0)
        self.assertEqual(stats.recommended_price, 13.0)
        self.assertEqual(stats.median_price, 13.0)
        self.assertEqual(stats.price_basis, "item_price")

    def test_confidence_and_stale_after_rules(self) -> None:
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        stats = calculate_pricing_stats(
            [evaluated(20.0, score=0.95) for _ in range(8)],
            now=now,
            config=config(),
        )
        self.assertEqual(determine_confidence(included_count=8, average_match_score=0.95), "high")
        self.assertEqual(stats.confidence, "high")
        self.assertEqual((stats.stale_after - now).total_seconds(), 24 * 3600)

    def test_no_comps_uses_short_stale_after(self) -> None:
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        stale_after = calculate_stale_after(now=now, included_count=0, confidence="low", config=config())
        self.assertEqual((stale_after - now).total_seconds(), 3 * 3600)

    def test_wide_spread_small_sample_downgrades_high_confidence(self) -> None:
        stats = calculate_pricing_stats(
            [
                evaluated_prices(10.0, 0.0, score=0.95),
                evaluated_prices(11.0, 0.0, score=0.95),
                evaluated_prices(12.0, 0.0, score=0.95),
                evaluated_prices(13.0, 0.0, score=0.95),
                evaluated_prices(14.0, 0.0, score=0.95),
                evaluated_prices(15.0, 0.0, score=0.95),
                evaluated_prices(16.0, 0.0, score=0.95),
                evaluated_prices(40.0, 0.0, score=0.95),
            ],
            now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            config=config(),
        )
        self.assertEqual(stats.confidence, "medium")
        self.assertGreater(stats.price_spread_ratio or 0, 3)
        self.assertIn("wide_item_price_spread_small_sample", stats.confidence_warnings)
        self.assertEqual(stats.included_price_distribution[-1], 40.0)

    def test_variant_specific_small_sample_adds_confidence_warning(self) -> None:
        stats = calculate_pricing_stats(
            [
                evaluated_prices(2.0, 0.0, raw_metadata={"requested_variant": "non_holo"}),
                evaluated_prices(2.5, 0.0, raw_metadata={"requested_variant": "non_holo"}),
            ],
            now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            config=config(),
        )
        self.assertEqual(stats.confidence, "low")
        self.assertIn("insufficient_variant_specific_comps", stats.confidence_warnings)

    def test_single_recent_clean_comp_is_low_confidence_and_marked(self) -> None:
        stats = calculate_pricing_stats(
            [evaluated_prices(2.0, 0.0, raw_metadata={"requested_variant": "non_holo"})],
            now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            config=config(),
        )
        self.assertEqual(stats.confidence, "low")
        self.assertEqual(stats.recommended_price, 2.0)
        self.assertEqual(stats.price_reliability, "single_comp_low_confidence")
        self.assertIn("single_clean_comp_only", stats.confidence_warnings)
        self.assertEqual(stats.clean_recent_comp_count, 1)
        self.assertEqual(stats.clean_stale_comp_count, 0)

    def test_single_stale_clean_comp_is_not_reliable_current_price(self) -> None:
        stats = calculate_pricing_stats(
            [
                evaluated_prices(
                    1.79,
                    2.0,
                    raw_metadata={"requested_variant": "non_holo"},
                    sold_date=datetime(2025, 5, 18, tzinfo=timezone.utc),
                )
            ],
            now=datetime(2026, 6, 5, tzinfo=timezone.utc),
            config=config(),
        )
        self.assertEqual(stats.confidence, "low")
        self.assertIsNone(stats.recommended_price)
        self.assertIsNone(stats.item_recommended_price)
        self.assertEqual(stats.median_price, 1.79)
        self.assertEqual(stats.price_reliability, "stale_single_comp")
        self.assertEqual(stats.no_reliable_price_reason, "stale_single_comp_only")
        self.assertIn("single_clean_comp_only", stats.confidence_warnings)
        self.assertIn("stale_single_comp", stats.confidence_warnings)
        self.assertIn("stale_evidence_only", stats.confidence_warnings)
        self.assertEqual(stats.clean_recent_comp_count, 0)
        self.assertEqual(stats.clean_stale_comp_count, 1)
        self.assertEqual(stats.sold_listing_recency_threshold_days, 180)


if __name__ == "__main__":
    unittest.main()
