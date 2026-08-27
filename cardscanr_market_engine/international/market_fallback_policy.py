"""Central market fallback policy for international pricing estimates."""
from __future__ import annotations

from typing import Any

from ..marketplaces import browser_supported_market_routes, normalize_market_country
from ..models import MarketPriceKey

# Home market -> ordered fallback markets (browser-supported only).
MARKET_FALLBACK_POLICY: dict[str, tuple[str, ...]] = {
    "AU": ("US", "GB", "CA"),
    "US": ("CA", "GB", "AU"),
    "GB": ("US", "CA", "AU"),
    "CA": ("US", "GB", "AU"),
}

# Language families that may borrow evidence from these source markets.
LANGUAGE_COMPATIBLE_MARKETS: dict[str, frozenset[str]] = {
    "en": frozenset({"AU", "US", "GB", "CA"}),
    "ja": frozenset(),
    "ko": frozenset(),
    "zh": frozenset(),
    "zh-hans": frozenset(),
    "zh-hant": frozenset(),
}

MARKET_DISPLAY_NAMES: dict[str, str] = {
    "AU": "Australia",
    "US": "United States",
    "GB": "United Kingdom",
    "CA": "Canada",
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "JP": "Japan",
    "EU": "Europe",
}

_BROWSER_MARKETS = frozenset(country for country, _currency in browser_supported_market_routes())


def _language_family(value: object) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if text.startswith("zh"):
        return "zh"
    if text.startswith("ja"):
        return "ja"
    if text.startswith("ko"):
        return "ko"
    return "en" if text in {"", "en", "english"} else text


def market_fallback_policy() -> dict[str, tuple[str, ...]]:
    return dict(MARKET_FALLBACK_POLICY)


def is_browser_fallback_market(market_country: object) -> bool:
    return normalize_market_country(market_country) in _BROWSER_MARKETS


def fallback_markets_for_key(
    price_key: MarketPriceKey,
    *,
    policy: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Return ordered fallback markets for a price key, respecting language safety."""
    home = normalize_market_country(price_key.market_country)
    configured = policy or MARKET_FALLBACK_POLICY
    candidates = configured.get(home, ())
    language = _language_family(price_key.language)
    allowed = LANGUAGE_COMPATIBLE_MARKETS.get(language, frozenset())
    if not allowed:
        return ()
    ordered: list[str] = []
    for market in candidates:
        normalized = normalize_market_country(market)
        if normalized == home:
            continue
        if normalized not in _BROWSER_MARKETS:
            continue
        if normalized not in allowed:
            continue
        if normalized not in ordered:
            ordered.append(normalized)
    return tuple(ordered)


def market_display_name(market_country: object) -> str:
    code = normalize_market_country(market_country)
    return MARKET_DISPLAY_NAMES.get(code, code)


def parse_international_job_reason(reason: str) -> dict[str, Any] | None:
    text = str(reason or "").strip()
    if not text.startswith("international_fallback"):
        return None
    parts = text.split(":")
    payload: dict[str, Any] = {"kind": "international_fallback"}
    if len(parts) >= 2 and parts[1]:
        payload["targetMarket"] = normalize_market_country(parts[1])
    return payload
