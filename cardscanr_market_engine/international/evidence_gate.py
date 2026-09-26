"""Evidence sufficiency gate for user-facing international estimates."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import PricingStats

# Independent exact-card comps required before any user-facing international value.
MIN_RANGE_INCLUDED = 3
# High-confidence numeric point estimate (with est. suffix).
MIN_NUMERIC_INCLUDED = 8
# Extreme dispersion → unavailable even when sample size looks large enough.
EXTREME_SPREAD_RATIO = 5.0

UNAVAILABLE_INSUFFICIENT = "insufficient_international_evidence"
UNAVAILABLE_LOW_CONFIDENCE = "insufficient_international_evidence_low_confidence"
UNAVAILABLE_HIGH_DISPERSION = "high_dispersion_international_evidence"


@dataclass(frozen=True)
class InternationalEvidenceDecision:
    """Whether international comps may become a user-facing price."""

    outcome: str  # numeric_estimate | range_estimate | unavailable
    reason: str
    allows_user_facing_price: bool
    show_as_range: bool
    included_count: int
    confidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "allowsUserFacingPrice": self.allows_user_facing_price,
            "showAsRange": self.show_as_range,
            "includedCount": self.included_count,
            "confidence": self.confidence,
        }


def evaluate_international_evidence_gate(
    *,
    included_count: int,
    confidence: str,
    recommended_price: float | None,
    price_spread_ratio: float | None = None,
    price_reliability: str | None = None,
) -> InternationalEvidenceDecision:
    """Gate international comps before minting a user-facing estimate.

    Policy:
    - 0 / 1 / 2 comps, or low confidence → Price unavailable (no numeric).
    - medium confidence with ≥3 comps and non-extreme spread → range estimate.
    - high confidence with ≥8 comps and non-extreme spread → numeric estimate.
    - extreme dispersion (spread ratio > 5) → unavailable.
    """
    included = max(0, int(included_count or 0))
    conf = str(confidence or "").strip().lower() or "low"
    reliability = str(price_reliability or "").strip().lower()

    if included <= 0 or recommended_price is None:
        return InternationalEvidenceDecision(
            outcome="unavailable",
            reason=UNAVAILABLE_INSUFFICIENT,
            allows_user_facing_price=False,
            show_as_range=False,
            included_count=included,
            confidence=conf,
        )

    if reliability in {
        "no_reliable_price",
        "stale_single_comp",
        "stale_evidence_only",
        "single_comp_low_confidence",
    }:
        reason = (
            UNAVAILABLE_LOW_CONFIDENCE
            if reliability == "single_comp_low_confidence"
            else UNAVAILABLE_INSUFFICIENT
        )
        return InternationalEvidenceDecision(
            outcome="unavailable",
            reason=reason,
            allows_user_facing_price=False,
            show_as_range=False,
            included_count=included,
            confidence=conf,
        )

    if price_spread_ratio is not None and float(price_spread_ratio) > EXTREME_SPREAD_RATIO:
        return InternationalEvidenceDecision(
            outcome="unavailable",
            reason=UNAVAILABLE_HIGH_DISPERSION,
            allows_user_facing_price=False,
            show_as_range=False,
            included_count=included,
            confidence=conf,
        )

    if conf == "high" and included >= MIN_NUMERIC_INCLUDED:
        return InternationalEvidenceDecision(
            outcome="numeric_estimate",
            reason="sufficient_high_confidence_evidence",
            allows_user_facing_price=True,
            show_as_range=False,
            included_count=included,
            confidence=conf,
        )

    if conf == "medium" and included >= MIN_RANGE_INCLUDED:
        return InternationalEvidenceDecision(
            outcome="range_estimate",
            reason="sufficient_medium_confidence_evidence",
            allows_user_facing_price=True,
            show_as_range=True,
            included_count=included,
            confidence=conf,
        )

    return InternationalEvidenceDecision(
        outcome="unavailable",
        reason=UNAVAILABLE_LOW_CONFIDENCE,
        allows_user_facing_price=False,
        show_as_range=False,
        included_count=included,
        confidence=conf,
    )


def evaluate_international_evidence_gate_from_stats(
    stats: PricingStats,
) -> InternationalEvidenceDecision:
    return evaluate_international_evidence_gate(
        included_count=int(stats.included_count or 0),
        confidence=str(stats.confidence or "low"),
        recommended_price=stats.recommended_price,
        price_spread_ratio=stats.price_spread_ratio,
        price_reliability=str(stats.price_reliability or ""),
    )
