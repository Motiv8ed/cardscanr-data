"""Price source semantics for hybrid bulk + verification pricing."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

PriceEvidenceKind = Literal["reference", "verified_au", "international_estimate"]
DisplayPriceSource = Literal[
    "reference",
    "verified_au",
    "verified_local",
    "local_verified",
    "international_estimate",
    "pending_verification",
    "unavailable",
]
PriceClass = Literal[
    "local_verified",
    "reference",
    "international_estimate",
    "unavailable",
    "pending_verification",
]
MappingStatus = Literal["exact", "canonical", "alias", "unresolved", "ambiguous"]

REFERENCE_PROVIDERS = frozenset(
    {
        "static_reference",
        "tcgdex_reference",
        "pokemon_tcg_api_reference",
        "tcgdex_tcgplayer",
        "tcgdex_cardmarket",
        "pokemon_tcg_api",
    }
)
VERIFIED_PROVIDERS = frozenset({"ebay_browser", "ebay"})


@dataclass(frozen=True)
class ReferencePriceObservation:
    provider: str
    source_market: str
    source_currency: str
    market_price: float
    low_price: float | None
    high_price: float | None
    confidence: str
    mapping_status: MappingStatus
    source_record_id: str | None = None
    fetched_at_utc: str | None = None
    raw_source: str | None = None
    diagnostics: dict[str, Any] | None = None

    @property
    def is_usable(self) -> bool:
        return self.market_price > 0 and self.mapping_status in {"exact", "canonical", "alias"}


def is_reference_provider(provider: str | None) -> bool:
    return (provider or "").strip().lower() in REFERENCE_PROVIDERS


def is_verified_provider(provider: str | None) -> bool:
    return (provider or "").strip().lower() in VERIFIED_PROVIDERS
