#!/usr/bin/env python3
"""
market_price_evidence_normalizer.py

Normalises raw sold-listing evidence into MarketPriceEvidenceListing instances.

Rules:
- Normalises prices (float, rounded to 2 dp)
- Normalises shipping (missing → 0.0)
- Computes total price
- Normalises currency to upper-case ISO code
- Normalises sold date to ISO-8601 UTC string
- Normalises marketplace name to canonical upper-case slug
- Normalises condition string
- Detects graded/ungraded from title keywords
- Rejects listings matching exclusion terms
- Returns (listing | None, reject_reason | None)

No live network calls.  No secrets required.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

from market_pricing_provider_contracts import MarketPriceEvidenceListing


# ---------------------------------------------------------------------------
# Exclusion / rejection terms
# ---------------------------------------------------------------------------

EXCLUSION_TERM_PATTERNS: dict[str, re.Pattern[str]] = {
    "proxy":   re.compile(r"\bprox(y|ies)\b|\bcustom\b|\bfan.?art\b|\balter(ed)?\b", re.IGNORECASE),
    "fake":    re.compile(r"\bfake\b|\breplica\b|\bcounterfeit\b", re.IGNORECASE),
    "digital": re.compile(r"\bdigital\b|\bptcg[ol]\b|\bonline.?code\b|\bcode.?card\b", re.IGNORECASE),
    "lot":     re.compile(r"\blot\b|\bbundle\b|\bx\s*[2-9]\b|\bplayset\b|\bcollection\b", re.IGNORECASE),
    "damaged": re.compile(r"\bdamaged\b|\bheavily.?played\b|\bpoor.?condition\b", re.IGNORECASE),
}

# ---------------------------------------------------------------------------
# Graded detection
# ---------------------------------------------------------------------------

GRADED_PATTERN = re.compile(
    r"\b(psa|bgs|cgc|ace|sgc|beckett|graded|slab)\b", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Condition normalisation
# ---------------------------------------------------------------------------

_CONDITION_MAP: dict[re.Pattern[str], str] = {
    re.compile(r"\b(near.?mint|nm)\b", re.IGNORECASE): "near_mint",
    re.compile(r"\b(lightly.?played|lp)\b", re.IGNORECASE): "lightly_played",
    re.compile(r"\b(moderately.?played|mp)\b", re.IGNORECASE): "moderately_played",
    re.compile(r"\b(heavily.?played|hp)\b", re.IGNORECASE): "heavily_played",
    re.compile(r"\bdamaged\b", re.IGNORECASE): "damaged",
    re.compile(r"\bmint\b", re.IGNORECASE): "mint",
    re.compile(r"\bgood\b", re.IGNORECASE): "good",
    re.compile(r"\bexcellent\b", re.IGNORECASE): "excellent",
    re.compile(r"\bvery.?good\b", re.IGNORECASE): "very_good",
}

# ---------------------------------------------------------------------------
# Marketplace normalisation
# ---------------------------------------------------------------------------

_MARKETPLACE_MAP: dict[re.Pattern[str], str] = {
    re.compile(r"ebay\.com\.au", re.IGNORECASE): "EBAY_AU",
    re.compile(r"ebay\.co\.uk", re.IGNORECASE): "EBAY_GB",
    re.compile(r"ebay\.ca",     re.IGNORECASE): "EBAY_CA",
    re.compile(r"ebay\.ie",     re.IGNORECASE): "EBAY_EU",
    re.compile(r"ebay\.com",    re.IGNORECASE): "EBAY_US",
    re.compile(r"ebay",         re.IGNORECASE): "EBAY",
    re.compile(r"tcgplayer",    re.IGNORECASE): "TCGPLAYER",
    re.compile(r"cardmarket",   re.IGNORECASE): "CARDMARKET",
}

# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
)


def _parse_date(value: str) -> Optional[str]:
    """Try to parse a date string, return ISO-8601 UTC string or None."""
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Price normalisation
# ---------------------------------------------------------------------------

_PRICE_STRIP = re.compile(r"[^\d.]")


def _parse_price(value: Any) -> Optional[float]:
    """Parse a price value from various input types, returning float or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return round(float(value), 2)
        except (ValueError, TypeError):
            return None
    cleaned = _PRICE_STRIP.sub("", str(value))
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Currency normalisation
# ---------------------------------------------------------------------------

_CURRENCY_ALIASES: dict[str, str] = {
    "$": "USD",
    "au$": "AUD",
    "a$": "AUD",
    "us$": "USD",
    "ca$": "CAD",
    "c$": "CAD",
    "£": "GBP",
    "€": "EUR",
    "aud": "AUD",
    "usd": "USD",
    "gbp": "GBP",
    "cad": "CAD",
    "eur": "EUR",
    "nzd": "NZD",
    "jpy": "JPY",
}


def _normalize_currency(value: Any) -> str:
    if not value:
        return "USD"
    raw = str(value).strip().lower()
    return _CURRENCY_ALIASES.get(raw, raw.upper())


# ---------------------------------------------------------------------------
# Core normaliser
# ---------------------------------------------------------------------------


def normalize_evidence(
    raw: dict[str, Any],
    *,
    source_provider: str = "unknown",
    marketplace_hint: str = "",
    condition_hint: str = "unknown",
    allow_exclusion_terms: frozenset[str] = frozenset(),
) -> tuple[Optional[MarketPriceEvidenceListing], Optional[str]]:
    """
    Normalise a raw sold-listing dict into a MarketPriceEvidenceListing.

    Returns (listing, None) on success or (None, reject_reason) on rejection.

    Parameters
    ----------
    raw:
        Raw dict from a provider (CSV row, JSON object, etc.)
    source_provider:
        Name tag to embed in the listing.
    marketplace_hint:
        Fallback marketplace string when not present in ``raw``.
    condition_hint:
        Fallback condition when not detectable from title/raw.
    allow_exclusion_terms:
        Set of exclusion-term category names to *not* reject on
        (e.g. frozenset({"damaged"}) to allow damaged listings).
    """
    title_raw = str(raw.get("title") or "")
    title_norm = unicodedata.normalize("NFKC", title_raw).strip()

    # Exclusion term checks
    for term_key, pattern in EXCLUSION_TERM_PATTERNS.items():
        if term_key in allow_exclusion_terms:
            continue
        if pattern.search(title_norm):
            return None, f"excluded:{term_key}"

    # Price
    sold_price = _parse_price(raw.get("soldPrice") or raw.get("sold_price") or raw.get("price"))
    if sold_price is None or sold_price <= 0:
        return None, "invalid_sold_price"

    shipping_raw = raw.get("shippingPrice") or raw.get("shipping_price") or raw.get("shipping") or 0
    shipping_price = _parse_price(shipping_raw) or 0.0
    total_price = round(sold_price + shipping_price, 2)

    # Currency
    currency = _normalize_currency(raw.get("currency") or raw.get("currencyCode"))

    # Sold date
    date_raw = str(raw.get("soldDate") or raw.get("sold_date") or raw.get("soldAtUtc") or "")
    sold_date: str
    if date_raw:
        parsed = _parse_date(date_raw)
        sold_date = parsed if parsed else date_raw
    else:
        sold_date = ""

    # Listing URL
    listing_url = str(raw.get("listingUrl") or raw.get("listing_url") or raw.get("url") or "")

    # Marketplace
    marketplace_raw = str(raw.get("marketplace") or raw.get("market") or marketplace_hint or "")
    marketplace = _normalize_marketplace(marketplace_raw) if marketplace_raw else "UNKNOWN"

    # Condition
    condition = _normalize_condition(
        str(raw.get("condition") or ""),
        title=title_norm,
        fallback=condition_hint,
    )

    # Graded detection
    graded = _detect_graded(title_norm, raw)

    # Raw provider id
    raw_provider_id = str(raw.get("listingId") or raw.get("id") or raw.get("rawProviderId") or "")

    # Raw data: strip anything that looks like a secret
    safe_raw = _scrub_raw(raw)

    return (
        MarketPriceEvidenceListing(
            title=title_norm,
            sold_price=sold_price,
            shipping_price=shipping_price,
            total_price=total_price,
            currency=currency,
            sold_date=sold_date,
            listing_url=listing_url,
            marketplace=marketplace,
            condition=condition,
            graded=graded,
            source_provider=source_provider,
            seller_location=str(raw.get("sellerLocation") or "") or None,
            raw_provider_id=raw_provider_id or None,
            raw_data=safe_raw,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_marketplace(value: str) -> str:
    for pattern, canonical in _MARKETPLACE_MAP.items():
        if pattern.search(value):
            return canonical
    return value.upper().replace(" ", "_") or "UNKNOWN"


def _normalize_condition(raw_condition: str, *, title: str = "", fallback: str = "unknown") -> str:
    combined = f"{raw_condition} {title}"
    for pattern, canonical in _CONDITION_MAP.items():
        if pattern.search(combined):
            return canonical
    return fallback or "unknown"


def _detect_graded(title: str, raw: dict[str, Any]) -> bool:
    if isinstance(raw.get("graded"), bool):
        return raw["graded"]
    if str(raw.get("graded") or "").lower() in {"true", "yes", "1"}:
        return True
    return bool(GRADED_PATTERN.search(title))


_SECRET_FIELD_PATTERN = re.compile(
    r"(api[_\-]?key|token|secret|password|credential|bearer|auth|access[_\-]?key)",
    re.IGNORECASE,
)


def _scrub_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of raw with secret-looking fields redacted."""
    scrubbed: dict[str, Any] = {}
    for k, v in raw.items():
        if _SECRET_FIELD_PATTERN.search(str(k)):
            scrubbed[k] = "[REDACTED]"
        elif isinstance(v, str) and _SECRET_FIELD_PATTERN.search(v):
            scrubbed[k] = "[REDACTED]"
        else:
            scrubbed[k] = v
    return scrubbed


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------


def normalize_evidence_batch(
    rows: list[dict[str, Any]],
    *,
    source_provider: str = "unknown",
    marketplace_hint: str = "",
    condition_hint: str = "unknown",
    allow_exclusion_terms: frozenset[str] = frozenset(),
) -> tuple[list[MarketPriceEvidenceListing], list[dict[str, Any]]]:
    """
    Normalise a batch of raw rows.

    Returns (accepted_listings, rejected_rows).
    Each rejected row dict contains the original raw data plus a ``rejectReason`` key.
    """
    accepted: list[MarketPriceEvidenceListing] = []
    rejected: list[dict[str, Any]] = []

    for row in rows:
        listing, reason = normalize_evidence(
            row,
            source_provider=source_provider,
            marketplace_hint=marketplace_hint,
            condition_hint=condition_hint,
            allow_exclusion_terms=allow_exclusion_terms,
        )
        if listing is not None:
            accepted.append(listing)
        else:
            rejected.append({**row, "rejectReason": reason})

    return accepted, rejected
