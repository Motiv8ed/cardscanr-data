"""Rolling safety ceilings for continuous AU owned_daily (not throughput targets)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .atomic_json_state import (
    AtomicStateError,
    atomic_write_json,
    mutate_json_state,
    read_json_object,
)
from .owned_daily_enablement import BUDGET_LEDGER_PATH
from .scheduler import _parse_positive_int, utc_iso, utc_now

HARD_STOP_SOURCE_SET_SWEEP = "set_sweep"
HARD_STOP_SOURCE_WORKER = "worker"
SAFETY_LEDGER_FAIL_CLOSED = "SAFETY_LEDGER_FAIL_CLOSED"


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


@dataclass
class ContinuousSafetyBudget:
    max_submissions_per_hour: int = 20
    max_submissions_per_day: int = 60
    max_consecutive_transient: int = 3
    max_transient_per_hour: int = 5
    submissions: list[datetime] = field(default_factory=list)
    transients: list[datetime] = field(default_factory=list)
    consecutive_transient: int = 0
    last_hard_stop: str | None = None
    hard_stop_source: str | None = None
    market_submissions: list[tuple[datetime, str]] = field(default_factory=list)
    fail_closed: bool = False
    ledger_initialized: bool = False

    @classmethod
    def from_env(cls, *, path: Path | None = None) -> "ContinuousSafetyBudget":
        budget = cls(
            max_submissions_per_hour=_parse_positive_int("MAX_LIVE_SUBMISSIONS_PER_HOUR", 20),
            max_submissions_per_day=_parse_positive_int("MAX_LIVE_SUBMISSIONS_PER_DAY", 60),
            max_consecutive_transient=_parse_positive_int(
                "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES", 3
            ),
            max_transient_per_hour=_parse_positive_int(
                "MAX_TRANSIENT_MARKETPLACE_FAILURES_PER_HOUR", 5
            ),
        )
        target = path or BUDGET_LEDGER_PATH
        budget.load(target)
        return budget

    def to_payload(self) -> dict[str, Any]:
        self._reconcile_consecutive_with_history()
        payload: dict[str, Any] = {
            "submissions": [utc_iso(x) for x in self.submissions[-400:]],
            "transients": [utc_iso(x) for x in self.transients[-200:]],
            "consecutiveTransient": self.consecutive_transient,
            "lastHardStop": self.last_hard_stop,
            "hardStopSource": self.hard_stop_source,
            "marketSubmissions": [
                {"at": utc_iso(at), "market": market} for at, market in self.market_submissions[-400:]
            ],
            "updatedAtUtc": utc_iso(),
            "ledgerInitialized": True,
        }
        return payload

    def apply_payload(self, data: dict[str, Any]) -> None:
        self.submissions = [d for d in (_parse_dt(x) for x in data.get("submissions") or []) if d]
        self.transients = [d for d in (_parse_dt(x) for x in data.get("transients") or []) if d]
        self.consecutive_transient = int(data.get("consecutiveTransient") or 0)
        self.last_hard_stop = str(data.get("lastHardStop") or "") or None
        self.hard_stop_source = str(data.get("hardStopSource") or "") or None
        self.ledger_initialized = bool(data.get("ledgerInitialized"))
        tagged: list[tuple[datetime, str]] = []
        for row in data.get("marketSubmissions") or []:
            if isinstance(row, dict):
                parsed = _parse_dt(row.get("at"))
                market = str(row.get("market") or "").upper()
                if parsed is not None:
                    tagged.append((parsed, market))
        self.market_submissions = tagged
        self._reconcile_consecutive_with_history()

    def load(self, path: Path) -> None:
        if not path.is_file():
            return
        try:
            data = read_json_object(path, default={})
        except AtomicStateError:
            self.fail_closed = True
            self.ledger_initialized = True
            return
        self.apply_payload(data)

    def persist(self, path: Path | None = None) -> None:
        if self.fail_closed:
            return
        target = path or BUDGET_LEDGER_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_initialized = True
        atomic_write_json(target, self.to_payload())
        self.ledger_initialized = True

    def reload(self, path: Path | None = None) -> None:
        target = path or BUDGET_LEDGER_PATH
        was_initialized = self.ledger_initialized
        if was_initialized and not target.is_file():
            self.fail_closed = True
            return
        if not target.is_file():
            return
        self.load(target)

    def reserve_live_submission(
        self,
        *,
        path: Path | None = None,
        market: str | None = None,
        now: datetime | None = None,
    ) -> str | None:
        """Cross-process locked reservation: reload, check caps, record submission, persist."""
        target = path or BUDGET_LEDGER_PATH
        current = now or utc_now()
        blocked_reason: list[str] = []

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            self.apply_payload(data)
            if self.fail_closed:
                blocked_reason.append(SAFETY_LEDGER_FAIL_CLOSED)
                return data
            blocked = self.budget_exhausted(current)
            if blocked:
                blocked_reason.append(blocked)
                return data
            self.record_submission(now=current, market=market)
            data.clear()
            data.update(self.to_payload())
            return data

        try:
            mutate_json_state(target, _mutate, default={})
        except AtomicStateError:
            self.fail_closed = True
            return SAFETY_LEDGER_FAIL_CLOSED
        self.reload(target)
        return blocked_reason[0] if blocked_reason else None

    def _reconcile_consecutive_with_history(self) -> None:
        retained = len(self.transients)
        if self.consecutive_transient > retained:
            self.consecutive_transient = retained
        if self.consecutive_transient < 0:
            self.consecutive_transient = 0

    def _prune(self, now: datetime) -> None:
        hour = now - timedelta(hours=1)
        day = now - timedelta(hours=24)
        self.submissions = [t for t in self.submissions if t >= day]
        self.transients = [t for t in self.transients if t >= hour]
        self.market_submissions = [(t, m) for t, m in self.market_submissions if t >= day]
        self._reconcile_consecutive_with_history()

    def submissions_1h(self, now: datetime | None = None) -> int:
        current = now or utc_now()
        hour = current - timedelta(hours=1)
        return sum(1 for t in self.submissions if t >= hour)

    def submissions_24h(self, now: datetime | None = None) -> int:
        current = now or utc_now()
        day = current - timedelta(hours=24)
        return sum(1 for t in self.submissions if t >= day)

    def submissions_1h_for_market(self, market: str, now: datetime | None = None) -> int:
        current = now or utc_now()
        hour = current - timedelta(hours=1)
        code = str(market or "").upper()
        return sum(1 for t, m in self.market_submissions if t >= hour and m == code)

    def submissions_24h_for_market(self, market: str, now: datetime | None = None) -> int:
        current = now or utc_now()
        day = current - timedelta(hours=24)
        code = str(market or "").upper()
        return sum(1 for t, m in self.market_submissions if t >= day and m == code)

    def transients_1h(self, now: datetime | None = None) -> int:
        current = now or utc_now()
        hour = current - timedelta(hours=1)
        return sum(1 for t in self.transients if t >= hour)

    def budget_exhausted(self, now: datetime | None = None) -> str | None:
        if self.fail_closed:
            return SAFETY_LEDGER_FAIL_CLOSED
        current = now or utc_now()
        self._prune(current)
        if self.submissions_1h(current) >= self.max_submissions_per_hour:
            return "HOURLY_SUBMISSION_CEILING"
        if self.submissions_24h(current) >= self.max_submissions_per_day:
            return "DAILY_SUBMISSION_CEILING"
        return None

    def transient_hard_stop(self, now: datetime | None = None) -> str | None:
        current = now or utc_now()
        self._prune(current)
        if self.last_hard_stop:
            return self.last_hard_stop
        if self.consecutive_transient >= self.max_consecutive_transient:
            return "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES"
        if self.transients_1h(current) >= self.max_transient_per_hour:
            return "MAX_TRANSIENT_MARKETPLACE_FAILURES_PER_HOUR"
        return None

    def record_submission(self, now: datetime | None = None, *, market: str | None = None) -> None:
        current = now or utc_now()
        self.submissions.append(current)
        if market:
            self.market_submissions.append((current, str(market).upper()))
        self._prune(current)

    def record_healthy(self) -> None:
        """Owner-clear / healthy reset; never clears a set-sweep HARD_STOP latch."""
        if self.hard_stop_source == HARD_STOP_SOURCE_SET_SWEEP and self.last_hard_stop:
            self.consecutive_transient = 0
            self.transients = []
            return
        self.consecutive_transient = 0
        self.last_hard_stop = None
        self.hard_stop_source = None
        self.transients = []

    def record_transient(
        self,
        now: datetime | None = None,
        *,
        hard_stop_source: str | None = HARD_STOP_SOURCE_WORKER,
    ) -> str | None:
        current = now or utc_now()
        existing = self.transient_hard_stop(current)
        if existing:
            return existing
        self.transients.append(current)
        self.consecutive_transient += 1
        self._prune(current)
        reason: str | None = None
        if self.consecutive_transient >= self.max_consecutive_transient:
            reason = "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES"
        elif self.transients_1h(current) >= self.max_transient_per_hour:
            reason = "MAX_TRANSIENT_MARKETPLACE_FAILURES_PER_HOUR"
        if reason:
            self.last_hard_stop = reason
            self.hard_stop_source = hard_stop_source
        return reason

    def latch_hard_stop(
        self,
        reason: str,
        *,
        hard_stop_source: str = HARD_STOP_SOURCE_SET_SWEEP,
    ) -> str:
        self.last_hard_stop = str(reason)
        self.hard_stop_source = hard_stop_source
        return self.last_hard_stop


# Imported by tests that monkeypatch env after import.
os.environ.setdefault("MAX_LIVE_SUBMISSIONS_PER_HOUR", "20")