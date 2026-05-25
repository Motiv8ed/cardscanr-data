from __future__ import annotations

from typing import Protocol

from ..models import ProviderRequest, ProviderResult


class MarketCompsProvider(Protocol):
    def fetch_comps(self, request: ProviderRequest) -> ProviderResult:
        ...
