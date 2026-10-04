"""Continuous AU worker gate: cooldown wait vs hard-stop vs resume."""

from __future__ import annotations

from typing import Any

from .ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from .owned_daily_outcomes import (
    CHALLENGE_REQUIRED,
    EBAY_ACCESS_DENIED_403,
    EBAY_CHALLENGE_REQUIRED,
    TEMPORARY_EBAY_SERVER_FAILURE,
    UNACCOUNTED_SEARCH_URL_NAVIGATION,
)
from .owned_daily_pacing import OwnedDailyPacingController


HARD_STOP_OUTCOMES = frozenset(
    {
        CHALLENGE_REQUIRED,
        EBAY_CHALLENGE_REQUIRED,
        EBAY_ACCESS_DENIED_403,
        UNACCOUNTED_SEARCH_URL_NAVIGATION,
    }
)


def classify_continuous_gate(*, market: str = "AU") -> dict[str, Any]:
    gate = evaluate_ebay_browser_work_gate(market=market, for_probe=False)
    probe = evaluate_ebay_browser_work_gate(market=market, for_probe=True)
    payload = gate.to_dict() if hasattr(gate, "to_dict") else {}
    challenges = int(getattr(gate, "active_challenge_count", 0) or 0)
    integrity = bool(getattr(gate, "state_integrity_ok", True))
    state = str(getattr(gate, "availability_state", "") or "").upper()
    allowed = bool(getattr(gate, "allowed", False))
    hard = None
    worker_state = "SELECTING"
    if not integrity:
        hard = "STATE_INTEGRITY_FAILURE"
        worker_state = "HARD_STOP"
    elif challenges > 0 or state == "CHALLENGE_REQUIRED":
        hard = "ACTIVE_CHALLENGE"
        worker_state = "HARD_STOP"
    elif allowed:
        worker_state = "SELECTING"
    elif state == "COOLDOWN" or any(
        "COOLDOWN" in str(c) for c in (getattr(gate, "reason_codes", []) or [])
    ):
        worker_state = "COOLDOWN"
    elif state == "PROBE_REQUIRED" and bool(getattr(probe, "allowed", False)):
        worker_state = "PROBE_REQUIRED"
    else:
        worker_state = "WAITING"
    return {
        "gate": payload,
        "probeAllowed": bool(getattr(probe, "allowed", False)),
        "workerState": worker_state,
        "hardStop": hard,
        "availabilityState": state,
        "activeChallenges": challenges,
        "stateIntegrityOk": integrity,
        "cooldownUntil": (payload.get("cooldown") or {}).get("until") if isinstance(payload, dict) else None,
        "nextProbeAt": ((payload.get("availability") or {}) if isinstance(payload, dict) else {}).get(
            "nextProbeAt"
        ),
    }


def maybe_resume_transient_halt(pacing: OwnedDailyPacingController, gate_info: dict[str, Any]) -> bool:
    """Clear TEMPORARY_EBAY_SERVER_FAILURE halt once the control plane is not in cooldown."""
    if not pacing.state.browser_halted:
        return False
    if pacing.state.halt_reason not in {TEMPORARY_EBAY_SERVER_FAILURE, None}:
        if pacing.state.halt_reason in HARD_STOP_OUTCOMES:
            return False
    if gate_info.get("hardStop"):
        return False
    if gate_info.get("workerState") in {"SELECTING", "PROBE_REQUIRED"}:
        return pacing.clear_transient_ebay_halt() or (
            pacing.state.halt_reason == TEMPORARY_EBAY_SERVER_FAILURE
        )
    return False
