"""
disabled_ebay_provider.py

Disabled eBay provider stub for CardScanR market price evidence.

This provider ALWAYS fails fast with a structured error.
Live eBay scraping/API access is not enabled until a proper
provider/legal/terms approach has been approved.

Rules:
- Must never open a network connection.
- Must never import requests, playwright, selenium, or similar.
- Must never call eBay, Apify, or any external service.
- Must always raise DisabledProviderError before doing any real work.
"""

from __future__ import annotations

from market_pricing_provider_contracts import (
    MarketPriceProviderCapabilities,
    MarketPriceProviderError,
    MarketPriceSearchRequest,
)

DISABLED_REASON = (
    "Live eBay access is disabled until provider/legal/terms approach is approved."
)


class DisabledProviderError(RuntimeError):
    """Raised when a disabled provider is called."""

    def __init__(self, provider_name: str, reason: str = DISABLED_REASON) -> None:
        super().__init__(f"Provider '{provider_name}' is disabled: {reason}")
        self.provider_error = MarketPriceProviderError(
            provider_name=provider_name,
            error_code="provider_disabled",
            message=reason,
            live_network_attempted=False,
            safe_for_cloud=True,
        )


class DisabledEbayMarketPriceProvider:
    """
    Placeholder for future eBay sold-listings providers.

    Covers all planned eBay access methods:
    - ebay_sold_listings_apify_planned
    - ebay_sold_listings_api_planned
    - ebay_sold_listings_local_browser_planned

    Calling ``fetch()`` always raises DisabledProviderError immediately,
    before any network activity.
    """

    name = "ebay_disabled"

    CAPABILITIES = MarketPriceProviderCapabilities(
        provider_name="ebay_disabled",
        enabled=False,
        live_network_required=True,
        requires_credentials=True,
        supported_markets=("AU", "US", "GB", "CA", "EU"),
        supported_languages=("en", "jp"),
        supported_currencies=("AUD", "USD", "GBP", "CAD", "EUR"),
        returns_evidence_listings=False,
        returns_confidence_score=False,
        safe_for_cloud=False,
        next_implementation_step=(
            "Choose one: (a) eBay Browse API with OAuth, "
            "(b) Apify eBay scraper actor, "
            "(c) local browser worker. "
            "Obtain legal/terms sign-off, then implement as a new provider module."
        ),
        notes=(
            "liveEbayScrapingEnabled: false. "
            + DISABLED_REASON
        ),
    )

    def fetch(self, request: MarketPriceSearchRequest) -> None:  # type: ignore[return]
        # Intentionally raise before any network activity.
        raise DisabledProviderError(self.name)
