#!/usr/bin/env python3
"""
market_pricing_provider_contracts.py

Typed contracts (dataclasses) for the CardScanR market price provider adapter layer.

These structures define the shared language between the market pricing worker
and any concrete provider implementation (mock, manual, planned eBay/Apify/browser).

No live network calls are made here.  No secrets are required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Search request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketPriceSearchRequest:
    """Describes a single pricing look-up that a provider must fulfil."""

    # Core identity
    market: str
    currency: str
    marketplace: str
    game: str
    language: str
    canonical_id: str
    card_name: str
    set_name: str
    set_id: str
    collector_number: str

    # Optional identity refinement
    variant: str = "raw"
    condition: str = "near_mint"
    graded: bool = False

    # Query
    query: str = ""
    exclusion_terms: tuple[str, ...] = ()
    max_results: int = 25

    # Optional date constraint  (ISO-8601 partial strings, e.g. "2024-01-01")
    date_range_from: Optional[str] = None
    date_range_to: Optional[str] = None


# ---------------------------------------------------------------------------
# Evidence listing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketPriceEvidenceListing:
    """A single normalised sold-listing entry returned by a provider."""

    title: str
    sold_price: float
    shipping_price: float
    total_price: float
    currency: str
    sold_date: str                  # ISO-8601 UTC, e.g. "2024-03-15T10:00:00Z"
    listing_url: str
    marketplace: str

    condition: str = "unknown"
    graded: bool = False
    source_provider: str = "unknown"

    # Optional / enrichment
    seller_location: Optional[str] = None
    raw_provider_id: Optional[str] = None
    raw_data: Optional[dict[str, Any]] = None   # must be scrubbed of secrets before storage


# ---------------------------------------------------------------------------
# Provider result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketPriceProviderResult:
    """Successful response from a provider fetch."""

    provider_name: str
    source: str
    listings: list[MarketPriceEvidenceListing] = field(default_factory=list)
    notes: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Provider error
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketPriceProviderError:
    """Structured error payload returned when a provider cannot fulfil a request."""

    provider_name: str
    error_code: str
    message: str
    live_network_attempted: bool = False
    safe_for_cloud: bool = True


# ---------------------------------------------------------------------------
# Provider capabilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketPriceProviderCapabilities:
    """Declares what a provider can and cannot do."""

    provider_name: str
    enabled: bool
    live_network_required: bool
    secrets_required: bool
    supported_markets: tuple[str, ...]
    supported_languages: tuple[str, ...]
    supported_currencies: tuple[str, ...]
    returns_evidence_listings: bool
    returns_confidence_score: bool
    safe_for_cloud: bool
    next_implementation_step: str
    notes: str = ""
