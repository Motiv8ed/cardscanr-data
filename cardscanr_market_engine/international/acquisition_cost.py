"""Acquisition-cost helpers: keep sold market value separate from shipping.

Historical sold comps do not expose reliable destination-specific shipping for
the buyer's home country. Current active listings may optionally supply a
shipping estimate only — never sold-market evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any, Sequence


@dataclass(frozen=True)
class ShippingEstimate:
    status: str  # available | limited | unavailable
    typical_shipping: float | None
    shipping_low: float | None
    shipping_high: float | None
    sample_count: int
    source_market: str | None
    destination_market: str | None
    currency: str | None
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "typicalShipping": self.typical_shipping,
            "shippingLow": self.shipping_low,
            "shippingHigh": self.shipping_high,
            "sampleCount": self.sample_count,
            "sourceMarket": self.source_market,
            "destinationMarket": self.destination_market,
            "currency": self.currency,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class AcquisitionCostEstimate:
    market_value: float | None
    market_value_currency: str | None
    shipping: ShippingEstimate
    acquisition_low: float | None
    acquisition_typical: float | None
    acquisition_high: float | None
    taxes_included: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "marketValue": self.market_value,
            "marketValueCurrency": self.market_value_currency,
            "shipping": self.shipping.to_dict(),
            "acquisitionLow": self.acquisition_low,
            "acquisitionTypical": self.acquisition_typical,
            "acquisitionHigh": self.acquisition_high,
            "taxesIncluded": self.taxes_included,
            "wording": "Estimated acquisition cost",
        }


def unavailable_shipping(
    *,
    source_market: str | None = None,
    destination_market: str | None = None,
    reason: str = "destination_specific_shipping_unavailable_for_sold_comps",
) -> ShippingEstimate:
    return ShippingEstimate(
        status="unavailable",
        typical_shipping=None,
        shipping_low=None,
        shipping_high=None,
        sample_count=0,
        source_market=source_market,
        destination_market=destination_market,
        currency=None,
        notes=reason,
    )


def estimate_shipping_from_observations(
    amounts: Sequence[float],
    *,
    source_market: str,
    destination_market: str,
    currency: str,
    min_samples_for_typical: int = 3,
) -> ShippingEstimate:
    """Build a shipping estimate from destination-specific current-listing fees.

    Never invent amounts. Empty/invalid observations → unavailable.
    """
    cleaned = sorted(float(v) for v in amounts if v is not None and float(v) >= 0)
    if not cleaned:
        return unavailable_shipping(
            source_market=source_market,
            destination_market=destination_market,
            reason="no_destination_shipping_observations",
        )
    low = cleaned[0]
    high = cleaned[-1]
    mid = float(median(cleaned))
    if len(cleaned) < min_samples_for_typical:
        return ShippingEstimate(
            status="limited",
            typical_shipping=mid,
            shipping_low=low,
            shipping_high=high,
            sample_count=len(cleaned),
            source_market=source_market,
            destination_market=destination_market,
            currency=currency.upper(),
            notes="limited_shipping_data",
        )
    return ShippingEstimate(
        status="available",
        typical_shipping=mid,
        shipping_low=low,
        shipping_high=high,
        sample_count=len(cleaned),
        source_market=source_market,
        destination_market=destination_market,
        currency=currency.upper(),
        notes="destination_shipping_from_current_listings",
    )


def combine_acquisition_cost(
    *,
    market_value: float | None,
    market_value_currency: str | None,
    shipping: ShippingEstimate,
) -> AcquisitionCostEstimate:
    """market value + shipping range. Taxes/duties are never auto-added."""
    if market_value is None or market_value <= 0:
        return AcquisitionCostEstimate(
            market_value=None,
            market_value_currency=market_value_currency,
            shipping=shipping,
            acquisition_low=None,
            acquisition_typical=None,
            acquisition_high=None,
            taxes_included=False,
        )
    if shipping.status == "unavailable" or shipping.typical_shipping is None:
        return AcquisitionCostEstimate(
            market_value=round(float(market_value), 2),
            market_value_currency=market_value_currency,
            shipping=shipping,
            acquisition_low=None,
            acquisition_typical=None,
            acquisition_high=None,
            taxes_included=False,
        )
    mv = float(market_value)
    low_ship = float(shipping.shipping_low if shipping.shipping_low is not None else shipping.typical_shipping)
    high_ship = float(shipping.shipping_high if shipping.shipping_high is not None else shipping.typical_shipping)
    return AcquisitionCostEstimate(
        market_value=round(mv, 2),
        market_value_currency=market_value_currency,
        shipping=shipping,
        acquisition_low=round(mv + low_ship, 2),
        acquisition_typical=round(mv + float(shipping.typical_shipping), 2),
        acquisition_high=round(mv + high_ship, 2),
        taxes_included=False,
    )
