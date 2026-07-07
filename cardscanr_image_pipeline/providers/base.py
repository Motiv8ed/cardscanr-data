from __future__ import annotations

from typing import Protocol

from ..models import CardImageIdentity, ProviderImageCandidate


class ImageProvider(Protocol):
    provider_id: str

    def resolve(self, identity: CardImageIdentity) -> ProviderImageCandidate | None:
        """Resolve a provider image candidate using identity fields only."""


class ImageProviderError(RuntimeError):
    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"{provider}: {message}")
