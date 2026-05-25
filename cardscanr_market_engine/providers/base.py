from __future__ import annotations

from typing import Protocol

from ..models import MarketPriceKey, ProviderResult


class MarketCompsProvider(Protocol):
    def fetch_comps(self, price_key: MarketPriceKey) -> ProviderResult:
        ...
