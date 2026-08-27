"""Shared ECB FX cache and freshness for international estimates."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import REPORTS_DIR
from .ecb_client import (
    ECB_SOURCE_LABEL,
    ECB_SOURCE_NAME,
    EcbFxSnapshot,
    fetch_ecb_snapshot,
)

FX_RATE_STALE_NO_SAFE_CONVERSION = "FX_RATE_STALE_NO_SAFE_CONVERSION"
DEFAULT_CACHE_PATH = REPORTS_DIR / "runtime" / "ecb_fx_rates.json"
# CardScanR must successfully check ECB within this window for HEALTHY.
DEFAULT_FETCH_MAX_AGE = timedelta(hours=36)
# After fetch failures, keep converting with last-known-good for this grace.
DEFAULT_OUTAGE_GRACE = timedelta(hours=96)
# Provider publication date alone may lag over weekends/holidays; still OK if fetch is fresh.
DEFAULT_PROVIDER_MAX_AGE = timedelta(days=5)


@dataclass(frozen=True)
class FxFreshness:
    source: str
    rate_timestamp: datetime  # effective timestamp for conversion metadata (fetched_at preferred)
    provider_rate_date: date | None
    fetched_at: datetime | None
    max_age: timedelta
    stale: bool
    age_seconds: int
    health: str  # HEALTHY | WARNING | STALE
    allows_conversion: bool
    block_reason: str | None = None
    currencies: tuple[str, ...] = ()

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


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def resolve_fx_cache_path() -> Path:
    raw = os.getenv("MARKET_CURRENCY_RATES_CACHE_PATH", "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else (REPORTS_DIR.parent / path)
    return DEFAULT_CACHE_PATH


def load_fx_cache(path: Path | None = None) -> dict[str, Any] | None:
    cache_path = path or resolve_fx_cache_path()
    if not cache_path.is_file():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def save_fx_cache(payload: dict[str, Any], path: Path | None = None) -> Path:
    cache_path = path or resolve_fx_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(cache_path)
    return cache_path


def snapshot_from_cache(payload: dict[str, Any]) -> EcbFxSnapshot | None:
    if str(payload.get("source") or "").upper() != ECB_SOURCE_NAME:
        return None
    provider_date = _parse_date(payload.get("providerRateDate") or payload.get("provider_rate_date"))
    fetched_at = _parse_iso(payload.get("fetchedAt") or payload.get("fetched_at"))
    eur_rates_raw = payload.get("eurRates") or payload.get("eur_rates") or {}
    if provider_date is None or fetched_at is None or not isinstance(eur_rates_raw, dict):
        return None
    from decimal import Decimal

    eur_rates = {str(k).upper(): Decimal(str(v)) for k, v in eur_rates_raw.items()}
    return EcbFxSnapshot(
        source=ECB_SOURCE_NAME,
        source_url=str(payload.get("sourceUrl") or ""),
        provider_rate_date=provider_date,
        fetched_at=fetched_at,
        eur_rates=eur_rates,
    )


def evaluate_ecb_fx_freshness(
    *,
    cache: dict[str, Any] | None,
    now: datetime,
    fetch_max_age: timedelta = DEFAULT_FETCH_MAX_AGE,
    outage_grace: timedelta = DEFAULT_OUTAGE_GRACE,
    provider_max_age: timedelta = DEFAULT_PROVIDER_MAX_AGE,
    same_currency: bool = False,
) -> FxFreshness:
    if same_currency:
        return FxFreshness(
            source="same_currency",
            rate_timestamp=now,
            provider_rate_date=None,
            fetched_at=now,
            max_age=fetch_max_age,
            stale=False,
            age_seconds=0,
            health="HEALTHY",
            allows_conversion=True,
            block_reason=None,
            currencies=("AUD", "USD", "EUR", "GBP", "CAD", "JPY", "NZD"),
        )

    if not cache:
        return FxFreshness(
            source="missing",
            rate_timestamp=now,
            provider_rate_date=None,
            fetched_at=None,
            max_age=fetch_max_age,
            stale=True,
            age_seconds=0,
            health="STALE",
            allows_conversion=False,
            block_reason=FX_RATE_STALE_NO_SAFE_CONVERSION,
        )

    source = str(cache.get("source") or "").upper() or "unknown"
    # Never treat legacy static May rates as production FX for international estimates.
    if source in {"CONFIGURED_STATIC_RATES", "STATIC"}:
        return FxFreshness(
            source=source.lower(),
            rate_timestamp=now,
            provider_rate_date=None,
            fetched_at=None,
            max_age=fetch_max_age,
            stale=True,
            age_seconds=0,
            health="STALE",
            allows_conversion=False,
            block_reason=FX_RATE_STALE_NO_SAFE_CONVERSION,
            currencies=tuple(cache.get("currencies") or ()),
        )

    fetched_at = _parse_iso(cache.get("fetchedAt") or cache.get("fetched_at"))
    provider_date = _parse_date(cache.get("providerRateDate") or cache.get("provider_rate_date"))
    currencies = tuple(str(c).upper() for c in (cache.get("currencies") or []))
    if fetched_at is None:
        return FxFreshness(
            source=source,
            rate_timestamp=now,
            provider_rate_date=provider_date,
            fetched_at=None,
            max_age=fetch_max_age,
            stale=True,
            age_seconds=0,
            health="STALE",
            allows_conversion=False,
            block_reason=FX_RATE_STALE_NO_SAFE_CONVERSION,
            currencies=currencies,
        )

    fetch_age = now - fetched_at.astimezone(timezone.utc)
    age_seconds = max(0, int(fetch_age.total_seconds()))
    provider_age = None
    if provider_date is not None:
        provider_age = timedelta(days=(now.date() - provider_date).days)

    last_error = str(cache.get("lastError") or cache.get("last_error") or "").strip()
    last_attempt = _parse_iso(cache.get("lastAttemptAt") or cache.get("last_attempt_at"))

    # HEALTHY: recent successful fetch + provider date not absurdly old.
    if fetch_age <= fetch_max_age and (provider_age is None or provider_age <= provider_max_age):
        return FxFreshness(
            source=source,
            rate_timestamp=fetched_at,
            provider_rate_date=provider_date,
            fetched_at=fetched_at,
            max_age=fetch_max_age,
            stale=False,
            age_seconds=age_seconds,
            health="HEALTHY",
            allows_conversion=True,
            block_reason=None,
            currencies=currencies,
        )

    # WARNING: fetch older than ideal but still within outage grace; last-known-good usable.
    if fetch_age <= outage_grace and (provider_age is None or provider_age <= provider_max_age):
        return FxFreshness(
            source=source,
            rate_timestamp=fetched_at,
            provider_rate_date=provider_date,
            fetched_at=fetched_at,
            max_age=fetch_max_age,
            stale=False,
            age_seconds=age_seconds,
            health="WARNING",
            allows_conversion=True,
            block_reason=None,
            currencies=currencies,
        )

    return FxFreshness(
        source=source,
        rate_timestamp=fetched_at,
        provider_rate_date=provider_date,
        fetched_at=fetched_at,
        max_age=fetch_max_age,
        stale=True,
        age_seconds=age_seconds,
        health="STALE",
        allows_conversion=False,
        block_reason=FX_RATE_STALE_NO_SAFE_CONVERSION,
        currencies=currencies,
    )


def assert_fx_allows_international_conversion(fx: FxFreshness) -> None:
    if not fx.allows_conversion:
        raise ValueError(fx.block_reason or FX_RATE_STALE_NO_SAFE_CONVERSION)


def evaluate_fx_freshness(
    *,
    rate_source: str | None = None,
    rate_timestamp: datetime | None = None,
    now: datetime,
    max_age: timedelta | None = None,
    same_currency: bool = False,
    cache: dict[str, Any] | None = None,
) -> FxFreshness:
    """Compatibility wrapper used by fallback_runner / pilot."""
    _ = rate_source, rate_timestamp, max_age
    payload = cache if cache is not None else load_fx_cache()
    return evaluate_ecb_fx_freshness(
        cache=payload,
        now=now,
        same_currency=same_currency,
    )


def resolve_rate_timestamp(*, rate_source: str, now: datetime) -> datetime:
    cache = load_fx_cache()
    fx = evaluate_ecb_fx_freshness(cache=cache, now=now)
    if fx.fetched_at is not None:
        return fx.fetched_at
    if str(rate_source or "").lower() == "same_currency":
        return now
    return now


def fx_health_payload(*, rate_source: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    _ = rate_source
    now = now or datetime.now(timezone.utc)
    cache = load_fx_cache() or {}
    fx = evaluate_ecb_fx_freshness(cache=cache or None, now=now)
    return {
        "source": ECB_SOURCE_NAME if fx.source in {ECB_SOURCE_NAME, "ECB"} else fx.source,
        "sourceLabel": ECB_SOURCE_LABEL,
        "providerRateDate": fx.provider_rate_date.isoformat() if fx.provider_rate_date else None,
        "lastSuccessfulRefresh": fx.fetched_at.isoformat().replace("+00:00", "Z") if fx.fetched_at else None,
        "lastChecked": (cache.get("lastAttemptAt") or cache.get("fetchedAt")),
        "ageHours": round(fx.age_hours, 2),
        "fetchMaxAgeHours": int(DEFAULT_FETCH_MAX_AGE.total_seconds() // 3600),
        "outageGraceHours": int(DEFAULT_OUTAGE_GRACE.total_seconds() // 3600),
        "health": fx.health,
        "stale": fx.stale,
        "allowsConversion": fx.allows_conversion,
        "blockReason": fx.block_reason,
        "currencies": list(fx.currencies or cache.get("currencies") or []),
        "refreshFailures": int(cache.get("consecutiveFailures") or 0),
        "lastError": cache.get("lastError"),
        "note": (
            "ECB publishes on working days; weekends/holidays reuse the latest official provider rate "
            "while CardScanR fetch/check remains within the freshness window."
            if fx.health in {"HEALTHY", "WARNING"}
            else "No acceptable ECB FX cache; international conversions are blocked."
        ),
    }


def refresh_ecb_fx_cache(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    fetch_fn=fetch_ecb_snapshot,
) -> dict[str, Any]:
    """Fetch ECB rates once and write the shared cache. Safe for many workers to read."""
    now = now or datetime.now(timezone.utc)
    cache_path = path or resolve_fx_cache_path()
    lock_path = cache_path.with_suffix(cache_path.suffix + ".lock")
    prior = load_fx_cache(cache_path) or {}
    # Simple non-blocking overlap guard via lock file age.
    if lock_path.is_file():
        try:
            lock_age = now.timestamp() - lock_path.stat().st_mtime
            if lock_age < 120:
                current = load_fx_cache(cache_path) or prior
                current["lastAttemptAt"] = now.isoformat().replace("+00:00", "Z")
                current["lastError"] = current.get("lastError") or "refresh_overlap_skipped"
                return current
        except OSError:
            pass
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(now.isoformat(), encoding="utf-8")
    try:
        snapshot = fetch_fn(now=now)
        payload = snapshot.to_cache_payload()
        payload.update(
            {
                "lastAttemptAt": now.isoformat().replace("+00:00", "Z"),
                "lastError": None,
                "consecutiveFailures": 0,
                "status": "success",
            }
        )
        save_fx_cache(payload, cache_path)
        return payload
    except Exception as exc:
        failed = dict(prior)
        failed.update(
            {
                "lastAttemptAt": now.isoformat().replace("+00:00", "Z"),
                "lastError": str(exc),
                "consecutiveFailures": int(prior.get("consecutiveFailures") or 0) + 1,
                "status": "failed",
            }
        )
        # Preserve last-known-good rates for WARNING grace if present.
        if "pairRates" not in failed and "eurRates" not in failed:
            failed.setdefault("source", ECB_SOURCE_NAME)
        save_fx_cache(failed, cache_path)
        raise
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def maybe_refresh_ecb_fx_cache(
    *,
    ttl: timedelta = timedelta(hours=12),
    force: bool = False,
    now: datetime | None = None,
    path: Path | None = None,
    fetch_fn=fetch_ecb_snapshot,
) -> dict[str, Any]:
    """Refresh ECB cache when forced or older than TTL; otherwise reuse shared cache."""
    now = now or datetime.now(timezone.utc)
    cache = load_fx_cache(path)
    if not force and cache:
        fx = evaluate_ecb_fx_freshness(cache=cache, now=now)
        if fx.allows_conversion and fx.fetched_at is not None and (now - fx.fetched_at) <= ttl:
            return cache
    return refresh_ecb_fx_cache(now=now, path=path, fetch_fn=fetch_fn)
def load_production_pair_rates(*, now: datetime | None = None) -> tuple[dict[str, float], FxFreshness, dict[str, Any]]:
    """Return shared pair rates only when FX freshness allows conversion."""
    now = now or datetime.now(timezone.utc)
    cache = load_fx_cache()
    fx = evaluate_ecb_fx_freshness(cache=cache, now=now)
    if not fx.allows_conversion or not cache:
        raise ValueError(fx.block_reason or FX_RATE_STALE_NO_SAFE_CONVERSION)
    pairs = cache.get("pairRates") or {}
    if not isinstance(pairs, dict) or not pairs:
        snapshot = snapshot_from_cache(cache)
        if snapshot is None:
            raise ValueError(FX_RATE_STALE_NO_SAFE_CONVERSION)
        pairs = snapshot.pair_rates_float()
    rates = {str(k).upper(): float(v) for k, v in pairs.items()}
    return rates, fx, cache
