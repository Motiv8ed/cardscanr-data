from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from statistics import mean, median

from .config import MarketEngineConfig
from .fingerprints import normalize_market_variant
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


def sold_listing_recency_threshold_days() -> int:
    raw = os.getenv("MARKET_PRICE_SOLD_LISTING_RECENCY_DAYS", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 180


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
    recency_threshold_days = sold_listing_recency_threshold_days()
    recency_cutoff = current_time - timedelta(days=recency_threshold_days)
    clean_dates = [item.comp.sold_date for item in included if item.comp.sold_date is not None]
    recent_included = [item for item in included if item.comp.sold_date >= recency_cutoff]
    stale_included = [item for item in included if item.comp.sold_date < recency_cutoff]
    single_clean_comp_only = len(included) == 1
    stale_evidence_only = bool(included) and len(recent_included) == 0
    average_match_score = mean([item.match_score for item in included]) if included else 0.0
    confidence = determine_confidence(included_count=len(included), average_match_score=average_match_score)
    spread_ratio = round(max(item_prices) / min(item_prices), 4) if item_prices and min(item_prices) > 0 else None
    confidence_warnings: list[str] = []
    requested_variants = {
        normalize_market_variant(item.comp.raw_metadata.get("requested_variant"))
        for item in evaluated_comps
        if item.comp.raw_metadata.get("requested_variant")
    }
    if any(variant in {"non_holo", "holo", "reverse_holo"} for variant in requested_variants) and len(included) < 5:
        confidence_warnings.append("insufficient_variant_specific_comps")
    if single_clean_comp_only:
        confidence_warnings.append("single_clean_comp_only")
        if stale_included:
            confidence_warnings.append("stale_single_comp")
    if stale_evidence_only:
        confidence_warnings.append("stale_evidence_only")
    if spread_ratio is not None and spread_ratio > 3 and len(included) < 10:
        confidence_warnings.append("wide_item_price_spread_small_sample")
        if confidence == "high":
            confidence = "medium"
    if spread_ratio is not None and spread_ratio > 5:
        confidence_warnings.append("extreme_item_price_spread")
    no_reliable_price_reason: str | None = None
    if not included:
        noisy_identity_rejections = {
            "wrong_card_name",
            "wrong_variant",
            "wrong_variant_holo",
            "wrong_variant_reverse_holo",
            "weak_variant_match",
            "wrong_language",
        }
        rejected_reasons = {item.rejection_reason for item in rejected if item.rejection_reason}
        conflicting_collector_rejection = any(
            item.rejection_reason == "wrong_collector_number"
            and bool(item.comp.raw_metadata.get("detected_collector_numbers"))
            for item in rejected
        )
        if conflicting_collector_rejection or rejected_reasons & noisy_identity_rejections:
            no_reliable_price_reason = "no_clean_exact_comps"
        else:
            no_reliable_price_reason = "all_comps_rejected" if rejected else "no_comps_parsed"
        confidence_warnings.append(no_reliable_price_reason)
    elif stale_evidence_only:
        no_reliable_price_reason = "stale_single_comp_only" if single_clean_comp_only else "stale_evidence_only"
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
            price_spread_ratio=spread_ratio,
            confidence_warnings=tuple(confidence_warnings),
            included_price_distribution=tuple(sorted(item_prices)),
            no_reliable_price_reason=no_reliable_price_reason,
            price_reliability="no_reliable_price",
            sold_listing_recency_threshold_days=recency_threshold_days,
            clean_recent_comp_count=len(recent_included),
            clean_stale_comp_count=len(stale_included),
            oldest_clean_comp_date=min(clean_dates) if clean_dates else None,
            newest_clean_comp_date=max(clean_dates) if clean_dates else None,
        )
    item_median_price = round_money(median(item_prices))
    item_average_price = round_money(mean(item_prices))
    item_low_price = round_money(min(item_prices))
    item_high_price = round_money(max(item_prices))
    landed_median_price = round_money(median(landed_prices))
    landed_average_price = round_money(mean(landed_prices))
    landed_low_price = round_money(min(landed_prices))
    landed_high_price = round_money(max(landed_prices))
    price_reliability = "reliable"
    recommended_price = item_median_price
    item_recommended_price = item_median_price
    landed_recommended_price = landed_median_price
    price_basis = "item_price"
    if single_clean_comp_only:
        price_reliability = "single_comp_low_confidence"
    if stale_evidence_only:
        price_reliability = "stale_single_comp" if single_clean_comp_only else "stale_evidence_only"
        recommended_price = None
        item_recommended_price = None
        landed_recommended_price = None
        price_basis = price_reliability
    return PricingStats(
        median_price=item_median_price,
        average_price=item_average_price,
        low_price=item_low_price,
        high_price=item_high_price,
        recommended_price=recommended_price,
        sample_size=len(included),
        included_count=len(included),
        rejected_count=len(rejected),
        confidence=confidence,
        stale_after=stale_after,
        item_median_price=item_median_price,
        item_average_price=item_average_price,
        item_low_price=item_low_price,
        item_high_price=item_high_price,
        item_recommended_price=item_recommended_price,
        landed_median_price=landed_median_price,
        landed_average_price=landed_average_price,
        landed_low_price=landed_low_price,
        landed_high_price=landed_high_price,
        landed_recommended_price=landed_recommended_price,
        price_basis=price_basis,
        price_spread_ratio=spread_ratio,
        confidence_warnings=tuple(confidence_warnings),
        included_price_distribution=tuple(sorted(item_prices)),
        no_reliable_price_reason=no_reliable_price_reason,
        price_reliability=price_reliability,
        sold_listing_recency_threshold_days=recency_threshold_days,
        clean_recent_comp_count=len(recent_included),
        clean_stale_comp_count=len(stale_included),
        oldest_clean_comp_date=min(clean_dates) if clean_dates else None,
        newest_clean_comp_date=max(clean_dates) if clean_dates else None,
    )
