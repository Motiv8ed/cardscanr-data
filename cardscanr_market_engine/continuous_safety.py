"""Rolling safety ceilings for continuous AU owned_daily (not throughput targets)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .atomic_json_state import mutate_json_state
from .live_ebay_navigation_accounting import result_consumes_submission_cap
from .owned_daily_enablement import BUDGET_LEDGER_PATH
from .scheduler import _parse_positive_int, utc_iso, utc_now

_LEDGER_DEFAULT: dict[str, Any] = {
    "submissions": [],
    "transients": [],
    "consecutiveTransient": 0,
    "lastHardStop": None,
    "marketSubmissions": [],
}


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _parse_submissions(data: dict[str, Any]) -> list[datetime]:
    return [d for d in (_parse_dt(x) for x in data.get("submissions") or []) if d]


def _parse_market_submissions(data: dict[str, Any]) -> list[tuple[datetime, str]]:
    tagged: list[tuple[datetime, str]] = []
    for row in data.get("marketSubmissions") or []:
        if isinstance(row, dict):
            parsed = _parse_dt(row.get("at"))
            market = str(row.get("market") or "").upper()
            if parsed is not None:
                tagged.append((parsed, market))
    return tagged


def _prune_submission_lists(
    data: dict[str, Any],
    *,
    now: datetime,
) -> None:
    day = now - timedelta(hours=24)
    hour = now - timedelta(hours=1)
    subs = [t for t in _parse_submissions(data) if t >= day]
    data["submissions"] = [utc_iso(x) for x in subs[-400:]]
    trans = [d for d in (_parse_dt(x) for x in data.get("transients") or []) if d and d >= hour]
    data["transients"] = [utc_iso(x) for x in trans[-200:]]
    tagged = [(t, m) for t, m in _parse_market_submissions(data) if t >= day]
    data["marketSubmissions"] = [{"at": utc_iso(at), "market": market} for at, market in tagged[-400:]]


def _reconcile_consecutive(data: dict[str, Any]) -> None:
    retained = len(data.get("transients") or [])
    consecutive = int(data.get("consecutiveTransient") or 0)
    if consecutive > retained:
        data["consecutiveTransient"] = retained
    if consecutive < 0:
        data["consecutiveTransient"] = 0


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
    market_submissions: list[tuple[datetime, str]] = field(default_factory=list)

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
        budget.load(path or BUDGET_LEDGER_PATH)
        return budget

    def _apply_ledger_dict(self, data: dict[str, Any]) -> None:
        self.submissions = _parse_submissions(data)
        self.transients = [d for d in (_parse_dt(x) for x in data.get("transients") or []) if d]
        self.consecutive_transient = int(data.get("consecutiveTransient") or 0)
        self.last_hard_stop = str(data.get("lastHardStop") or "") or None
        self.market_submissions = _parse_market_submissions(data)
        self._reconcile_consecutive_with_history()

    def load(self, path: Path) -> None:
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        self._apply_ledger_dict(data)

    def _payload_from_state(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        self._reconcile_consecutive_with_history()
        return {
            "submissions": [utc_iso(x) for x in self.submissions[-400:]],
            "transients": [utc_iso(x) for x in self.transients[-200:]],
            "consecutiveTransient": self.consecutive_transient,
            "lastHardStop": self.last_hard_stop,
            "marketSubmissions": [
                {"at": utc_iso(at), "market": market} for at, market in self.market_submissions[-400:]
            ],
            "updatedAtUtc": utc_iso(now),
        }

    def persist(self, path: Path | None = None) -> None:
        target = Path(path or BUDGET_LEDGER_PATH)
        snapshot = self._payload_from_state()

        def mutator(data: dict[str, Any]) -> dict[str, Any]:
            if not isinstance(data, dict) or not data:
                data = dict(_LEDGER_DEFAULT)
            disk_subs = _parse_submissions(data)
            mem_subs = list(self.submissions)
            if len(mem_subs) >= len(disk_subs):
                merged_subs = mem_subs
            else:
                merged_subs = sorted(disk_subs + mem_subs)
            merged_market = list(_parse_market_submissions(data)) + list(self.market_submissions)
            data["submissions"] = [utc_iso(x) for x in merged_subs[-400:]]
            data["transients"] = snapshot["transients"]
            data["consecutiveTransient"] = snapshot["consecutiveTransient"]
            data["lastHardStop"] = snapshot["lastHardStop"]
            data["marketSubmissions"] = snapshot["marketSubmissions"]
            if len(merged_market) > len(_parse_market_submissions(data)):
                data["marketSubmissions"] = [
                    {"at": utc_iso(at), "market": market} for at, market in merged_market[-400:]
                ]
            _prune_submission_lists(data, now=utc_now())
            _reconcile_consecutive(data)
            data["updatedAtUtc"] = utc_iso()
            return data

        mutate_json_state(target, mutator, default=dict(_LEDGER_DEFAULT))
        self.load(target)

    def _reconcile_consecutive_with_history(self) -> None:
        retained = len(self.transients)
        if self.consecutive_transient > retained:
            self.consecutive_transient = retained
        if self.consecutive_transient < 0:
            self.consecutive_transient = 0

    def _prune(self, now: datetime) -> None:
        day = now - timedelta(hours=24)
        hour = now - timedelta(hours=1)
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

    def record_submission(
        self,
        now: datetime | None = None,
        *,
        market: str | None = None,
        path: Path | None = None,
    ) -> None:
        current = now or utc_now()
        target = Path(path or BUDGET_LEDGER_PATH)

        def mutator(data: dict[str, Any]) -> dict[str, Any]:
            if not isinstance(data, dict) or not data:
                data = dict(_LEDGER_DEFAULT)
            subs = _parse_submissions(data)
            subs.append(current)
            data["submissions"] = [utc_iso(x) for x in subs[-400:]]
            if market:
                tagged = _parse_market_submissions(data)
                tagged.append((current, str(market).upper()))
                data["marketSubmissions"] = [
                    {"at": utc_iso(at), "market": m} for at, m in tagged[-400:]
                ]
            _prune_submission_lists(data, now=current)
            data["updatedAtUtc"] = utc_iso(current)
            return data

        mutate_json_state(target, mutator, default=dict(_LEDGER_DEFAULT))
        self.load(target)

    def record_healthy(self) -> None:
        self.consecutive_transient = 0
        self.last_hard_stop = None
        self.transients = []

    def record_transient(self, now: datetime | None = None) -> str | None:
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
        return reason


# Re-export for worker / tests.
__all__ = [
    "ContinuousSafetyBudget",
    "result_consumes_submission_cap",
]

# Imported by tests that monkeypatch env after import.
os.environ.setdefault("MAX_LIVE_SUBMISSIONS_PER_HOUR", "20")
os.environ.setdefault("MAX_LIVE_SUBMISSIONS_PER_DAY", "60")