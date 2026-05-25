from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import MarketPriceKey, PricingStats, ProviderResult


def utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_cache_payload(
    *,
    price_key: MarketPriceKey,
    provider_result: ProviderResult,
    pricing_stats: PricingStats,
    snapshot_id: str,
    refreshed_at: datetime,
) -> dict[str, Any]:
    stale_after_iso = utc_iso(pricing_stats.stale_after)
    refreshed_at_iso = utc_iso(refreshed_at)
    raw_market_country = provider_result.raw_metadata.get("marketCountry")
    raw_currency = provider_result.raw_metadata.get("currency")
    return {
        "price_key_id": price_key.id,
        "current_market_price": pricing_stats.recommended_price,
        "median_price": pricing_stats.median_price,
        "low_price": pricing_stats.low_price,
        "average_price": pricing_stats.average_price,
        "high_price": pricing_stats.high_price,
        "recommended_price": pricing_stats.recommended_price,
        "sample_size": pricing_stats.sample_size,
        "confidence": pricing_stats.confidence,
        "provider": provider_result.provider_name,
        "marketplace": provider_result.marketplace,
        "market_country": str(raw_market_country or price_key.market_country or "").upper() or None,
        "currency": str(raw_currency or price_key.currency or "").upper() or None,
        "last_updated_at": refreshed_at_iso,
        "stale_after": stale_after_iso,
        "next_refresh_due_at": stale_after_iso,
        "refresh_status": "completed",
        "latest_snapshot_id": snapshot_id,
        "last_error_message": None,
    }
