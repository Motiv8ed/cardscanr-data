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
    totals = [item.comp.total_price for item in included]
    average_match_score = mean([item.match_score for item in included]) if included else 0.0
    confidence = determine_confidence(included_count=len(included), average_match_score=average_match_score)
    stale_after = calculate_stale_after(
        now=current_time,
        included_count=len(included),
        confidence=confidence,
        config=config,
    )
    if not totals:
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
        )
    median_price = round_money(median(totals))
    average_price = round_money(mean(totals))
    low_price = round_money(min(totals))
    high_price = round_money(max(totals))
    return PricingStats(
        median_price=median_price,
        average_price=average_price,
        low_price=low_price,
        high_price=high_price,
        recommended_price=median_price,
        sample_size=len(included),
        included_count=len(included),
        rejected_count=len(rejected),
        confidence=confidence,
        stale_after=stale_after,
    )
