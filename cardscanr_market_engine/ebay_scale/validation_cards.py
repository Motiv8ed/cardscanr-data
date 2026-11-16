from __future__ import annotations

from .set_sweep_matcher import SetSweepCatalogueCard

DEFAULT_VALIDATION_CARD_LIMIT = 10


def select_validation_per_card_ids(
    catalogue: list[SetSweepCatalogueCard],
    *,
    reference_prices: dict[str, float],
    max_cards: int = DEFAULT_VALIDATION_CARD_LIMIT,
) -> set[str]:
    """Pick up to N highest-value cards in the set for paired per-card price jobs (same run).

    Does not enqueue jobs itself — caller must respect existing hourly/daily caps.
    """
    limit = max(0, int(max_cards))
    if limit == 0 or not catalogue:
        return set()

    def sort_key(card: SetSweepCatalogueCard) -> float:
        ref = reference_prices.get(card.catalogue_card_id)
        if ref is not None:
            return float(ref)
        if "/" in card.collector_number:
            try:
                num = int(card.collector_number.split("/")[0])
                return float(num)
            except ValueError:
                pass
        return 0.0

    ordered = sorted(catalogue, key=sort_key, reverse=True)
    return {card.catalogue_card_id for card in ordered[:limit]}