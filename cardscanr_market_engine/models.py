from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from .marketplaces import LocalMarketConfig

Confidence = Literal["high", "medium", "low", "unknown"]


@dataclass(frozen=True)
class MarketPriceKey:
    id: str
    game: str
    card_name: str
    normalized_card_name: str
    set_name: str
    set_code: str | None
    collector_number: str
    language: str
    variant: str
    condition: str
    market_country: str
    currency: str
    fingerprint: str
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MarketPriceKey":
        return cls(
            id=str(row["id"]),
            game=str(row["game"]),
            card_name=str(row["card_name"]),
            normalized_card_name=str(row["normalized_card_name"]),
            set_name=str(row["set_name"]),
            set_code=row.get("set_code"),
            collector_number=str(row["collector_number"]),
            language=str(row["language"]),
            variant=str(row["variant"]),
            condition=str(row["condition"]),
            market_country=str(row["market_country"]),
            currency=str(row["currency"]),
            fingerprint=str(row["fingerprint"]),
            raw=dict(row),
        )


@dataclass(frozen=True)
class MarketPriceRefreshJob:
    id: str
    price_key_id: str
    reason: str
    priority: int
    status: str
    attempt_count: int
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MarketPriceRefreshJob":
        return cls(
            id=str(row["id"]),
            price_key_id=str(row["price_key_id"]),
            reason=str(row.get("reason", "")),
            priority=int(row.get("priority", 0)),
            status=str(row.get("status", "")),
            attempt_count=int(row.get("attempt_count", 0)),
            raw=dict(row),
        )


@dataclass(frozen=True)
class SoldComp:
    source_listing_id: str
    title: str
    sold_price: float
    shipping_price: float
    total_price: float
    currency: str
    sold_date: datetime
    listing_url: str
    condition_text: str
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResult:
    provider_name: str
    marketplace: str
    provider_fingerprint: str
    query_used: str
    comps: list[SoldComp]
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderRequest:
    price_key: MarketPriceKey
    market_country: str
    currency: str
    marketplace: str
    provider_marketplace_id: str
    provider_domain: str
    search_locale: str
    display_name: str
    market_config: LocalMarketConfig


@dataclass(frozen=True)
class EvaluatedComp:
    comp: SoldComp
    included_in_estimate: bool
    rejection_reason: str | None
    match_score: float


@dataclass(frozen=True)
class PricingStats:
    median_price: float | None
    average_price: float | None
    low_price: float | None
    high_price: float | None
    recommended_price: float | None
    sample_size: int
    included_count: int
    rejected_count: int
    confidence: Confidence
    stale_after: datetime
    item_median_price: float | None = None
    item_average_price: float | None = None
    item_low_price: float | None = None
    item_high_price: float | None = None
    item_recommended_price: float | None = None
    landed_median_price: float | None = None
    landed_average_price: float | None = None
    landed_low_price: float | None = None
    landed_high_price: float | None = None
    landed_recommended_price: float | None = None
    price_basis: str = "item_price"
