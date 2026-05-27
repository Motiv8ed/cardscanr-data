"""
mock_provider.py

Deterministic mock provider for CardScanR market price evidence.

Returns fake but consistent sold-listing evidence based on a hash of the
search-request fields.  Useful for:
- Unit tests
- CI validation
- Worker smoke-testing without network

No live network calls.  No secrets required.  Safe for cloud/Codex.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from market_pricing_provider_contracts import (
    MarketPriceEvidenceListing,
    MarketPriceProviderCapabilities,
    MarketPriceProviderResult,
    MarketPriceSearchRequest,
)


_MOCK_MARKETPLACE_DOMAIN: dict[str, str] = {
    "AU": "www.ebay.com.au",
    "US": "www.ebay.com",
    "GB": "www.ebay.co.uk",
    "CA": "www.ebay.ca",
    "EU": "www.ebay.ie",
}

_MOCK_MARKETPLACE_ID: dict[str, str] = {
    "AU": "EBAY_AU",
    "US": "EBAY_US",
    "GB": "EBAY_GB",
    "CA": "EBAY_CA",
    "EU": "EBAY_EU",
}


class MockMarketPriceProvider:
    """Deterministic mock provider — always enabled, never calls network."""

    name = "mock"

    CAPABILITIES = MarketPriceProviderCapabilities(
        provider_name="mock",
        enabled=True,
        live_network_required=False,
        requires_credentials=False,
        supported_markets=("AU", "US", "GB", "CA", "EU"),
        supported_languages=("en", "jp"),
        supported_currencies=("AUD", "USD", "GBP", "CAD", "EUR"),
        returns_evidence_listings=True,
        returns_confidence_score=True,
        safe_for_cloud=True,
        next_implementation_step="Already functional. Extend fixture cards if needed.",
        notes="Returns deterministic fake evidence seeded from the search request fields.",
    )

    def __init__(self, *, now_utc: datetime | None = None) -> None:
        self._now_utc = now_utc or datetime.now(timezone.utc)

    def fetch(self, request: MarketPriceSearchRequest) -> MarketPriceProviderResult:
        seed_input = (
            f"{request.market}|{request.language}|{request.canonical_id}"
            f"|{request.variant}|{request.condition}|{request.graded}"
        )
        digest = hashlib.sha256(seed_input.encode("utf-8")).hexdigest()
        seed = int(digest[:8], 16)

        base = 40.0 + (seed % 120)
        spread = 3.0 + (seed % 7)

        prices = [
            round(base - spread, 2),
            round(base, 2),
            round(base + spread, 2),
            round(base + spread + 1.5, 2),
            round(base + spread * 2.0, 2),
        ]
        shippings = [0.0, 2.99, 0.0, 4.5, 0.0]

        domain = _MOCK_MARKETPLACE_DOMAIN.get(request.market.upper(), "www.ebay.com")
        marketplace_id = _MOCK_MARKETPLACE_ID.get(request.market.upper(), "EBAY_US")

        listings: list[MarketPriceEvidenceListing] = []
        for idx, sold_price in enumerate(prices, start=1):
            shipping = shippings[idx - 1]
            sold_dt = (self._now_utc.replace(microsecond=0) - timedelta(days=idx + 1))
            listing_id = f"mock-{digest[:10]}-{idx}"
            listings.append(
                MarketPriceEvidenceListing(
                    title=(
                        f"{request.card_name} {request.set_name} "
                        f"{request.collector_number} {request.variant} {request.condition}"
                    ),
                    sold_price=sold_price,
                    shipping_price=shipping,
                    total_price=round(sold_price + shipping, 2),
                    currency=request.currency,
                    sold_date=sold_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    listing_url=f"https://{domain}/itm/{listing_id}",
                    marketplace=marketplace_id,
                    condition=request.condition,
                    graded=request.graded,
                    source_provider=self.name,
                    raw_provider_id=listing_id,
                )
            )

        return MarketPriceProviderResult(
            provider_name=self.name,
            source="mock_market_provider",
            listings=listings,
            notes=f"Deterministic mock listings (seed={digest[:10]})",
            raw_metadata={"providerDomain": domain, "mockSeed": digest[:10]},
        )
