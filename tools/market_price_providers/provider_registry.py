"""
provider_registry.py

Registry for CardScanR market price providers.

Default allow-list: mock, manual only.
Any attempt to use an eBay/Apify/browser provider without explicit opt-in
will fail fast before any network call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from market_pricing_provider_contracts import (
    MarketPriceProviderCapabilities,
    MarketPriceProviderError,
    MarketPriceSearchRequest,
)
from market_price_providers.mock_provider import MockMarketPriceProvider
from market_price_providers.manual_provider import ManualMarketPriceProvider
from market_price_providers.disabled_ebay_provider import (
    DisabledEbayMarketPriceProvider,
    DisabledProviderError,
)


# ---------------------------------------------------------------------------
# Provider name constants
# ---------------------------------------------------------------------------

PROVIDER_MOCK = "mock"
PROVIDER_MANUAL = "manual"

# All eBay / live-network provider names that must be blocked by default
LIVE_PROVIDER_NAMES: frozenset[str] = frozenset(
    {
        "ebay",
        "ebay_disabled",
        "ebay_sold_listings_manual",   # NB: this is the *source* id, not a live provider
        "ebay_sold_listings_apify_planned",
        "ebay_sold_listings_api_planned",
        "ebay_sold_listings_local_browser_planned",
        "apify",
        "browser",
        "local_browser",
    }
)

# Provider names that are safe to use without explicit enablement
DEFAULT_ALLOWED: frozenset[str] = frozenset({PROVIDER_MOCK, PROVIDER_MANUAL})


class ProviderNotAllowedError(RuntimeError):
    """Raised when a caller requests a provider that is not in the allow-list."""


class MarketPriceProviderRegistry:
    """
    Resolves provider instances by name and enforces the allow-list.

    By default only ``mock`` and ``manual`` are allowed.
    Live/eBay providers are always blocked unless ``_force_allow_live=True``
    is passed (for future use; currently no live providers exist).
    """

    def __init__(
        self,
        *,
        manual_json_path: Path | None = None,
        _force_allow_live: bool = False,
    ) -> None:
        self._manual_json_path = manual_json_path
        self._force_allow_live = _force_allow_live

        self._providers: dict[str, Any] = {
            PROVIDER_MOCK: MockMarketPriceProvider(),
            PROVIDER_MANUAL: ManualMarketPriceProvider(manual_json_path),
            "ebay_disabled": DisabledEbayMarketPriceProvider(),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, provider_name: str) -> Any:
        """
        Return a provider instance for *provider_name*.

        Raises:
            ProviderNotAllowedError – if the provider name matches a live/eBay
                pattern and live access has not been explicitly enabled.
            KeyError – if the provider name is not registered.
        """
        name_lower = provider_name.strip().lower()

        # Block live providers unless explicitly force-allowed
        if not self._force_allow_live and self._is_live_provider(name_lower):
            raise ProviderNotAllowedError(
                f"Provider '{provider_name}' is a live/eBay provider and is not allowed "
                "by default. Live eBay access is disabled. "
                "Reason: Live eBay access is disabled until provider/legal/terms "
                "approach is approved."
            )

        if name_lower not in self._providers:
            available = ", ".join(sorted(self._providers.keys()))
            raise KeyError(
                f"Unknown provider '{provider_name}'. Available: {available}"
            )

        return self._providers[name_lower]

    def is_allowed(self, provider_name: str) -> bool:
        """Return True if the provider is allowed under current registry settings."""
        name_lower = provider_name.strip().lower()
        if self._is_live_provider(name_lower) and not self._force_allow_live:
            return False
        return name_lower in self._providers

    def registered_names(self) -> list[str]:
        """Return names of all registered providers (including disabled)."""
        return sorted(self._providers.keys())

    def capabilities(self) -> list[MarketPriceProviderCapabilities]:
        """Return capability descriptors for all registered providers."""
        caps = []
        for provider in self._providers.values():
            if hasattr(provider, "CAPABILITIES"):
                caps.append(provider.CAPABILITIES)
        return caps

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _is_live_provider(name: str) -> bool:
        if name in LIVE_PROVIDER_NAMES:
            return True
        # Heuristic: anything with 'ebay', 'apify', or 'browser' in the name
        return any(kw in name for kw in ("ebay", "apify", "browser", "live"))


# ---------------------------------------------------------------------------
# Default singleton
# ---------------------------------------------------------------------------

_default_registry: MarketPriceProviderRegistry | None = None


def get_default_registry(
    *,
    manual_json_path: Path | None = None,
) -> MarketPriceProviderRegistry:
    """Return (or create) the module-level default registry."""
    global _default_registry
    if _default_registry is None:
        _default_registry = MarketPriceProviderRegistry(manual_json_path=manual_json_path)
    return _default_registry
