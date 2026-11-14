"""Local runtime gate parsing for live eBay pricing workers (no network)."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from cardscanr_market_engine.marketplace_ops_state import parse_utc

RUNTIME_EVIDENCE_STALE_MINUTES = int(os.getenv("MARKET_HEALTH_RUNTIME_STALE_MINUTES", "10"))
RUNTIME_EVIDENCE_FUTURE_TOLERANCE_MINUTES = float(
    os.getenv("MARKET_HEALTH_RUNTIME_FUTURE_TOLERANCE_MINUTES", "2")
)

WORKER_GATE_STATES_BLOCKING = frozenset(
    {
        "HARD_STOP",
        "BUDGET_WAIT",
        "COOLDOWN",
        "TRANSIENT_HALT",
        "BROWSER_HALTED",
        "PROBE_REQUIRED",
        "NOT_RUNNING",
    }
)


def normalize_gate_state(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text.upper().replace("-", "_")


def worker_gate_blocks_pricing(gate_state: str | None) -> bool:
    normalized = normalize_gate_state(gate_state)
    return normalized in WORKER_GATE_STATES_BLOCKING if normalized else False


def runtime_file_evidence(
    payload: dict | None,
    *,
    path: Path,
    now: datetime,
) -> tuple[str, float | None]:
    """Return freshness label (fresh|stale|missing) and age in minutes when known."""
    if payload is None:
        if not path.exists():
            return "missing", None
        return "missing", None
    updated = parse_utc(payload.get("updatedAtUtc"))
    if updated is None:
        return "stale", None
    age_minutes = (now - updated).total_seconds() / 60.0
    if age_minutes < -RUNTIME_EVIDENCE_FUTURE_TOLERANCE_MINUTES:
        return "stale", round(age_minutes, 1)
    if age_minutes > RUNTIME_EVIDENCE_STALE_MINUTES:
        return "stale", round(age_minutes, 1)
    return "fresh", round(age_minutes, 1)


def worker_runtime_gate(
    status: dict | None,
    ledger: dict | None,
    *,
    status_evidence: str | None = None,
    db_heartbeat_fresh: bool = False,
) -> tuple[str | None, str | None]:
    """Fail-safe latch / gate state from local runtime status files (no Supabase)."""
    gate_state = None
    gate_reason = None
    ledger_stop = None
    ledger_present = ledger is not None
    if ledger is not None:
        ledger_stop = str(ledger.get("lastHardStop") or "").strip() or None

    if status:
        gate_state = str(status.get("workerState") or "").strip() or None
        if normalize_gate_state(gate_state) == "HARD_STOP":
            gate_reason = str(status.get("lastHardStopReason") or "").strip() or None

    if ledger_stop:
        gate_state = "HARD_STOP"
        gate_reason = ledger_stop
    elif ledger_present and normalize_gate_state(gate_state) == "HARD_STOP":
        gate_state = None
        gate_reason = None
        fresh_runtime = status_evidence == "fresh"
        if status:
            gate_state = str(status.get("workerState") or "").strip() or None
            if normalize_gate_state(gate_state) == "HARD_STOP":
                if fresh_runtime or db_heartbeat_fresh:
                    gate_state = "SELECTING"
                else:
                    gate_state = None
                    gate_reason = "STALE_RUNTIME_AFTER_LATCH_CLEAR"

    if normalize_gate_state(gate_state) == "BUDGET_WAIT" and not gate_reason:
        gate_reason = "BUDGET_WAIT"
    elif normalize_gate_state(gate_state) == "COOLDOWN":
        if not gate_reason and status:
            gate_reason = str(status.get("cooldownUntil") or "COOLDOWN").strip() or "COOLDOWN"
    elif normalize_gate_state(gate_state) == "HARD_STOP" and not gate_reason:
        gate_reason = "UNKNOWN_HARD_STOP"
    return gate_state, gate_reason