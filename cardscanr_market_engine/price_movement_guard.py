"""Large price-movement safety for shared market price cache updates."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PriceMovementDecision:
    action: str  # accept | pending_verification | reject_weak
    reason: str
    pct_change: float | None
    abs_change: float | None
    old_price: float | None
    new_price: float | None
    diagnostics: dict[str, Any]


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_price_movement(
    *,
    old_price: Any,
    new_price: Any,
    included_count: int = 0,
    confidence: str | None = None,
    absolute_threshold: float = 15.0,
    percent_threshold: float = 50.0,
    low_price_floor: float = 2.0,
    min_included_for_large_move: int = 3,
) -> PriceMovementDecision:
    """Decide whether a proposed price can overwrite a trusted prior price.

    Rules of thumb:
    - Tiny absolute moves on cheap cards: accept even if % is large ($0.20->$0.40).
    - Large absolute + large % moves ($65->$5): require stronger evidence or pending verification.
    """
    old_v = _f(old_price)
    new_v = _f(new_price)
    if new_v is None:
        return PriceMovementDecision(
            action="accept",
            reason="no_new_price",
            pct_change=None,
            abs_change=None,
            old_price=old_v,
            new_price=new_v,
            diagnostics={},
        )
    if old_v is None or old_v <= 0:
        return PriceMovementDecision(
            action="accept",
            reason="no_prior_trusted_price",
            pct_change=None,
            abs_change=None,
            old_price=old_v,
            new_price=new_v,
            diagnostics={},
        )

    abs_change = abs(new_v - old_v)
    pct_change = (abs_change / old_v) * 100.0
    diag = {
        "absoluteThreshold": absolute_threshold,
        "percentThreshold": percent_threshold,
        "lowPriceFloor": low_price_floor,
        "includedCount": int(included_count),
        "confidence": confidence,
        "minIncludedForLargeMove": min_included_for_large_move,
    }

    # Cheap cards: percentage alone is noisy.
    if max(old_v, new_v) < low_price_floor and abs_change < absolute_threshold:
        return PriceMovementDecision(
            action="accept",
            reason="low_price_band_absolute_ok",
            pct_change=round(pct_change, 4),
            abs_change=round(abs_change, 4),
            old_price=old_v,
            new_price=new_v,
            diagnostics=diag,
        )

    large = abs_change >= absolute_threshold and pct_change >= percent_threshold
    if not large:
        return PriceMovementDecision(
            action="accept",
            reason="within_movement_thresholds",
            pct_change=round(pct_change, 4),
            abs_change=round(abs_change, 4),
            old_price=old_v,
            new_price=new_v,
            diagnostics=diag,
        )

    # Extreme moves (e.g. $65 -> $5) need a higher bar than ordinary large moves.
    extreme = pct_change >= 70.0 or abs_change >= 40.0
    conf = (confidence or "").strip().lower()
    included = int(included_count)
    if included <= 0:
        return PriceMovementDecision(
            action="reject_weak",
            reason="large_move_without_included_evidence",
            pct_change=round(pct_change, 4),
            abs_change=round(abs_change, 4),
            old_price=old_v,
            new_price=new_v,
            diagnostics=diag,
        )

    if extreme:
        # Preserve prior price until high-confidence, high-sample verification.
        if conf == "high" and included >= max(8, min_included_for_large_move):
            return PriceMovementDecision(
                action="accept",
                reason="extreme_move_verified_by_high_confidence",
                pct_change=round(pct_change, 4),
                abs_change=round(abs_change, 4),
                old_price=old_v,
                new_price=new_v,
                diagnostics={**diag, "extremeMove": True},
            )
        return PriceMovementDecision(
            action="pending_verification",
            reason="extreme_move_needs_verification",
            pct_change=round(pct_change, 4),
            abs_change=round(abs_change, 4),
            old_price=old_v,
            new_price=new_v,
            diagnostics={**diag, "extremeMove": True},
        )

    strong = included >= min_included_for_large_move and conf in {"medium", "high"}
    if strong:
        return PriceMovementDecision(
            action="accept",
            reason="large_move_verified_by_strong_evidence",
            pct_change=round(pct_change, 4),
            abs_change=round(abs_change, 4),
            old_price=old_v,
            new_price=new_v,
            diagnostics=diag,
        )

    return PriceMovementDecision(
        action="pending_verification",
        reason="large_move_needs_verification",
        pct_change=round(pct_change, 4),
        abs_change=round(abs_change, 4),
        old_price=old_v,
        new_price=new_v,
        diagnostics=diag,
    )


def movement_diagnostics(decision: PriceMovementDecision) -> dict[str, Any]:
    return {
        "priceMovementAction": decision.action,
        "priceMovementReason": decision.reason,
        "priceMovementPct": decision.pct_change,
        "priceMovementAbs": decision.abs_change,
        "previousTrustedPrice": decision.old_price,
        "proposedPrice": decision.new_price,
        **(decision.diagnostics or {}),
    }
