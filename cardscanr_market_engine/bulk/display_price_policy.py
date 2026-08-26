"""Choose displayed cache price from reference vs verified evidence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..price_movement_guard import PriceMovementDecision, evaluate_price_movement
from .price_semantics import ReferencePriceObservation, is_verified_provider


@dataclass(frozen=True)
class DisplayPriceDecision:
    action: str  # apply_reference | preserve_verified | pending_verification | reject_reference | no_change
    display_price: float | None
    display_source: str  # reference | verified_au | pending_verification
    provider: str | None
    marketplace: str | None
    confidence: str
    movement: PriceMovementDecision | None
    reference_price: float | None
    reference_provider: str | None
    verification_required: bool
    verification_reason: str | None
    diagnostics: dict[str, Any]


def decide_display_price(
    *,
    prior_cache: dict[str, Any] | None,
    observation: ReferencePriceObservation,
    converted_price: float,
    target_currency: str,
    now: datetime | None = None,
) -> DisplayPriceDecision:
    now = now or datetime.now(timezone.utc)
    prior = prior_cache or {}
    prior_provider = str(prior.get("provider") or "")
    prior_display_source = str(prior.get("display_price_source") or "")
    prior_price = prior.get("current_market_price")
    verified_recent = is_verified_provider(prior_provider) and prior_price is not None

    movement = evaluate_price_movement(
        old_price=prior_price,
        new_price=converted_price,
        included_count=1,
        confidence=observation.confidence,
    )

    if verified_recent and prior_display_source == "verified_au":
        if movement.action in {"pending_verification", "reject_weak"}:
            return DisplayPriceDecision(
                action="pending_verification",
                display_price=float(prior_price),
                display_source="verified_au",
                provider=prior_provider,
                marketplace=str(prior.get("marketplace") or ""),
                confidence=str(prior.get("confidence") or "medium"),
                movement=movement,
                reference_price=converted_price,
                reference_provider=observation.provider,
                verification_required=True,
                verification_reason=movement.reason,
                diagnostics={"policy": "preserve_verified_on_large_move"},
            )
        return DisplayPriceDecision(
            action="preserve_verified",
            display_price=float(prior_price),
            display_source="verified_au",
            provider=prior_provider,
            marketplace=str(prior.get("marketplace") or ""),
            confidence=str(prior.get("confidence") or "medium"),
            movement=movement,
            reference_price=converted_price,
            reference_provider=observation.provider,
            verification_required=False,
            verification_reason=None,
            diagnostics={"policy": "verified_au_beats_reference"},
        )

    if movement.action == "reject_weak":
        return DisplayPriceDecision(
            action="reject_reference",
            display_price=float(prior_price) if prior_price is not None else None,
            display_source=str(prior_display_source or "reference") or "reference",
            provider=prior_provider or None,
            marketplace=str(prior.get("marketplace") or "") or None,
            confidence=str(prior.get("confidence") or "low"),
            movement=movement,
            reference_price=converted_price,
            reference_provider=observation.provider,
            verification_required=True,
            verification_reason=movement.reason,
            diagnostics={"policy": "reject_weak_reference"},
        )

    if movement.action == "pending_verification":
        return DisplayPriceDecision(
            action="pending_verification",
            display_price=float(prior_price) if prior_price is not None else None,
            display_source="pending_verification",
            provider=observation.provider,
            marketplace="REFERENCE",
            confidence=observation.confidence,
            movement=movement,
            reference_price=converted_price,
            reference_provider=observation.provider,
            verification_required=True,
            verification_reason=movement.reason,
            diagnostics={"policy": "reference_pending_verification"},
        )

    unchanged = prior_price is not None and abs(float(prior_price) - converted_price) < 0.01
    if unchanged and str(prior.get("reference_provider") or prior_provider) == observation.provider:
        return DisplayPriceDecision(
            action="no_change",
            display_price=float(prior_price),
            display_source="reference",
            provider=observation.provider,
            marketplace="REFERENCE",
            confidence=observation.confidence,
            movement=movement,
            reference_price=converted_price,
            reference_provider=observation.provider,
            verification_required=False,
            verification_reason=None,
            diagnostics={"policy": "unchanged_reference"},
        )

    return DisplayPriceDecision(
        action="apply_reference",
        display_price=converted_price,
        display_source="reference",
        provider=observation.provider,
        marketplace="REFERENCE",
        confidence=observation.confidence,
        movement=movement,
        reference_price=converted_price,
        reference_provider=observation.provider,
        verification_required=False,
        verification_reason=None,
        diagnostics={
            "policy": "apply_reference",
            "targetCurrency": target_currency.upper(),
            "sourceCurrency": observation.source_currency,
            "sourceMarket": observation.source_market,
            "mappingStatus": observation.mapping_status,
        },
    )
