from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from typing import Any


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _parse_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _float_or_none(value: Any) -> float | None:
    if value is None or value is False:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


@dataclass(frozen=True)
class RefreshCooldownConfig:
    default_cooldown_hours: int
    high_value_cooldown_hours: int
    popular_cooldown_hours: int
    hot_card_cooldown_hours: int
    low_value_cooldown_hours: int

    @classmethod
    def from_env(cls) -> "RefreshCooldownConfig":
        return cls(
            default_cooldown_hours=_parse_positive_int("MARKET_REFRESH_DEFAULT_COOLDOWN_HOURS", 6),
            high_value_cooldown_hours=_parse_positive_int("MARKET_REFRESH_HIGH_VALUE_COOLDOWN_HOURS", 4),
            popular_cooldown_hours=_parse_positive_int("MARKET_REFRESH_POPULAR_COOLDOWN_HOURS", 4),
            hot_card_cooldown_hours=_parse_positive_int("MARKET_REFRESH_HOT_CARD_COOLDOWN_HOURS", 2),
            low_value_cooldown_hours=_parse_positive_int("MARKET_REFRESH_LOW_VALUE_COOLDOWN_HOURS", 12),
        )


@dataclass(frozen=True)
class RefreshPolicyDecision:
    cooldown_hours: int
    cooldown_until: datetime | None
    is_in_cooldown: bool
    reason: str
    can_refresh: bool
    cache_is_fresh: bool
    active_refresh_job: dict[str, Any] | None = None


def classify_cooldown(
    *,
    cache_row: dict[str, Any] | None,
    price_key_row: dict[str, Any] | None,
    config: RefreshCooldownConfig,
) -> tuple[int, str]:
    current_market_price = _float_or_none((cache_row or {}).get("current_market_price"))
    recommended_price = _float_or_none((cache_row or {}).get("recommended_price"))
    popularity_score = _int_or_zero((price_key_row or {}).get("popularity_score"))
    inventory_count = _int_or_zero((price_key_row or {}).get("inventory_count"))

    high_value = any(value is not None and value >= 100 for value in (current_market_price, recommended_price))
    popular = popularity_score >= 10 or inventory_count >= 10
    low_value_common = (
        current_market_price is not None
        and recommended_price is not None
        and current_market_price < 10
        and recommended_price < 10
        and popularity_score < 3
        and inventory_count < 3
    )

    if high_value and popular:
        return config.hot_card_cooldown_hours, "hot_card"
    if high_value:
        return config.high_value_cooldown_hours, "high_value"
    if popular:
        return config.popular_cooldown_hours, "popular"
    if low_value_common:
        return config.low_value_cooldown_hours, "low_value_common"
    return config.default_cooldown_hours, "default"


def calculate_refresh_policy(
    *,
    cache_row: dict[str, Any] | None,
    price_key_row: dict[str, Any] | None,
    active_job: dict[str, Any] | None,
    now: datetime,
    request_reason: str = "user_refresh",
    force: bool = False,
    config: RefreshCooldownConfig | None = None,
) -> RefreshPolicyDecision:
    cooldown_config = config or RefreshCooldownConfig.from_env()
    current_time = now.astimezone(timezone.utc)
    cooldown_hours, cooldown_reason = classify_cooldown(
        cache_row=cache_row,
        price_key_row=price_key_row,
        config=cooldown_config,
    )

    if active_job is not None:
        return RefreshPolicyDecision(
            cooldown_hours=cooldown_hours,
            cooldown_until=None,
            is_in_cooldown=False,
            reason="active_job_exists",
            can_refresh=False,
            cache_is_fresh=False,
            active_refresh_job=active_job,
        )

    last_updated_at = _parse_utc((cache_row or {}).get("last_updated_at"))
    if last_updated_at is None:
        return RefreshPolicyDecision(
            cooldown_hours=cooldown_hours,
            cooldown_until=None,
            is_in_cooldown=False,
            reason="no_cache" if cache_row is None else "cache_without_last_updated_at",
            can_refresh=True,
            cache_is_fresh=False,
        )

    cooldown_until = last_updated_at + timedelta(hours=cooldown_hours)
    is_in_cooldown = current_time < cooldown_until
    if force:
        return RefreshPolicyDecision(
            cooldown_hours=cooldown_hours,
            cooldown_until=cooldown_until,
            is_in_cooldown=is_in_cooldown,
            reason=f"force_refresh:{cooldown_reason}:{request_reason}",
            can_refresh=True,
            cache_is_fresh=is_in_cooldown,
        )

    return RefreshPolicyDecision(
        cooldown_hours=cooldown_hours,
        cooldown_until=cooldown_until,
        is_in_cooldown=is_in_cooldown,
        reason=cooldown_reason if is_in_cooldown else f"{cooldown_reason}_expired",
        can_refresh=not is_in_cooldown,
        cache_is_fresh=is_in_cooldown,
    )
