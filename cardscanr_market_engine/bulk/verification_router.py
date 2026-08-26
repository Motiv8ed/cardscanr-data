"""Decide when keys should enter the eBay verification queue."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .display_price_policy import DisplayPriceDecision
from .price_semantics import ReferencePriceObservation, is_verified_provider


@dataclass(frozen=True)
class VerificationRouteDecision:
    should_verify: bool
    reason: str | None
    priority: int


def route_verification(
    *,
    prior_cache: dict[str, Any] | None,
    observation: ReferencePriceObservation | None,
    display: DisplayPriceDecision,
    value_signal: float,
    high_value_threshold: float = 50.0,
    now: datetime | None = None,
) -> VerificationRouteDecision:
    now = now or datetime.now(timezone.utc)
    prior = prior_cache or {}

    if display.verification_required:
        return VerificationRouteDecision(should_verify=True, reason=display.verification_reason or "pending_verification", priority=40)

    if observation is None or not observation.is_usable:
        return VerificationRouteDecision(should_verify=True, reason="missing_reference_price", priority=30)

    if observation.mapping_status == "ambiguous":
        return VerificationRouteDecision(should_verify=True, reason="ambiguous_mapping", priority=55)

    prior_provider = str(prior.get("provider") or "")
    if is_verified_provider(prior_provider):
        return VerificationRouteDecision(should_verify=False, reason="verified_recent", priority=100)

    if value_signal >= high_value_threshold:
        return VerificationRouteDecision(should_verify=True, reason="high_value", priority=70)

    if display.action in {"apply_reference", "no_change"} and display.display_price is not None:
        if value_signal < 5.0:
            return VerificationRouteDecision(should_verify=False, reason="stable_low_value_reference", priority=100)
        return VerificationRouteDecision(should_verify=False, reason="reference_sufficient", priority=100)

    return VerificationRouteDecision(should_verify=True, reason="reference_insufficient", priority=80)
