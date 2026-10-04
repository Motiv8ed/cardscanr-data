"""Rolling safety ceilings for continuous AU owned_daily (not throughput targets)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .owned_daily_enablement import BUDGET_LEDGER_PATH
from .scheduler import _parse_positive_int, utc_iso, utc_now


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
    max_submissions_per_day: int = 200
    max_consecutive_transient: int = 3
    max_transient_per_hour: int = 5
    submissions: list[datetime] = field(default_factory=list)
    transients: list[datetime] = field(default_factory=list)
    consecutive_transient: int = 0
    last_hard_stop: str | None = None

    @classmethod
    def from_env(cls, *, path: Path | None = None) -> "ContinuousSafetyBudget":
        budget = cls(
            max_submissions_per_hour=_parse_positive_int("MAX_LIVE_SUBMISSIONS_PER_HOUR", 20),
            max_submissions_per_day=_parse_positive_int("MAX_LIVE_SUBMISSIONS_PER_DAY", 200),
            max_consecutive_transient=_parse_positive_int(
                "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES", 3
            ),
            max_transient_per_hour=_parse_positive_int(
                "MAX_TRANSIENT_MARKETPLACE_FAILURES_PER_HOUR", 5
            ),
        )
        budget.load(path or BUDGET_LEDGER_PATH)
        return budget

    def load(self, path: Path) -> None:
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        self.submissions = [d for d in (_parse_dt(x) for x in data.get("submissions") or []) if d]
        self.transients = [d for d in (_parse_dt(x) for x in data.get("transients") or []) if d]
        self.consecutive_transient = int(data.get("consecutiveTransient") or 0)
        self.last_hard_stop = str(data.get("lastHardStop") or "") or None

    def persist(self, path: Path | None = None) -> None:
        target = path or BUDGET_LEDGER_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "submissions": [utc_iso(x) for x in self.submissions[-400:]],
            "transients": [utc_iso(x) for x in self.transients[-200:]],
            "consecutiveTransient": self.consecutive_transient,
            "lastHardStop": self.last_hard_stop,
            "updatedAtUtc": utc_iso(),
        }
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _prune(self, now: datetime) -> None:
        hour = now - timedelta(hours=1)
        day = now - timedelta(hours=24)
        self.submissions = [t for t in self.submissions if t >= day]
        self.transients = [t for t in self.transients if t >= hour]

    def submissions_1h(self, now: datetime | None = None) -> int:
        current = now or utc_now()
        hour = current - timedelta(hours=1)
        return sum(1 for t in self.submissions if t >= hour)

    def submissions_24h(self, now: datetime | None = None) -> int:
        current = now or utc_now()
        day = current - timedelta(hours=24)
        return sum(1 for t in self.submissions if t >= day)

    def transients_1h(self, now: datetime | None = None) -> int:
        current = now or utc_now()
        hour = current - timedelta(hours=1)
        return sum(1 for t in self.transients if t >= hour)

    def budget_exhausted(self, now: datetime | None = None) -> str | None:
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
        if self.consecutive_transient >= self.max_consecutive_transient:
            return "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES"
        if self.transients_1h(current) >= self.max_transient_per_hour:
            return "MAX_TRANSIENT_MARKETPLACE_FAILURES_PER_HOUR"
        return None

    def record_submission(self, now: datetime | None = None) -> None:
        current = now or utc_now()
        self.submissions.append(current)
        self._prune(current)

    def record_healthy(self) -> None:
        self.consecutive_transient = 0

    def record_transient(self, now: datetime | None = None) -> str | None:
        current = now or utc_now()
        self.transients.append(current)
        self.consecutive_transient += 1
        self._prune(current)
        reason = self.transient_hard_stop(current)
        if reason:
            self.last_hard_stop = reason
        return reason


# Imported by tests that monkeypatch env after import.
os.environ.setdefault("MAX_LIVE_SUBMISSIONS_PER_HOUR", "20")
