"""Determine when a shared price key may enter international fallback."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..bulk.price_semantics import is_reference_provider, is_verified_provider
from .market_fallback_policy import fallback_markets_for_key
from ..models import MarketPriceKey


VERIFIED_DISPLAY_SOURCES = frozenset({"verified_au", "verified_local", "local_verified"})
REFERENCE_DISPLAY_SOURCES = frozenset({"reference"})
INTERNATIONAL_DISPLAY_SOURCES = frozenset({"international_estimate"})


@dataclass(frozen=True)
class InternationalFallbackEligibility:
    eligible: bool
    reason: str
    fallback_markets: tuple[str, ...]
    diagnostics: dict[str, Any]


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _has_sufficient_local_or_reference(cache: dict[str, Any] | None) -> bool:
    if not cache:
        return False
    display_source = str(cache.get("display_price_source") or "").strip().lower()
    price = cache.get("current_market_price") or cache.get("recommended_price")
    has_price = price is not None and float(price) > 0
    if not has_price:
        return False
    if display_source in VERIFIED_DISPLAY_SOURCES:
        return True
    if display_source in REFERENCE_DISPLAY_SOURCES and not bool(cache.get("verification_required")):
        return True
    provider = str(cache.get("provider") or "").strip().lower()
    if is_verified_provider(provider) and has_price:
        return True
    if is_reference_provider(provider) and has_price and display_source != "pending_verification":
        return True
    return False


def _has_fresh_international_estimate(cache: dict[str, Any] | None, *, now: datetime) -> bool:
    if not cache:
        return False
    display_source = str(cache.get("display_price_source") or "").strip().lower()
    if display_source not in INTERNATIONAL_DISPLAY_SOURCES:
        return False
    price = cache.get("current_market_price") or cache.get("recommended_price")
    if price is None or float(price) <= 0:
        return False
    due = _parse_iso(cache.get("next_refresh_due_at") or cache.get("stale_after"))
    if due is not None and due > now:
        return True
    return False


def _is_in_international_backoff(cache: dict[str, Any] | None, *, now: datetime) -> bool:
    if not cache:
        return False
    message = str(cache.get("last_error_message") or "")
    if not message.startswith("international_fallback_exhausted"):
        return False
    due = _parse_iso(cache.get("next_refresh_due_at"))
    return due is not None and due > now


def evaluate_international_fallback_eligibility(
    *,
    price_key: MarketPriceKey,
    cache: dict[str, Any] | None,
    now: datetime | None = None,
) -> InternationalFallbackEligibility:
    now = now or datetime.now(timezone.utc)
    fallback_markets = fallback_markets_for_key(price_key)
    if not fallback_markets:
        return InternationalFallbackEligibility(
            eligible=False,
            reason="no_compatible_fallback_markets",
            fallback_markets=(),
            diagnostics={"language": price_key.language, "homeMarket": price_key.market_country},
        )
    if _has_sufficient_local_or_reference(cache):
        return InternationalFallbackEligibility(
            eligible=False,
            reason="local_or_reference_sufficient",
            fallback_markets=fallback_markets,
            diagnostics={"displayPriceSource": cache.get("display_price_source") if cache else None},
        )
    if _has_fresh_international_estimate(cache, now=now):
        return InternationalFallbackEligibility(
            eligible=False,
            reason="fresh_international_estimate",
            fallback_markets=fallback_markets,
            diagnostics={"displayPriceSource": "international_estimate"},
        )
    if _is_in_international_backoff(cache, now=now):
        return InternationalFallbackEligibility(
            eligible=False,
            reason="international_backoff",
            fallback_markets=fallback_markets,
            diagnostics={"nextRefreshDueAt": cache.get("next_refresh_due_at") if cache else None},
        )
    return InternationalFallbackEligibility(
        eligible=True,
        reason="eligible",
        fallback_markets=fallback_markets,
        diagnostics={"homeMarket": price_key.market_country, "language": price_key.language},
    )
