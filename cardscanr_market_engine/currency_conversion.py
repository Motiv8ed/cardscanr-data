from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CurrencyConversion:
    source_currency: str
    target_currency: str
    rate: float
    rate_source: str
    rate_timestamp: datetime

    def amount(self, value: float | None) -> float | None:
        if value is None:
            return None
        return round(float(value) * self.rate, 2)

    def metadata(self, *, source_amount: float | None, converted_amount: float | None) -> dict[str, object]:
        return {
            "sourceAmount": source_amount,
            "sourceCurrency": self.source_currency,
            "targetCurrency": self.target_currency,
            "rate": self.rate,
            "rateSource": self.rate_source,
            "rateTimestamp": self.rate_timestamp.isoformat().replace("+00:00", "Z"),
            "convertedAmount": converted_amount,
        }


def resolve_currency_conversion(
    *,
    source_currency: str,
    target_currency: str,
    rates: dict[str, float],
    rate_source: str,
    now: datetime,
) -> CurrencyConversion:
    source = source_currency.strip().upper()
    target = target_currency.strip().upper()
    if source == target:
        return CurrencyConversion(
            source_currency=source,
            target_currency=target,
            rate=1.0,
            rate_source="same_currency",
            rate_timestamp=now,
        )
    key = f"{source}:{target}"
    rate = rates.get(key)
    if rate is None:
        raise ValueError(f"Missing currency conversion rate for {key}")
    if rate <= 0:
        raise ValueError(f"Currency conversion rate for {key} must be > 0")
    return CurrencyConversion(
        source_currency=source,
        target_currency=target,
        rate=float(rate),
        rate_source=rate_source,
        rate_timestamp=now,
    )
