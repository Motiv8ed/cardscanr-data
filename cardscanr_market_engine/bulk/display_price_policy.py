"""Choose displayed cache price from reference vs verified evidence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..price_movement_guard import PriceMovementDecision, evaluate_price_movement
from ..price_source_precedence import (
    can_proposed_replace_selected,
    display_source_for_tier,
    provider_tier,
    TIER_FRESH_EBAY,
    TIER_STALE_EBAY,
)
from .price_semantics import ReferencePriceObservation, is_verified_provider


@dataclass(frozen=True)
class DisplayPriceDecision:
    action: str  # apply_reference | preserve_verified | pending_verification | reject_reference | no_change
    display_price: float | None
    display_source: str  # reference | verified_au | verified_local | pending_verification
    provider: str | None
    marketplace: str | None
    confidence: str
    movement: PriceMovementDecision | None
    reference_price: float | None
    reference_provider: str | None
    verification_required: bool
    verification_reason: str | None
    diagnostics: dict[str, Any]


def _prior_display_source(prior: dict[str, Any]) -> str:
    raw = str(prior.get("display_price_source") or "").strip().lower()
    if raw:
        return raw
    if is_verified_provider(str(prior.get("provider") or "")):
        market = str(prior.get("market_country") or "").strip().upper()
        return "verified_au" if market == "AU" else "verified_local"
    return "reference"


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
    prior_display_source = _prior_display_source(prior)
    prior_price = prior.get("current_market_price")
    prior_marketplace = str(prior.get("marketplace") or "") or None

    movement = evaluate_price_movement(
        old_price=prior_price,
        new_price=converted_price,
        included_count=1,
        confidence=observation.confidence,
        prior_confidence=str(prior.get("confidence") or "") or None,
        prior_included_count=prior.get("sample_size"),
    )

    # Central source-precedence guard: valid eBay/selected higher tier cannot be
    # silently replaced by a later reference refresh.
    may_replace, replace_reason = can_proposed_replace_selected(
        current_provider=prior_provider or None,
        current_price=prior_price,
        current_display_source=prior_display_source,
        current_observed_at=prior.get("last_updated_at"),
        proposed_provider=observation.provider,
        proposed_price=converted_price,
        proposed_display_source="reference",
        proposed_observed_at=now,
        now=now,
    )
    if not may_replace and prior_price is not None:
        try:
            keep_price = float(prior_price)
        except (TypeError, ValueError):
            keep_price = None
        if keep_price is not None and keep_price > 0:
            keep_source = prior_display_source
            if is_verified_provider(prior_provider):
                tier = provider_tier(
                    provider=prior_provider,
                    display_source=prior_display_source,
                    observed_at=None,
                    now=now,
                )
                if tier in {TIER_FRESH_EBAY, TIER_STALE_EBAY}:
                    keep_source = display_source_for_tier(
                        tier,
                        market_country=str(prior.get("market_country") or ""),
                    )
            return DisplayPriceDecision(
                action="preserve_verified",
                display_price=keep_price,
                display_source=keep_source,
                provider=prior_provider or None,
                marketplace=prior_marketplace,
                confidence=str(prior.get("confidence") or "medium"),
                movement=movement,
                reference_price=converted_price,
                reference_provider=observation.provider,
                verification_required=False,
                verification_reason=None,
                diagnostics={
                    "policy": "source_precedence_blocks_reference",
                    "reason": replace_reason,
                    "targetCurrency": target_currency.upper(),
                },
            )

    if is_verified_provider(prior_provider) and prior_price is not None:
        if movement.action in {"pending_verification", "reject_weak"}:
            return DisplayPriceDecision(
                action="pending_verification",
                display_price=float(prior_price),
                display_source=_prior_display_source(prior),
                provider=prior_provider,
                marketplace=prior_marketplace,
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
            display_source=_prior_display_source(prior),
            provider=prior_provider,
            marketplace=prior_marketplace,
            confidence=str(prior.get("confidence") or "medium"),
            movement=movement,
            reference_price=converted_price,
            reference_provider=observation.provider,
            verification_required=False,
            verification_reason=None,
            diagnostics={"policy": "verified_provider_beats_reference"},
        )

    if movement.action == "reject_weak":
        return DisplayPriceDecision(
            action="reject_reference",
            display_price=float(prior_price) if prior_price is not None else None,
            display_source=str(prior_display_source or "reference") or "reference",
            provider=prior_provider or None,
            marketplace=prior_marketplace,
            confidence=str(prior.get("confidence") or "low"),
            movement=movement,
            reference_price=converted_price,
            reference_provider=observation.provider,
            verification_required=True,
            verification_reason=movement.reason,
            diagnostics={"policy": "reject_weak_reference"},
        )

    if movement.action == "pending_verification":
        # Do not promote reference provider into selected fields while pending.
        return DisplayPriceDecision(
            action="pending_verification",
            display_price=float(prior_price) if prior_price is not None else None,
            display_source=str(prior_display_source or "pending_verification") or "pending_verification",
            provider=prior_provider or None,
            marketplace=prior_marketplace,
            confidence=str(prior.get("confidence") or observation.confidence),
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
