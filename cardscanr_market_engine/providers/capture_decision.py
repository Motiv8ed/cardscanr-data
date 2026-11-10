"""Single authoritative PageSnapshot -> PageDecision classifier (pure, no IO)."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .capture_evidence import (
    PROTOCOL_VERSION,
    PageSnapshot,
    ValidationError,
    derive_policy_signals,
    validate_page_snapshot,
)

AUTH_ENVELOPE_KINDS = frozenset({"visible_auth_modal", "visible_auth_form", "session_expired"})


class PageOutcome(str, Enum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CHALLENGE_STOP = "CHALLENGE_STOP"
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    SORRY_COOLDOWN = "SORRY_COOLDOWN"
    AMBIGUOUS_NO_PRICE = "AMBIGUOUS_NO_PRICE"
    INCOMPLETE_TRANSIENT = "INCOMPLETE_TRANSIENT"
    VERIFIED_SOLD_RESULTS = "VERIFIED_SOLD_RESULTS"
    VERIFIED_NO_RESULTS = "VERIFIED_NO_RESULTS"


class LatchAction(str, Enum):
    SECURITY_HARD_STOP_RETAIN_NO_AUTO_CLEAR = "SECURITY_HARD_STOP_RETAIN_NO_AUTO_CLEAR"
    TRANSIENT_FAILURE_RETAIN_THRESHOLDS = "TRANSIENT_FAILURE_RETAIN_THRESHOLDS"


class RecoveryAction(str, Enum):
    STOP_NO_AUTO_REFRESH_OR_RETRY = "STOP_NO_AUTO_REFRESH_OR_RETRY"
    BOUNDED_NEXT_JOB_FRESH_SURFACE = "BOUNDED_NEXT_JOB_FRESH_SURFACE"


class AuditClass(str, Enum):
    BOUNDED_REDACTED_NO_FRESHNESS_DELTA = "BOUNDED_REDACTED_NO_FRESHNESS_DELTA"


@dataclass(frozen=True, slots=True)
class PageDecision:
    decision_id: str
    protocol_version: str
    snapshot_id: str
    snapshot_content_hash: str
    outcome: PageOutcome
    may_parse_price: bool
    may_produce_usable_comp: bool
    may_calculate_price: bool
    may_publish_market: bool
    may_write_failure_audit: bool
    latch_action: str
    recovery_action: str
    primary_reason: str
    all_reasons: tuple[str, ...]
    policy_row_id: str | None = None


def _policy_row_id(signals: Mapping[str, bool]) -> str:
    keys = (
        "auth_observed",
        "active_challenge_observed",
        "sorry_observed",
        "capture_incomplete",
        "optional_banner_present",
        "prior_state_present",
        "sold_looking_markers",
        "hidden_script_css_comment_tokens",
        "binding_valid_current",
        "access_403_observed",
        "complete_current_sold_document",
        "verified_current_zero_results",
    )
    bits = "".join("1" if signals.get(k) else "0" for k in keys)
    ordinal = int(bits, 2)
    return f"M{ordinal:04d}"


def classify_policy_signals(signals: Mapping[str, bool]) -> PageOutcome:
    """Authoritative 12-dimension reducer matching MIXED_SIGNAL_OUTCOME_TABLE.csv."""

    def b(key: str) -> bool:
        return bool(signals.get(key))

    if not b("binding_valid_current"):
        return PageOutcome.AMBIGUOUS_NO_PRICE
    if b("auth_observed"):
        return PageOutcome.AUTH_REQUIRED
    if b("active_challenge_observed"):
        return PageOutcome.CHALLENGE_STOP
    if b("access_403_observed"):
        return PageOutcome.ACCESS_BLOCKED
    if b("sorry_observed"):
        return PageOutcome.SORRY_COOLDOWN
    if b("capture_incomplete"):
        return PageOutcome.INCOMPLETE_TRANSIENT
    if b("complete_current_sold_document") and b("verified_current_zero_results"):
        return PageOutcome.AMBIGUOUS_NO_PRICE
    if b("complete_current_sold_document"):
        return PageOutcome.VERIFIED_SOLD_RESULTS
    if b("verified_current_zero_results"):
        return PageOutcome.VERIFIED_NO_RESULTS
    return PageOutcome.INCOMPLETE_TRANSIENT


def _permissions_for(outcome: PageOutcome) -> tuple[bool, bool, bool, bool, bool, str, str]:
    may_price = outcome == PageOutcome.VERIFIED_SOLD_RESULTS
    may_comp = may_price
    may_calc = may_price
    may_pub = may_price
    audit = True
    if outcome in {
        PageOutcome.AUTH_REQUIRED,
        PageOutcome.CHALLENGE_STOP,
        PageOutcome.ACCESS_BLOCKED,
        PageOutcome.AMBIGUOUS_NO_PRICE,
    }:
        latch = LatchAction.SECURITY_HARD_STOP_RETAIN_NO_AUTO_CLEAR.value
        recovery = RecoveryAction.STOP_NO_AUTO_REFRESH_OR_RETRY.value
    elif outcome == PageOutcome.SORRY_COOLDOWN:
        latch = LatchAction.TRANSIENT_FAILURE_RETAIN_THRESHOLDS.value
        recovery = RecoveryAction.BOUNDED_NEXT_JOB_FRESH_SURFACE.value
    elif outcome == PageOutcome.INCOMPLETE_TRANSIENT:
        latch = LatchAction.TRANSIENT_FAILURE_RETAIN_THRESHOLDS.value
        recovery = RecoveryAction.BOUNDED_NEXT_JOB_FRESH_SURFACE.value
    else:
        latch = LatchAction.TRANSIENT_FAILURE_RETAIN_THRESHOLDS.value
        recovery = RecoveryAction.STOP_NO_AUTO_REFRESH_OR_RETRY.value
    return may_price, may_comp, may_calc, may_pub, audit, latch, recovery


def _auth_from_envelope(snapshot: PageSnapshot, *, binding_valid: bool) -> bool:
    if not binding_valid:
        return False
    for fact in snapshot.facts:
        if fact.kind not in AUTH_ENVELOPE_KINDS:
            continue
        if fact.state.value == "TRUE" and fact.assurance.value == "QUALIFIED_CURRENT":
            return True
    return False


def classify_snapshot(snapshot: PageSnapshot, *, now: Any | None = None) -> PageDecision:
    """Sole permission authority. Rejects invalid protocol before classification."""
    try:
        validate_page_snapshot(snapshot, now=now)
    except ValidationError as exc:
        outcome = PageOutcome.AMBIGUOUS_NO_PRICE
        reasons = (str(exc), "invalid_envelope_no_confirmed_auth")
        return _finalize_decision(snapshot, outcome, reasons, row_id=None)

    signals = derive_policy_signals(snapshot, now=now)
    if _auth_from_envelope(snapshot, binding_valid=True):
        signals = dict(signals)
        signals["auth_observed"] = True
    outcome = classify_policy_signals(signals)
    reasons = tuple(
        sorted(
            {
                f"{k}={signals[k]}"
                for k in signals
                if signals[k] and k.endswith("_observed") or k.endswith("_present") or k.startswith("complete")
            }
        )
    )
    row_id = _policy_row_id(signals)
    return _finalize_decision(snapshot, outcome, reasons or (outcome.value,), row_id=row_id)


def _finalize_decision(
    snapshot: PageSnapshot,
    outcome: PageOutcome,
    reasons: tuple[str, ...],
    *,
    row_id: str | None,
) -> PageDecision:
    may_price, may_comp, may_calc, may_pub, audit, latch, recovery = _permissions_for(outcome)
    content_hash = snapshot.content_hash()
    return PageDecision(
        decision_id=str(uuid.uuid4()),
        protocol_version=PROTOCOL_VERSION,
        snapshot_id=snapshot.capture_id,
        snapshot_content_hash=content_hash,
        outcome=outcome,
        may_parse_price=may_price,
        may_produce_usable_comp=may_comp,
        may_calculate_price=may_calc,
        may_publish_market=may_pub,
        may_write_failure_audit=audit,
        latch_action=latch,
        recovery_action=recovery,
        primary_reason=reasons[0] if reasons else outcome.value,
        all_reasons=reasons,
        policy_row_id=row_id,
    )


def content_hash_canonical(decision: PageDecision) -> str:
    material = "|".join(
        (
            decision.protocol_version,
            decision.snapshot_content_hash,
            decision.outcome.value,
            str(decision.may_parse_price),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
