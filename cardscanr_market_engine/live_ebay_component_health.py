"""Ensure-script / control-plane component health for live eBay pricing (local only)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cardscanr_market_engine.owned_daily_enablement import BUDGET_LEDGER_PATH, STATUS_PATH
from cardscanr_market_engine.pricing_worker_runtime_gate import (
    normalize_gate_state,
    runtime_file_evidence,
    worker_gate_blocks_pricing,
    worker_runtime_gate,
)


@dataclass(frozen=True)
class LiveEbayWorkerComponentHealth:
    """Distinct ensure-runtime status; never treat HARD_STOP or unreadable flags as healthy."""

    component: str
    process_present: bool
    process_id: int | None
    status: str
    gate_state: str | None
    gate_reason: str | None
    status_evidence: str | None
    ledger_evidence: str | None
    note: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "processPresent": self.process_present,
            "processId": self.process_id,
            "status": self.status,
            "gateState": self.gate_state,
            "gateReason": self.gate_reason,
            "statusEvidence": self.status_evidence,
            "ledgerEvidence": self.ledger_evidence,
            "note": self.note,
        }


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _gated_status_label(gate_state: str | None) -> str:
    normalized = normalize_gate_state(gate_state)
    if normalized == "HARD_STOP":
        return "stopped_hard_stop"
    if normalized:
        return f"gated_{normalized.lower()}"
    return "unknown_fail_closed"


def classify_live_ebay_worker_component_health(
    *,
    process_present: bool,
    process_id: int | None = None,
    status_path: Path | None = None,
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> LiveEbayWorkerComponentHealth:
    """Classify worker ensure health from local PID + runtime flag files (fail-closed)."""
    status_path = status_path or STATUS_PATH
    ledger_path = ledger_path or BUDGET_LEDGER_PATH
    current = now or datetime.now(timezone.utc)

    if not process_present:
        return LiveEbayWorkerComponentHealth(
            component="worker",
            process_present=False,
            process_id=None,
            status="no_process",
            gate_state=None,
            gate_reason=None,
            status_evidence=None,
            ledger_evidence=None,
            note=None,
        )

    status_payload = _read_json(status_path)
    ledger_payload = _read_json(ledger_path)
    status_evidence, _ = runtime_file_evidence(status_payload, path=status_path, now=current)
    ledger_evidence, _ = runtime_file_evidence(ledger_payload, path=ledger_path, now=current)

    unreadable = (
        status_evidence != "fresh"
        or ledger_evidence != "fresh"
        or status_payload is None
        or ledger_payload is None
    )
    if unreadable:
        return LiveEbayWorkerComponentHealth(
            component="worker",
            process_present=True,
            process_id=process_id,
            status="unknown_fail_closed",
            gate_state=None,
            gate_reason=None,
            status_evidence=status_evidence,
            ledger_evidence=ledger_evidence,
            note=(
                "Worker PID present but continuous_au_worker_status or continuous_au_safety_ledger "
                f"is not fresh (status={status_evidence}, ledger={ledger_evidence}); fail-closed."
            ),
        )

    gate_state, gate_reason = worker_runtime_gate(
        status_payload,
        ledger_payload,
        status_evidence=status_evidence,
        db_heartbeat_fresh=False,
    )
    if worker_gate_blocks_pricing(gate_state):
        return LiveEbayWorkerComponentHealth(
            component="worker",
            process_present=True,
            process_id=process_id,
            status=_gated_status_label(gate_state),
            gate_state=gate_state,
            gate_reason=gate_reason,
            status_evidence=status_evidence,
            ledger_evidence=ledger_evidence,
            note=(
                f"Worker process present but gate blocks pricing ({gate_state}"
                f"{': ' + gate_reason if gate_reason else ''})."
            ),
        )

    return LiveEbayWorkerComponentHealth(
        component="worker",
        process_present=True,
        process_id=process_id,
        status="healthy",
        gate_state=gate_state,
        gate_reason=gate_reason,
        status_evidence=status_evidence,
        ledger_evidence=ledger_evidence,
        note=None,
    )