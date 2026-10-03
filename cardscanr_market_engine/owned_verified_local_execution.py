"""Source-aware execution eligibility for owned verified-local eBay pricing.

Separates:
  1) SHOULD THIS OWNED TARGET BE SCHEDULED?  (owned_daily scheduler bands)
  2) SHOULD THIS PRICING JOB EXECUTE?        (job_runner pre-provider gate)

Both use the same source-aware freshness view so a recent REFERENCE-ONLY
`last_updated_at` cannot satisfy verified-local browser freshness.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from .owned_daily_source_policy import classify_owned_daily_band
from .scheduler import utc_iso


@dataclass(frozen=True)
class OwnedVerifiedLocalExecutionDecision:
    should_execute: bool
    reason_code: str
    source_class: str
    verified_local: bool
    reference_only: bool
    successful_verified_at: str | None
    next_eligible_at: str | None
    scheduler_band: str
    scheduler_due: bool
    would_skip_fresh: bool
    pricing_intent: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        return out


PRICING_INTENT_OWNED_VERIFIED_LOCAL = "OWNED_VERIFIED_LOCAL_REFRESH"
PRICING_INTENT_GENERIC = "GENERIC_CACHE_FRESHNESS"


def job_requests_owned_verified_local(*, reason: str | None, pricing_intent: str | None = None) -> bool:
    """Detect owned verified-local browser pricing intent from job context.

    Accepts:
      - reason starting with or containing ``owned_daily:``
      - explicit pricing_intent = OWNED_VERIFIED_LOCAL_REFRESH
      - reliability harness reasons that embed owned_daily scheduler reasons
    """
    intent = str(pricing_intent or "").strip().upper()
    if intent == PRICING_INTENT_OWNED_VERIFIED_LOCAL:
        return True
    text = str(reason or "").strip().lower()
    return "owned_daily:" in text


def evaluate_owned_verified_local_execution(
    cache_or_target: dict[str, Any] | None,
    *,
    now: datetime,
    success_fresh_hours: int = 24,
    pricing_intent: str = PRICING_INTENT_OWNED_VERIFIED_LOCAL,
) -> OwnedVerifiedLocalExecutionDecision:
    """Decide whether an owned verified-local pricing job should execute.

    Required cases:
      - no price → EXECUTE
      - reference_only / structured_fallback (never verified-local) → EXECUTE
      - verified_local + fresh → SKIP_ALREADY_FRESH
      - verified_local + stale → EXECUTE
      - prior local infra failure + no verified-local → EXECUTE
    """
    row = dict(cache_or_target or {})
    band, due, reason_suffix, view, extras = classify_owned_daily_band(
        row,
        now=now,
        success_fresh_hours=success_fresh_hours,
    )
    source_class = str(view.source_class)
    verified_local = bool(view.has_verified_local_price)
    reference_only = bool(view.has_reference_only_price) or source_class == "reference_only"
    success_at = utc_iso(view.ebay_success_freshness_at) if view.ebay_success_freshness_at else None

    next_eligible: str | None = None
    if verified_local and view.ebay_success_freshness_at is not None and not due:
        next_eligible = utc_iso(view.ebay_success_freshness_at + timedelta(hours=success_fresh_hours))

    if due:
        reason_code = f"EXECUTE_{reason_suffix.upper()}"
        if source_class in {"reference_only", "structured_fallback"}:
            reason_code = "EXECUTE_REFERENCE_ONLY_NEEDS_VERIFIED_LOCAL"
        elif not view.has_any_price:
            reason_code = "EXECUTE_NO_PRICE"
        elif band == "P1_STALE_GT_24H":
            reason_code = "EXECUTE_VERIFIED_LOCAL_STALE"
        elif band == "P2_FAILED_RETRY":
            reason_code = "EXECUTE_FAILED_RETRY"
        return OwnedVerifiedLocalExecutionDecision(
            should_execute=True,
            reason_code=reason_code,
            source_class=source_class,
            verified_local=verified_local,
            reference_only=reference_only,
            successful_verified_at=success_at,
            next_eligible_at=next_eligible,
            scheduler_band=band,
            scheduler_due=True,
            would_skip_fresh=False,
            pricing_intent=pricing_intent,
            details={**extras, "bandReason": reason_suffix},
        )

    # FRESH_SKIP — only verified-local still within success window.
    return OwnedVerifiedLocalExecutionDecision(
        should_execute=False,
        reason_code="SKIP_ALREADY_FRESH_VERIFIED_LOCAL",
        source_class=source_class,
        verified_local=verified_local,
        reference_only=reference_only,
        successful_verified_at=success_at,
        next_eligible_at=next_eligible,
        scheduler_band=band,
        scheduler_due=False,
        would_skip_fresh=True,
        pricing_intent=pricing_intent,
        details={**extras, "bandReason": reason_suffix},
    )


def scheduler_jobrunner_agreement(
    *,
    scheduler_due: bool,
    execution: OwnedVerifiedLocalExecutionDecision,
) -> dict[str, Any]:
    """Invariant: schedulerDue + owned verified-local intent ⇒ executionEligible.

    Legitimate disagreement only when state changed (e.g. newly verified-local).
    """
    agrees = (not scheduler_due) or bool(execution.should_execute)
    return {
        "schedulerDue": scheduler_due,
        "executionEligible": execution.should_execute,
        "wouldSkipFresh": execution.would_skip_fresh,
        "agrees": agrees,
        "reasonCode": execution.reason_code,
        "schedulerBand": execution.scheduler_band,
        "sourceClass": execution.source_class,
    }


__all__ = [
    "OwnedVerifiedLocalExecutionDecision",
    "PRICING_INTENT_GENERIC",
    "PRICING_INTENT_OWNED_VERIFIED_LOCAL",
    "evaluate_owned_verified_local_execution",
    "job_requests_owned_verified_local",
    "scheduler_jobrunner_agreement",
]
