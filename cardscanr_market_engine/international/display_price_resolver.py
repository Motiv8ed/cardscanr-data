"""Resolve user-facing price class and presentation from cache rows."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..bulk.price_semantics import is_reference_provider, is_verified_provider
from .market_fallback_policy import market_display_name


PriceClass = str  # local_verified | reference | international_estimate | unavailable | pending_verification


@dataclass(frozen=True)
class PricePresentation:
    price_class: PriceClass
    display_price: float | None
    low_price: float | None
    high_price: float | None
    currency: str | None
    label: str
    short_label: str
    source_market: str | None
    source_market_name: str | None
    source_currency: str | None
    source_price: float | None
    fx_rate: float | None
    fx_rate_timestamp: str | None
    explanation: str | None
    disclaimer: str | None
    show_estimate_suffix: bool
    unavailable_reason: str | None


def _home_market_name(cache: dict[str, Any], key_row: dict[str, Any] | None) -> str:
    country = str(cache.get("market_country") or (key_row or {}).get("market_country") or "").upper()
    return market_display_name(country)


def resolve_price_presentation(
    *,
    cache: dict[str, Any] | None,
    key_row: dict[str, Any] | None = None,
) -> PricePresentation:
    if not cache:
        return PricePresentation(
            price_class="unavailable",
            display_price=None,
            low_price=None,
            high_price=None,
            currency=(key_row or {}).get("currency"),
            label="Price unavailable",
            short_label="Unavailable",
            source_market=None,
            source_market_name=None,
            source_currency=None,
            source_price=None,
            fx_rate=None,
            fx_rate_timestamp=None,
            explanation="We couldn't find enough reliable pricing evidence for this card yet.",
            disclaimer=None,
            show_estimate_suffix=False,
            unavailable_reason="missing_cache",
        )

    currency = str(cache.get("currency") or (key_row or {}).get("currency") or "").upper() or None
    display_source = str(cache.get("display_price_source") or "").strip().lower()
    provider = str(cache.get("provider") or "").strip().lower()
    price = cache.get("current_market_price") or cache.get("recommended_price")
    has_price = price is not None and float(price) > 0
    home_name = _home_market_name(cache, key_row)
    low = cache.get("low_price")
    high = cache.get("high_price")
    confidence = str(cache.get("confidence") or "").lower()

    if display_source == "pending_verification" or bool(cache.get("verification_required")):
        return PricePresentation(
            price_class="pending_verification",
            display_price=float(price) if has_price else None,
            low_price=float(low) if low is not None else None,
            high_price=float(high) if high is not None else None,
            currency=currency,
            label="Pending verification",
            short_label="Pending",
            source_market=str(cache.get("market_country") or "").upper() or None,
            source_market_name=home_name,
            source_currency=currency,
            source_price=float(price) if has_price else None,
            fx_rate=None,
            fx_rate_timestamp=None,
            explanation="This price change needs verification before it becomes the primary display value.",
            disclaimer=None,
            show_estimate_suffix=False,
            unavailable_reason=None,
        )

    if display_source == "international_estimate" and has_price:
        source_market = str(cache.get("source_market_country") or cache.get("international_source_market") or "").upper() or None
        source_market_name = market_display_name(source_market) if source_market else None
        source_currency = str(cache.get("source_currency") or "").upper() or None
        source_price = cache.get("source_price")
        show_range = confidence == "medium" and low is not None and high is not None
        return PricePresentation(
            price_class="international_estimate",
            display_price=float(price),
            low_price=float(low) if show_range else None,
            high_price=float(high) if show_range else None,
            currency=currency,
            label="International estimate",
            short_label="International",
            source_market=source_market,
            source_market_name=source_market_name,
            source_currency=source_currency,
            source_price=float(source_price) if source_price is not None else None,
            fx_rate=float(cache["fx_rate"]) if cache.get("fx_rate") is not None else None,
            fx_rate_timestamp=str(cache.get("fx_rate_timestamp") or "") or None,
            explanation=(
                f"No reliable {home_name} pricing was found for this card. "
                f"This estimate is based on recent {source_market_name or 'foreign'} market pricing "
                f"and converted to {currency or 'your currency'}."
            ),
            disclaimer=(
                "Shipping, taxes, import costs and regional market differences are not included."
            ),
            show_estimate_suffix=not show_range,
            unavailable_reason=None,
        )

    if display_source in {"verified_au", "verified_local", "local_verified"} or is_verified_provider(provider):
        return PricePresentation(
            price_class="local_verified",
            display_price=float(price) if has_price else None,
            low_price=float(low) if low is not None else None,
            high_price=float(high) if high is not None else None,
            currency=currency,
            label="Local market price",
            short_label="Local market",
            source_market=str(cache.get("market_country") or "").upper() or None,
            source_market_name=home_name,
            source_currency=currency,
            source_price=float(price) if has_price else None,
            fx_rate=None,
            fx_rate_timestamp=None,
            explanation=f"Based on {home_name} market evidence.",
            disclaimer="Shipping is not included unless explicitly shown as a delivered cost.",
            show_estimate_suffix=False,
            unavailable_reason=None,
        )

    if display_source == "reference" or is_reference_provider(provider):
        return PricePresentation(
            price_class="reference",
            display_price=float(price) if has_price else None,
            low_price=float(low) if low is not None else None,
            high_price=float(high) if high is not None else None,
            currency=currency,
            label="Reference price",
            short_label="Reference",
            source_market=None,
            source_market_name=None,
            source_currency=str(cache.get("reference_provider") or provider or "").upper() or None,
            source_price=cache.get("reference_price"),
            fx_rate=None,
            fx_rate_timestamp=None,
            explanation="Based on recognised card-market/reference pricing.",
            disclaimer=None,
            show_estimate_suffix=False,
            unavailable_reason=None if has_price else "reference_missing",
        )

    if has_price:
        return PricePresentation(
            price_class="local_verified",
            display_price=float(price),
            low_price=float(low) if low is not None else None,
            high_price=float(high) if high is not None else None,
            currency=currency,
            label="Local market price",
            short_label="Local market",
            source_market=str(cache.get("market_country") or "").upper() or None,
            source_market_name=home_name,
            source_currency=currency,
            source_price=float(price),
            fx_rate=None,
            fx_rate_timestamp=None,
            explanation=f"Based on {home_name} market evidence.",
            disclaimer=None,
            show_estimate_suffix=False,
            unavailable_reason=None,
        )

    return PricePresentation(
        price_class="unavailable",
        display_price=None,
        low_price=None,
        high_price=None,
        currency=currency,
        label="Price unavailable",
        short_label="Unavailable",
        source_market=None,
        source_market_name=None,
        source_currency=None,
        source_price=None,
        fx_rate=None,
        fx_rate_timestamp=None,
        explanation="We couldn't find enough reliable pricing evidence for this card yet.",
        disclaimer=None,
        show_estimate_suffix=False,
        unavailable_reason=str(cache.get("last_error_message") or "no_price"),
    )
