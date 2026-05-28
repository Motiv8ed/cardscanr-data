from __future__ import annotations

import os

from .base import MarketCompsProvider
from .ebay_browser_provider import EbayBrowserSoldCompsProvider
from .errors import ProviderDisabledError, ProviderPermanentError
from .mock_provider import MockMarketCompsProvider


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def create_market_comps_provider(provider_name: str | None = None) -> MarketCompsProvider:
    selected = (provider_name or os.getenv("MARKET_LOOKUP_PROVIDER", "mock")).strip().lower() or "mock"
    if selected == "mock":
        return MockMarketCompsProvider()
    if selected == "ebay_browser":
        if not _env_bool("ENABLE_EBAY_REAL_LOOKUP", False):
            raise ProviderDisabledError(
                "MARKET_LOOKUP_PROVIDER=ebay_browser requires ENABLE_EBAY_REAL_LOOKUP=true"
            )
        return EbayBrowserSoldCompsProvider()
    raise ProviderPermanentError(f"Unknown MARKET_LOOKUP_PROVIDER '{selected}'. Supported providers: mock, ebay_browser.")
