"""FX freshness semantics for international market estimates."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_FX_MAX_AGE = timedelta(hours=24)
STATIC_FX_LAST_REVIEWED = datetime(2026, 5, 7, tzinfo=timezone.utc)
FX_RATE_STALE_NO_SAFE_CONVERSION = "FX_RATE_STALE_NO_SAFE_CONVERSION"


@dataclass(frozen=True)
class FxFreshness:
    source: str
    rate_timestamp: datetime
    max_age: timedelta
    stale: bool
    age_seconds: int
    health: str  # HEALTHY | WARNING | STALE
    allows_conversion: bool
    block_reason: str | None = None

    @property
    def age_hours(self) -> float:
        return self.age_seconds / 3600.0


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
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


def resolve_rate_timestamp(*, rate_source: str, now: datetime) -> datetime:
    """Resolve the effective FX rate timestamp.

    Operator-supplied MARKET_CURRENCY_RATE_TIMESTAMP wins.
    A rates-cache file with fetched_at is next.
    Static configured rates fall back to the last-reviewed static date.
    same_currency conversions use ``now`` (always fresh).
    """
    env_ts = _parse_iso(os.getenv("MARKET_CURRENCY_RATE_TIMESTAMP", ""))
    if env_ts is not None:
        return env_ts

    cache_path = os.getenv("MARKET_CURRENCY_RATES_CACHE_PATH", "").strip()
    if cache_path:
        path = Path(cache_path)
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                cached = _parse_iso(payload.get("fetched_at") or payload.get("rateTimestamp"))
                if cached is not None:
                    return cached
            except Exception:
                pass

    normalized = str(rate_source or "").strip().lower()
    if normalized in {"same_currency"}:
        return now
    if normalized in {
        "configured_static_rates",
        "static",
        "configured_static_rates_via_aud",
    }:
        return STATIC_FX_LAST_REVIEWED
    return now


def evaluate_fx_freshness(
    *,
    rate_source: str,
    rate_timestamp: datetime | None,
    now: datetime,
    max_age: timedelta = DEFAULT_FX_MAX_AGE,
    same_currency: bool = False,
) -> FxFreshness:
    if same_currency or str(rate_source or "").strip().lower() == "same_currency":
        return FxFreshness(
            source="same_currency",
            rate_timestamp=now,
            max_age=max_age,
            stale=False,
            age_seconds=0,
            health="HEALTHY",
            allows_conversion=True,
            block_reason=None,
        )

    timestamp = rate_timestamp or resolve_rate_timestamp(rate_source=rate_source, now=now)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age = max(0, int((now - timestamp.astimezone(timezone.utc)).total_seconds()))
    stale = timedelta(seconds=age) > max_age
    warning = (not stale) and timedelta(seconds=age) > (max_age * 0.75)
    if stale:
        health = "STALE"
        allows = False
        block_reason = FX_RATE_STALE_NO_SAFE_CONVERSION
    elif warning:
        health = "WARNING"
        allows = True
        block_reason = None
    else:
        health = "HEALTHY"
        allows = True
        block_reason = None
    return FxFreshness(
        source=rate_source,
        rate_timestamp=timestamp.astimezone(timezone.utc),
        max_age=max_age,
        stale=stale,
        age_seconds=age,
        health=health,
        allows_conversion=allows,
        block_reason=block_reason,
    )


def assert_fx_allows_international_conversion(fx: FxFreshness) -> None:
    if not fx.allows_conversion:
        raise ValueError(fx.block_reason or FX_RATE_STALE_NO_SAFE_CONVERSION)


def fx_health_payload(*, rate_source: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    fx = evaluate_fx_freshness(rate_source=rate_source, rate_timestamp=None, now=now)
    return {
        "source": fx.source,
        "lastSuccessfulRefresh": fx.rate_timestamp.isoformat().replace("+00:00", "Z"),
        "ageHours": round(fx.age_hours, 2),
        "maxAgeHours": int(fx.max_age.total_seconds() // 3600),
        "health": fx.health,
        "stale": fx.stale,
        "allowsConversion": fx.allows_conversion,
        "blockReason": fx.block_reason,
        "currencies": ["AUD", "USD", "EUR", "GBP", "NZD", "JPY", "CAD"],
        "note": (
            "Operator must supply MARKET_CURRENCY_RATES_JSON with MARKET_CURRENCY_RATE_TIMESTAMP "
            "(or a rates cache file) within the max age window. Static May-2026 rates are blocked "
            "for new international estimate conversions."
            if fx.stale
            else None
        ),
    }
