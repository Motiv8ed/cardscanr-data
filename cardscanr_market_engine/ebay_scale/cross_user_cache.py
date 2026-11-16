from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any

GRADED_MARKERS = ("graded", "psa", "cgc", "bgs", "sgc", "ace")

HIGH_VALUE_AUD = 50.0
MID_VALUE_AUD = 5.0

FRESHNESS_DAYS_HIGH = 3
FRESHNESS_DAYS_MID = 7
FRESHNESS_DAYS_BULK = 30


def normalize_condition_band(condition: str, *, variant: str = "") -> str:
    text = f"{condition} {variant}".lower().replace("-", "_")
    if any(marker in text for marker in GRADED_MARKERS):
        return "graded"
    return "raw"


def cross_user_cache_key(
    *,
    catalogue_card_id: str | None,
    fingerprint: str,
    language: str,
    condition_band: str,
    market_country: str = "AU",
) -> str:
    """Stable cross-user cache identity: catalogue card + language + condition band (home market)."""
    card_id = str(catalogue_card_id or "").strip() or str(fingerprint or "").strip()
    if not card_id:
        raise ValueError("catalogue_card_id or fingerprint required")
    lang = str(language or "en").strip().lower()
    band = normalize_condition_band(condition_band)
    mkt = str(market_country or "AU").strip().upper()
    return f"{mkt}|{card_id}|{lang}|{band}"


def freshness_window_days(*, aud_value: float | None) -> int:
    value = float(aud_value) if aud_value is not None else 0.0
    if value >= HIGH_VALUE_AUD:
        return FRESHNESS_DAYS_HIGH
    if value >= MID_VALUE_AUD:
        return FRESHNESS_DAYS_MID
    return FRESHNESS_DAYS_BULK


def is_price_fresh_for_value(
    *,
    last_priced_at: datetime | None,
    aud_value: float | None,
    now: datetime | None = None,
) -> bool:
    if last_priced_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    if last_priced_at.tzinfo is None:
        last_priced_at = last_priced_at.replace(tzinfo=timezone.utc)
    window = timedelta(days=freshness_window_days(aud_value=aud_value))
    return last_priced_at + window > current.astimezone(timezone.utc)


@dataclass(frozen=True)
class CrossUserCacheLookup:
    skip_job: bool
    reason: str
    cache_key: str
    freshness_days: int


def evaluate_cross_user_cache_for_job(
    *,
    target: dict[str, Any],
    prior_cache: dict[str, Any] | None,
    now: datetime,
) -> CrossUserCacheLookup | None:
    """Return a skip decision when cache hit is fresh enough (AU catalogue card pricing)."""
    from .config import ebay_scale_cross_user_cache_enabled

    if not ebay_scale_cross_user_cache_enabled():
        return None
    catalogue_card_id = str(
        target.get("catalogue_card_id")
        or target.get("catalogueCardId")
        or (prior_cache or {}).get("catalogue_card_id")
        or ""
    ).strip()
    fingerprint = str(target.get("fingerprint") or (prior_cache or {}).get("fingerprint") or "").strip()
    language = str(target.get("language") or (prior_cache or {}).get("language") or "en").strip()
    condition = str(target.get("condition") or (prior_cache or {}).get("condition") or "raw")
    variant = str(target.get("variant") or (prior_cache or {}).get("variant") or "")
    market_country = str(target.get("market_country") or "AU").strip()
    if market_country.upper() != "AU":
        return None

    band = normalize_condition_band(condition, variant=variant)
    cache_key = cross_user_cache_key(
        catalogue_card_id=catalogue_card_id or None,
        fingerprint=fingerprint,
        language=language,
        condition_band=band,
        market_country=market_country,
    )
    price_raw = (prior_cache or {}).get("current_market_price")
    try:
        aud_value = float(price_raw) if price_raw is not None else None
    except (TypeError, ValueError):
        aud_value = None
    freshness_days = freshness_window_days(aud_value=aud_value)

    last_raw = (prior_cache or {}).get("last_updated_at") or (prior_cache or {}).get("verifiedSuccessFreshness")
    last_priced: datetime | None = None
    if last_raw:
        try:
            last_priced = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
        except ValueError:
            last_priced = None

    if is_price_fresh_for_value(last_priced_at=last_priced, aud_value=aud_value, now=now):
        return CrossUserCacheLookup(
            skip_job=True,
            reason="cross_user_cache_fresh",
            cache_key=cache_key,
            freshness_days=freshness_days,
        )
    return CrossUserCacheLookup(
        skip_job=False,
        reason="cross_user_cache_miss_or_stale",
        cache_key=cache_key,
        freshness_days=freshness_days,
    )