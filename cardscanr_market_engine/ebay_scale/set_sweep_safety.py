"""Set-sweep rate budget, circuit breaker, receipts, and marketplace failure classification."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..atomic_json_state import atomic_write_json
from ..config import REPORTS_DIR
from ..continuous_safety import HARD_STOP_SOURCE_SET_SWEEP
from ..continuous_worker_policy import HARD_STOP_OUTCOMES
from ..owned_daily_outcomes import (
    CHALLENGE_REQUIRED,
    EBAY_ACCESS_DENIED_403,
    EBAY_AUTH_REQUIRED,
    EBAY_RATE_LIMITED,
    TEMPORARY_EBAY_SERVER_FAILURE,
    TRANSIENT_EBAY_FAILURE_OUTCOMES,
    classify_exception_outcome,
)
from ..providers.errors import ProviderTemporaryError
from ..providers.linux_x11_gui_fsm import is_ebay_sorry_page
from ..providers.sold_page_health import (
    EBAY_ACCESS_DENIED_403 as PAGE_403,
    EBAY_AUTH_REQUIRED as PAGE_AUTH,
    EBAY_CHALLENGE_PAGE,
    EBAY_ERROR_PAGE,
    EBAY_SORRY_PAGE,
    classify_marketplace_page_class,
)
from ..scheduler import utc_iso

SET_SWEEP_ATTEMPTS_JSONL = REPORTS_DIR / "runtime" / "ebay_scale_set_sweep_attempts.jsonl"
SET_SWEEP_CIRCUIT_PATH = REPORTS_DIR / "runtime" / "ebay_scale_set_sweep_circuit.json"

MinFailureBackoffSecondsEnv = "EBAY_SCALE_SET_SWEEP_MIN_FAILURE_BACKOFF_SECONDS"
MaxFailureBackoffSecondsEnv = "EBAY_SCALE_SET_SWEEP_MAX_FAILURE_BACKOFF_SECONDS"
MinFailureBackoffFloorSeconds = 30.0
AttemptLogMaxLinesEnv = "EBAY_SCALE_SET_SWEEP_ATTEMPTS_MAX_LINES"
DefaultAttemptLogMaxLines = 4000


def _parse_positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.0, value)


def sweep_worker_gate_blocks(
    *,
    worker_state: str | None,
    availability_state: str | None = None,
    hard_stop: str | None = None,
) -> tuple[bool, str]:
    state = str(worker_state or "").upper()
    avail = str(availability_state or "").upper()
    if hard_stop:
        return True, "hard_stop"
    if state == "HARD_STOP" or avail == "HARD_STOP":
        return True, "worker_hard_stop"
    if state == "PROBE_REQUIRED" or avail == "PROBE_REQUIRED":
        return True, "probe_required"
    return False, "ok"


def sweep_outcome_counts_toward_transient_latch(outcome: str | None) -> bool:
    text = str(outcome or "").strip()
    if not text:
        return False
    if text in HARD_STOP_OUTCOMES or text in {CHALLENGE_REQUIRED, EBAY_AUTH_REQUIRED}:
        return False
    return text in TRANSIENT_EBAY_FAILURE_OUTCOMES


def sweep_immediate_hard_stop_outcome(outcome: str | None) -> str | None:
    text = str(outcome or "").strip()
    if not text:
        return None
    if text in HARD_STOP_OUTCOMES or text in {CHALLENGE_REQUIRED, EBAY_AUTH_REQUIRED}:
        return text
    if text == EBAY_ACCESS_DENIED_403:
        return text
    return None


def classify_set_sweep_html(html: str) -> str | None:
    body = str(html or "")
    lowered = body.lower()
    if "http 429" in lowered or "status code 429" in lowered or "too many requests" in lowered:
        return EBAY_RATE_LIMITED
    page_class = classify_marketplace_page_class(url=None, title=None, body=body)
    if page_class in {EBAY_SORRY_PAGE, EBAY_ERROR_PAGE} or is_ebay_sorry_page(body=body):
        return TEMPORARY_EBAY_SERVER_FAILURE
    if page_class == PAGE_403:
        return EBAY_ACCESS_DENIED_403
    if page_class == PAGE_AUTH:
        return EBAY_AUTH_REQUIRED
    if page_class == EBAY_CHALLENGE_PAGE:
        return CHALLENGE_REQUIRED
    return None


def classify_set_sweep_fetch_failure(exc: BaseException) -> str:
    diagnostics: dict[str, Any] = {}
    if isinstance(exc, ProviderTemporaryError):
        raw_diag = getattr(exc, "diagnostics", None)
        if isinstance(raw_diag, dict):
            diagnostics = raw_diag
    return classify_exception_outcome(exc, diagnostics=diagnostics)


@dataclass
class SetSweepCircuit:
    consecutive_failures: int = 0
    next_attempt_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_backoff_seconds: float = 0.0

    def backoff_active(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        if self.next_attempt_at is None:
            return False
        return current < self.next_attempt_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "consecutiveFailures": self.consecutive_failures,
            "nextAttemptAtUtc": utc_iso(self.next_attempt_at) if self.next_attempt_at else None,
            "lastFailureAtUtc": utc_iso(self.last_failure_at) if self.last_failure_at else None,
            "lastBackoffSeconds": self.last_backoff_seconds,
            "updatedAtUtc": utc_iso(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SetSweepCircuit":
        if not isinstance(data, dict):
            return cls()
        next_raw = data.get("nextAttemptAtUtc")
        last_raw = data.get("lastFailureAtUtc")
        next_at = _parse_dt(next_raw)
        last_at = _parse_dt(last_raw)
        return cls(
            consecutive_failures=int(data.get("consecutiveFailures") or 0),
            next_attempt_at=next_at,
            last_failure_at=last_at,
            last_backoff_seconds=float(data.get("lastBackoffSeconds") or 0.0),
        )


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def load_set_sweep_circuit(path: Path | None = None) -> SetSweepCircuit:
    target = path or SET_SWEEP_CIRCUIT_PATH
    if not target.is_file():
        return SetSweepCircuit()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return SetSweepCircuit()
    return SetSweepCircuit.from_dict(data if isinstance(data, dict) else {})


def persist_set_sweep_circuit(circuit: SetSweepCircuit, path: Path | None = None) -> None:
    target = path or SET_SWEEP_CIRCUIT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, circuit.to_dict())


def compute_failure_backoff_seconds(consecutive_failures: int) -> float:
    floor = max(
        MinFailureBackoffFloorSeconds,
        _parse_positive_float(MinFailureBackoffSecondsEnv, 60.0),
    )
    ceiling = _parse_positive_float(MaxFailureBackoffSecondsEnv, 900.0)
    ceiling = max(ceiling, floor)
    if consecutive_failures <= 0:
        return floor
    scaled = floor * (2 ** min(consecutive_failures - 1, 4))
    return min(ceiling, max(floor, scaled))


def record_set_sweep_circuit_failure(
    circuit: SetSweepCircuit,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> SetSweepCircuit:
    current = now or datetime.now(timezone.utc)
    circuit.consecutive_failures += 1
    circuit.last_failure_at = current
    circuit.last_backoff_seconds = compute_failure_backoff_seconds(circuit.consecutive_failures)
    circuit.next_attempt_at = current + timedelta(seconds=circuit.last_backoff_seconds)
    persist_set_sweep_circuit(circuit, path=path)
    return circuit


def record_set_sweep_circuit_success(
    circuit: SetSweepCircuit,
    *,
    path: Path | None = None,
) -> SetSweepCircuit:
    circuit.consecutive_failures = 0
    circuit.next_attempt_at = None
    circuit.last_backoff_seconds = 0.0
    persist_set_sweep_circuit(circuit, path=path)
    return circuit


def _rotate_attempts_log(path: Path, max_lines: int) -> None:
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= max_lines:
        return
    tail = lines[-max_lines:]
    tmp = path.with_suffix(path.suffix + ".rotate.tmp")
    tmp.write_text("\n".join(tail) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_set_sweep_attempt_receipt(
    payload: dict[str, Any],
    *,
    path: Path | None = None,
) -> None:
    target = path or SET_SWEEP_ATTEMPTS_JSONL
    target.parent.mkdir(parents=True, exist_ok=True)
    row = dict(payload)
    row.setdefault("recordedAtUtc", utc_iso())
    max_lines = int(_parse_positive_float(AttemptLogMaxLinesEnv, float(DefaultAttemptLogMaxLines)))
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    _rotate_attempts_log(target, max_lines=max_lines)


def handle_set_sweep_marketplace_outcome(
    outcome: str,
    *,
    budget: Any,
    now: datetime | None = None,
) -> str | None:
    """Apply transient/HARD_STOP latch to continuous safety budget; return latched reason if any."""
    immediate = sweep_immediate_hard_stop_outcome(outcome)
    if immediate:
        return budget.latch_hard_stop(immediate, hard_stop_source=HARD_STOP_SOURCE_SET_SWEEP)
    if sweep_outcome_counts_toward_transient_latch(outcome):
        return budget.record_transient(now, hard_stop_source=HARD_STOP_SOURCE_SET_SWEEP)
    return None