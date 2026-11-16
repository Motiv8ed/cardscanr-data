from __future__ import annotations

from typing import Any

from .config import should_write_set_sweep_live_price
from .shadow_audit import record_set_sweep_shadow_audit
from .set_sweep_matcher import SetSweepCatalogueCard
from .validation_cards import select_validation_per_card_ids


def apply_set_sweep_pricing_batch(
    *,
    sweep_id: str,
    set_code: str,
    set_name: str,
    html: str,
    catalogue: list[SetSweepCatalogueCard],
    per_card_prices: dict[str, float],
) -> dict[str, Any]:
    """Shadow-safe set sweep entrypoint for live worker integration (local audit only)."""
    validation_ids = select_validation_per_card_ids(catalogue, reference_prices=per_card_prices)
    audit_result = record_set_sweep_shadow_audit(
        sweep_id=sweep_id,
        set_code=set_code,
        set_name=set_name,
        html=html,
        catalogue=catalogue,
        per_card_prices=per_card_prices,
        validation_card_ids=validation_ids,
    )
    live = should_write_set_sweep_live_price()
    return {
        "audit": audit_result,
        "validationPerCardIds": sorted(validation_ids),
        "livePriceWrite": live,
        "livePricesApplied": live and audit_result.get("recorded"),
    }