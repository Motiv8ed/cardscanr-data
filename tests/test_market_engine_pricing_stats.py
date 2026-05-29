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
            sold_date=datetime(2026, 5, 20, tzinfo=timezone.utc),
            listing_url="https://example.test/listing",
            condition_text="Raw",
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


if __name__ == "__main__":
    unittest.main()
