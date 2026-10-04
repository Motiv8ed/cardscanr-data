#!/usr/bin/env python3
"""Authoritative fail-closed gate for beginning eBay browser pricing work.

Combines availability, marketplace cooldown, active challenge incidents, and
state integrity into one decision. Callers must not authorise navigation by
inspecting availability HEALTHY alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .atomic_json_state import AtomicStateError
from .control_plane_incidents import (
    INCIDENT_TYPE_AUTH,
    INCIDENT_TYPE_CHALLENGE,
    list_active_incidents,
)
from .ebay_availability import (
    EBAY_AVAILABILITY_COOLDOWN,
    EBAY_AVAILABILITY_PROBE,
    EBAY_CHALLENGE_REQUIRED,
    EbayAvailabilitySnapshot,
    browser_work_allowed,
)
from .marketplace_ops_state import MarketplaceCooldownState, get_active_cooldown


@dataclass
class EbayBrowserWorkGateResult:
    allowed: bool
    reason_codes: list[str] = field(default_factory=list)
    market: str = "AU"
    availability_state: str | None = None
    active_challenge_count: int = 0
    cooldown: dict[str, Any] | None = None
    probe_in_flight: bool = False
    state_integrity_ok: bool = True
    for_probe: bool = False
    availability: dict[str, Any] | None = None
    active_challenge_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasonCodes": list(self.reason_codes),
            "market": self.market,
            "availabilityState": self.availability_state,
            "activeChallengeCount": self.active_challenge_count,
            "activeChallengeIds": list(self.active_challenge_ids),
            "cooldown": self.cooldown,
            "probeInFlight": self.probe_in_flight,
            "stateIntegrityOk": self.state_integrity_ok,
            "forProbe": self.for_probe,
            "availability": self.availability,
        }


def evaluate_ebay_browser_work_gate(
    *,
    market: str = "AU",
    now: datetime | None = None,
    for_probe: bool = False,
    availability_path: Path | None = None,
    ops_path: Path | None = None,
    incidents_path: Path | None = None,
    require_local_runtime_ready: bool = False,
    local_runtime_ready: bool | None = None,
) -> EbayBrowserWorkGateResult:
    """Single authoritative answer to MAY_EBAY_BROWSER_WORK_BEGIN for a market.

    Fail closed on unreadable state, active challenges, cooldown, probe conflicts,
    or (when requested) missing local browser runtime.
    """
    market_n = str(market or "AU").strip().upper() or "AU"
    reasons: list[str] = []
    result = EbayBrowserWorkGateResult(allowed=False, market=market_n, for_probe=for_probe)

    # 1) Availability + probe semantics (read/evaluate; do not mutate merely to answer).
    try:
        allowed_avail, avail_reason, snap = browser_work_allowed(
            now=now,
            path=availability_path,
            for_probe=for_probe,
            persist_transitions=False,
            market=market_n,
        )
        result.availability_state = snap.state
        result.probe_in_flight = bool(snap.probe_in_flight)
        result.availability = snap.to_dict()
        # browser_work_allowed soft-fails corrupt state as CHALLENGE_REQUIRED with
        # last_outcome marker — treat that as integrity failure, not a real challenge.
        if str(snap.last_outcome or "") == "availability_state_unreadable_or_locked":
            result.state_integrity_ok = False
            reasons.append("AVAILABILITY_STATE_UNREADABLE")
        elif not allowed_avail:
            reasons.append(str(avail_reason or "EBAY_AVAILABILITY_BLOCKED"))
            if snap.probe_in_flight and for_probe:
                reasons.append("EBAY_AVAILABILITY_PROBE_IN_FLIGHT")
    except AtomicStateError as exc:
        result.state_integrity_ok = False
        reasons.append(f"AVAILABILITY_STATE_UNREADABLE:{exc}")
        snap = None

    # 2) Marketplace cooldown
    try:
        cooldown = get_active_cooldown(market_n, now=now, path=ops_path)
        if cooldown is not None:
            result.cooldown = cooldown.to_dict()
            reasons.append(f"MARKETPLACE_COOLDOWN:{cooldown.reason}")
    except AtomicStateError as exc:
        result.state_integrity_ok = False
        reasons.append(f"OPS_STATE_UNREADABLE:{exc}")

    # 3) Active unresolved challenge/auth incidents (ledger) — cannot be bypassed by HEALTHY
    try:
        active = list_active_incidents(market=market_n, path=incidents_path)
        challenge_like = [
            row
            for row in active
            if str(row.get("incidentType") or "") in {INCIDENT_TYPE_CHALLENGE, INCIDENT_TYPE_AUTH}
            or str(row.get("classification") or "").upper()
            in {"CHALLENGE_REQUIRED", "AUTH_REQUIRED"}
        ]
        result.active_challenge_count = len(challenge_like)
        result.active_challenge_ids = [str(r.get("incidentId") or "") for r in challenge_like]
        if challenge_like:
            reasons.append(f"ACTIVE_CHALLENGE_INCIDENTS:{len(challenge_like)}")
    except AtomicStateError as exc:
        result.state_integrity_ok = False
        reasons.append(f"INCIDENT_LEDGER_UNREADABLE:{exc}")
    except Exception as exc:  # fail closed on unexpected ledger errors
        result.state_integrity_ok = False
        reasons.append(f"INCIDENT_LEDGER_ERROR:{type(exc).__name__}")

    # 4) Optional local runtime readiness (Xvfb/CDP) — independent of marketplace challenge
    if require_local_runtime_ready:
        if local_runtime_ready is not True:
            reasons.append("LOCAL_BROWSER_RUNTIME_NOT_READY")

    result.reason_codes = reasons
    result.allowed = len(reasons) == 0 and result.state_integrity_ok
    if result.allowed and snap is not None and for_probe and snap.state == "PROBE_REQUIRED":
        # Still allowed for probe; surface probe reason without denying.
        result.reason_codes = [EBAY_AVAILABILITY_PROBE]
    return result


def may_ebay_browser_work_begin(**kwargs: Any) -> bool:
    return evaluate_ebay_browser_work_gate(**kwargs).allowed


__all__ = [
    "EbayBrowserWorkGateResult",
    "evaluate_ebay_browser_work_gate",
    "may_ebay_browser_work_begin",
]
