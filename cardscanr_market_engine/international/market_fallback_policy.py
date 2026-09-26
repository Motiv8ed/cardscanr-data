"""Central market fallback policy for international pricing estimates."""
from __future__ import annotations

import os
from typing import Any

from ..marketplaces import browser_supported_market_routes, normalize_market_country
from ..models import MarketPriceKey

# Home market -> ordered fallback markets (browser-supported only).
# NZ/JP/EU browser sold routes are not yet live — keep them out of this graph.
MARKET_FALLBACK_POLICY: dict[str, tuple[str, ...]] = {
    "AU": ("US", "GB", "CA"),
    "US": ("CA", "GB", "AU"),
    "GB": ("US", "CA", "AU"),
    "CA": ("US", "GB", "AU"),
}

# Language families that may borrow evidence from these source markets.
# Japanese printings may be searched on EN-domain eBay sites for the *same*
# Japanese identity — never as an English printing substitute.
# JP eBay (ebay.co.jp) is not yet a browser-supported home/fallback route.
LANGUAGE_COMPATIBLE_MARKETS: dict[str, frozenset[str]] = {
    "en": frozenset({"AU", "US", "GB", "CA"}),
    "ja": frozenset({"AU", "US", "GB", "CA"}),
    "jp": frozenset({"AU", "US", "GB", "CA"}),
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
    "NZ": "New Zealand",
    "EU": "Europe",
}

# Attempt classification for diagnostics (not shown raw to customers).
NO_RESULTS = "NO_RESULTS"
AMBIGUOUS_RESULTS = "AMBIGUOUS_RESULTS"
LOW_CONFIDENCE = "LOW_CONFIDENCE"
SUFFICIENT_FOREIGN_COMPS = "SUFFICIENT_FOREIGN_COMPS"
SUFFICIENT_LOCAL_COMPS = "SUFFICIENT_LOCAL_COMPS"
NOT_REQUIRED = "NOT_REQUIRED"
ERROR = "ERROR"

_BROWSER_MARKETS = frozenset(country for country, _currency in browser_supported_market_routes())


def _language_family(value: object) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if text.startswith("zh"):
        return "zh"
    if text in {"ja", "jp", "japanese"} or text.startswith("ja"):
        return "ja"
    if text.startswith("ko"):
        return "ko"
    return "en" if text in {"", "en", "english"} else text


def _parse_env_policy() -> dict[str, tuple[str, ...]] | None:
    """Optional override: INTERNATIONAL_FALLBACK_GRAPH=AU:US,GB,CA;US:CA,GB,AU"""
    raw = str(os.getenv("INTERNATIONAL_FALLBACK_GRAPH") or "").strip()
    if not raw:
        return None
    policy: dict[str, tuple[str, ...]] = {}
    for chunk in raw.split(";"):
        part = chunk.strip()
        if not part or ":" not in part:
            continue
        home_raw, markets_raw = part.split(":", 1)
        home = normalize_market_country(home_raw)
        markets = tuple(
            normalize_market_country(item)
            for item in markets_raw.split(",")
            if str(item or "").strip()
        )
        if home:
            policy[home] = markets
    return policy or None


def market_fallback_policy() -> dict[str, tuple[str, ...]]:
    override = _parse_env_policy()
    if override:
        merged = dict(MARKET_FALLBACK_POLICY)
        merged.update(override)
        return merged
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
    configured = policy or market_fallback_policy()
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


def classify_foreign_market_attempt(
    *,
    included_count: int,
    recommended_price: float | None,
    confidence: str | None,
    evidence_outcome: str | None = None,
    error: str | None = None,
) -> str:
    """Classify one foreign-market attempt for diagnostics / market trace."""
    if error:
        return ERROR
    included = max(0, int(included_count or 0))
    if included <= 0 or recommended_price is None:
        return NO_RESULTS
    outcome = str(evidence_outcome or "").strip().lower()
    if outcome in {"numeric_estimate", "range_estimate"}:
        return SUFFICIENT_FOREIGN_COMPS
    conf = str(confidence or "").strip().lower()
    if included == 1 or conf == "low":
        return LOW_CONFIDENCE
    return AMBIGUOUS_RESULTS


def parse_international_job_reason(reason: str) -> dict[str, Any] | None:
    text = str(reason or "").strip()
    if not text.startswith("international_fallback"):
        return None
    parts = text.split(":")
    payload: dict[str, Any] = {"kind": "international_fallback"}
    if len(parts) >= 2 and parts[1]:
        payload["targetMarket"] = normalize_market_country(parts[1])
    return payload
