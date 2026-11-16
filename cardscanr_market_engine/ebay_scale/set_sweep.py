from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from .set_sweep_matcher import (
    SetSweepCatalogueCard,
    classify_unmatched_rejection,
    match_sold_row_to_catalogue,
)
from .sold_row_html import SoldResultsRow, parse_sold_results_html


@dataclass
class SetSweepStats:
    rows_parsed: int = 0
    rows_matched: int = 0
    distinct_cards_priced: int = 0
    rows_rejected: dict[str, int] = field(default_factory=dict)
    pricing_mode: str = "set_median"  # set_median | exact_card
    card_prices: dict[str, list[float]] = field(default_factory=dict)
    card_price_labels: dict[str, str] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "rowsParsed": self.rows_parsed,
            "rowsMatched": self.rows_matched,
            "distinctCardsPriced": self.distinct_cards_priced,
            "rowsRejectedByReason": dict(self.rows_rejected),
            "pricingMode": self.pricing_mode,
            "cardPriceLabels": dict(self.card_price_labels),
        }


def run_set_sweep(
    *,
    html: str,
    catalogue: list[SetSweepCatalogueCard],
    exact_card_threshold_aud: float = 20.0,
) -> tuple[SetSweepStats, list[SoldResultsRow]]:
    rows = parse_sold_results_html(html)
    stats = SetSweepStats(rows_parsed=len(rows))
    sample = catalogue[0] if catalogue else None
    for index, row in enumerate(rows):
        match = match_sold_row_to_catalogue(row, catalogue=catalogue, index=index)
        if match is None:
            reason = (
                classify_unmatched_rejection(row, sample_card=sample, index=index)
                if sample is not None
                else "unmatched"
            )
            stats.rows_rejected[reason] = stats.rows_rejected.get(reason, 0) + 1
            continue
        if match.confidence != "high":
            stats.rows_rejected["ambiguous"] = stats.rows_rejected.get("ambiguous", 0) + 1
            continue
        stats.rows_matched += 1
        price = row.sold_price
        if price is None:
            stats.rows_rejected["missing_price"] = stats.rows_rejected.get("missing_price", 0) + 1
            continue
        stats.card_prices.setdefault(match.catalogue_card_id, []).append(price)
        if price >= exact_card_threshold_aud:
            stats.card_price_labels[match.catalogue_card_id] = "exact_per_card_search_required"
        else:
            stats.card_price_labels[match.catalogue_card_id] = "set_sweep_median_unverified"

    stats.distinct_cards_priced = len(stats.card_prices)
    return stats, rows


def median_prices_by_card(stats: SetSweepStats) -> dict[str, float]:
    out: dict[str, float] = {}
    for card_id, prices in stats.card_prices.items():
        if prices:
            out[card_id] = round(float(median(prices)), 2)
    return out