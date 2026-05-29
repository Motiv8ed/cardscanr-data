from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean, median

from .config import MarketEngineConfig
from .models import EvaluatedComp, PricingStats


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def round_money(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def determine_confidence(*, included_count: int, average_match_score: float) -> str:
    if included_count >= 8 and average_match_score >= 0.85:
        return "high"
    if included_count >= 3:
        return "medium"
    return "low"


def calculate_stale_after(
    *,
    now: datetime,
    included_count: int,
    confidence: str,
    config: MarketEngineConfig,
) -> datetime:
    if included_count <= 0:
        hours = config.no_comps_hours
    elif confidence == "high":
        hours = config.high_confidence_hours
    elif confidence == "medium":
        hours = config.medium_confidence_hours
    else:
        hours = config.low_confidence_hours
    return now + timedelta(hours=hours)


def calculate_pricing_stats(
    evaluated_comps: list[EvaluatedComp],
    *,
    now: datetime | None = None,
    config: MarketEngineConfig,
) -> PricingStats:
    current_time = now or utc_now()
    included = [item for item in evaluated_comps if item.included_in_estimate]
    rejected = [item for item in evaluated_comps if not item.included_in_estimate]
    item_prices = [item.comp.sold_price for item in included]
    landed_prices = [item.comp.total_price for item in included]
    average_match_score = mean([item.match_score for item in included]) if included else 0.0
    confidence = determine_confidence(included_count=len(included), average_match_score=average_match_score)
    stale_after = calculate_stale_after(
        now=current_time,
        included_count=len(included),
        confidence=confidence,
        config=config,
    )
    if not item_prices:
        return PricingStats(
            median_price=None,
            average_price=None,
            low_price=None,
            high_price=None,
            recommended_price=None,
            sample_size=0,
            included_count=0,
            rejected_count=len(rejected),
            confidence=confidence,
            stale_after=stale_after,
            item_median_price=None,
            item_average_price=None,
            item_low_price=None,
            item_high_price=None,
            item_recommended_price=None,
            landed_median_price=None,
            landed_average_price=None,
            landed_low_price=None,
            landed_high_price=None,
            landed_recommended_price=None,
            price_basis="item_price",
        )
    item_median_price = round_money(median(item_prices))
    item_average_price = round_money(mean(item_prices))
    item_low_price = round_money(min(item_prices))
    item_high_price = round_money(max(item_prices))
    landed_median_price = round_money(median(landed_prices))
    landed_average_price = round_money(mean(landed_prices))
    landed_low_price = round_money(min(landed_prices))
    landed_high_price = round_money(max(landed_prices))
    return PricingStats(
        median_price=item_median_price,
        average_price=item_average_price,
        low_price=item_low_price,
        high_price=item_high_price,
        recommended_price=item_median_price,
        sample_size=len(included),
        included_count=len(included),
        rejected_count=len(rejected),
        confidence=confidence,
        stale_after=stale_after,
        item_median_price=item_median_price,
        item_average_price=item_average_price,
        item_low_price=item_low_price,
        item_high_price=item_high_price,
        item_recommended_price=item_median_price,
        landed_median_price=landed_median_price,
        landed_average_price=landed_average_price,
        landed_low_price=landed_low_price,
        landed_high_price=landed_high_price,
        landed_recommended_price=landed_median_price,
        price_basis="item_price",
    )
