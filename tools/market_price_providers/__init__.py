"""
market_price_providers package

Provider adapter implementations for the CardScanR market pricing pipeline.

Available providers:
- mock_provider   — deterministic fake evidence, always safe
- manual_provider — reads manually collected sold-listing data
- disabled_ebay_provider — fails fast; live eBay is not enabled

Use provider_registry to resolve providers by name and enforce allow-list.
"""

from __future__ import annotations

from .mock_provider import MockMarketPriceProvider
from .manual_provider import ManualMarketPriceProvider
from .disabled_ebay_provider import DisabledEbayMarketPriceProvider
from .provider_registry import MarketPriceProviderRegistry, get_default_registry

__all__ = [
    "MockMarketPriceProvider",
    "ManualMarketPriceProvider",
    "DisabledEbayMarketPriceProvider",
    "MarketPriceProviderRegistry",
    "get_default_registry",
]
