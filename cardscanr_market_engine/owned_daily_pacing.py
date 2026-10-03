"""Owned-daily eBay market-check pacing (load throttling, not challenge bypass).

Completes one market check → wait → begin next. No parallel browsing.
Adaptive backoff lengthens cooldown after temporary browser failures; challenges
halt further browser work until human restoration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any

from .owned_daily_outcomes import (
    CHALLENGE_REQUIRED,
    CHECKED_NO_NEW_EXACT_EVIDENCE,
    EBAY_CHALLENGE_REQUIRED,
    HEALTHY_CHECK_OUTCOMES,
    NO_PRICE_EVER_FOUND,
    TEMPORARY_BROWSER_FAILURE,
    TEMPORARY_EBAY_SERVER_FAILURE,
    TRANSIENT_EBAY_FAILURE_OUTCOMES,
    UNCHANGED_FROM_EBAY,
    UPDATED_FROM_EBAY,
)
from .scheduler import _parse_non_negative_int, _parse_positive_int


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class OwnedDailyPacingConfig:
    """Conservative defaults from failure-timing + paced probe adjustment.

    Probe-10 (60/120/240) still saw PRE_SOLD streaks; one-shot tighten applied:
    longer normal cool, stronger failure backoff, more frequent session rest.
    """

    min_inter_job_delay_seconds: int = 90
    max_inter_job_delay_seconds: int = 360
    failure_base_delay_seconds: int = 180
    session_rest_every_n_checks: int = 3
    session_rest_seconds: int = 180
    operating_window_hours: int = 16
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "OwnedDailyPacingConfig":
        min_delay = _parse_positive_int("OWNED_DAILY_MIN_INTER_JOB_DELAY_SECONDS", 90)
        max_delay = _parse_positive_int("OWNED_DAILY_MAX_INTER_JOB_DELAY_SECONDS", 360)
        if max_delay < min_delay:
            max_delay = min_delay
        failure_base = _parse_positive_int("OWNED_DAILY_FAILURE_BACKOFF_SECONDS", 180)
        failure_base = max(min_delay, min(failure_base, max_delay))
        return cls(
            min_inter_job_delay_seconds=min_delay,
            max_inter_job_delay_seconds=max_delay,
            failure_base_delay_seconds=failure_base,
            session_rest_every_n_checks=_parse_positive_int("OWNED_DAILY_SESSION_REST_EVERY_N", 3),
            session_rest_seconds=_parse_non_negative_int("OWNED_DAILY_SESSION_REST_SECONDS", 180),
            operating_window_hours=_parse_positive_int("OWNED_DAILY_OPERATING_WINDOW_HOURS", 16),
            enabled=_parse_bool("OWNED_DAILY_PACING_ENABLED", True),
        )


@dataclass
class OwnedDailyPacingState:
    consecutive_browser_failures: int = 0
    checks_since_rest: int = 0
    checks_completed: int = 0
    estimates_updated: int = 0
    estimates_unchanged: int = 0
    no_new_evidence: int = 0
    browser_failures: int = 0
    challenges: int = 0
    last_good_retained: int = 0
    total_check_seconds: float = 0.0
    total_cooldown_seconds: float = 0.0
    cooldown_events: int = 0
    browser_halted: bool = False
    halt_reason: str | None = None
    last_outcome: str | None = None
    last_delay_seconds: int = 0
    delays_imposed: list[int] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        avg_check = (
            self.total_check_seconds / self.checks_completed if self.checks_completed else None
        )
        avg_cool = (
            self.total_cooldown_seconds / self.cooldown_events if self.cooldown_events else None
        )
        avg_total = None
        if avg_check is not None:
            avg_total = float(avg_check) + float(avg_cool or 0.0)
        checks_per_hour = (3600.0 / avg_total) if avg_total and avg_total > 0 else None
        return {
            "consecutive_browser_failures": self.consecutive_browser_failures,
            "checks_since_rest": self.checks_since_rest,
            "checks_completed": self.checks_completed,
            "estimates_updated": self.estimates_updated,
            "estimates_unchanged": self.estimates_unchanged,
            "no_new_evidence": self.no_new_evidence,
            "browser_failures": self.browser_failures,
            "challenges": self.challenges,
            "last_good_retained": self.last_good_retained,
            "browser_halted": self.browser_halted,
            "halt_reason": self.halt_reason,
            "last_outcome": self.last_outcome,
            "last_delay_seconds": self.last_delay_seconds,
            "average_check_seconds": round(avg_check, 1) if avg_check is not None else None,
            "average_cooldown_seconds": round(avg_cool, 1) if avg_cool is not None else None,
            "average_total_seconds_per_job": round(avg_total, 1) if avg_total is not None else None,
            "effective_checks_per_hour": round(checks_per_hour, 2) if checks_per_hour else None,
            "delays_imposed_count": len(self.delays_imposed),
        }


class OwnedDailyPacingController:
    """Stateful inter-job delay calculator for sequential eBay market checks."""

    def __init__(self, config: OwnedDailyPacingConfig | None = None) -> None:
        self.config = config or OwnedDailyPacingConfig.from_env()
        self.state = OwnedDailyPacingState()

    def record_check_duration(self, seconds: float) -> None:
        if seconds > 0:
            self.state.total_check_seconds += float(seconds)
            self.state.checks_completed += 1

    def observe_outcome(self, outcome: str | None, *, last_good_retained: bool | None = None) -> None:
        text = str(outcome or "").strip()
        self.state.last_outcome = text or None
        # Fresh/noop skips did not hit eBay; do not advance failure streak or session rest.
        if text in {"already_fresh_noop", "owned_daily_fresh_noop"} or text.endswith("_noop"):
            return
        if text == UPDATED_FROM_EBAY:
            self.state.estimates_updated += 1
            self.state.consecutive_browser_failures = 0
        elif text == UNCHANGED_FROM_EBAY:
            self.state.estimates_unchanged += 1
            self.state.consecutive_browser_failures = 0
        elif text == CHECKED_NO_NEW_EXACT_EVIDENCE:
            self.state.no_new_evidence += 1
            self.state.consecutive_browser_failures = 0
            self.state.last_good_retained += 1
        elif text == NO_PRICE_EVER_FOUND:
            # Market check completed; sparse/unpriced is not a browser failure.
            self.state.consecutive_browser_failures = 0
        elif text in TRANSIENT_EBAY_FAILURE_OUTCOMES or text == TEMPORARY_BROWSER_FAILURE:
            self.state.browser_failures += 1
            self.state.consecutive_browser_failures += 1
            self.state.last_good_retained += 1
            if text == TEMPORARY_EBAY_SERVER_FAILURE:
                # Stop active browser batch; resume only after cooldown (pilot/scheduler).
                self.state.browser_halted = True
                self.state.halt_reason = TEMPORARY_EBAY_SERVER_FAILURE
        elif text in {CHALLENGE_REQUIRED, EBAY_CHALLENGE_REQUIRED}:
            self.state.challenges += 1
            self.state.last_good_retained += 1
            self.state.browser_halted = True
            self.state.halt_reason = CHALLENGE_REQUIRED
            self.state.consecutive_browser_failures += 1
        elif text in HEALTHY_CHECK_OUTCOMES:
            self.state.consecutive_browser_failures = 0
        elif last_good_retained:
            self.state.last_good_retained += 1

        self.state.checks_since_rest += 1

    def clear_transient_ebay_halt(self) -> bool:
        """Allow resume after TEMPORARY_EBAY_SERVER_FAILURE batch cooldown (not challenge)."""
        if self.state.halt_reason == TEMPORARY_EBAY_SERVER_FAILURE and self.state.browser_halted:
            self.state.browser_halted = False
            self.state.halt_reason = None
            return True
        return False

    def next_delay_seconds(self, *, more_jobs_pending: bool = True) -> int:
        """Return seconds to wait before the next market check (0 if none / halted / disabled)."""
        if not more_jobs_pending:
            return 0
        if self.state.browser_halted:
            return 0
        if not self.config.enabled:
            return 0

        cfg = self.config
        last = str(self.state.last_outcome or "")
        is_noop = last in {"already_fresh_noop", "owned_daily_fresh_noop"} or last.endswith("_noop")
        # Noop alone: brief settle. If a prior browser failure is still open, keep that backoff.
        if is_noop and self.state.consecutive_browser_failures <= 0:
            delay = min(15, cfg.min_inter_job_delay_seconds)
            self.state.last_delay_seconds = delay
            self.state.delays_imposed.append(delay)
            self.state.total_cooldown_seconds += delay
            self.state.cooldown_events += 1
            return delay

        if self.state.consecutive_browser_failures > 0:
            # Progressive bounded backoff: base, 1.5x, 2x … capped at max.
            step = self.state.consecutive_browser_failures - 1
            delay = int(cfg.failure_base_delay_seconds * (1.0 + 0.5 * step))
            delay = max(cfg.min_inter_job_delay_seconds, min(delay, cfg.max_inter_job_delay_seconds))
        else:
            delay = cfg.min_inter_job_delay_seconds

        # Periodic session rest after a small batch of attempts (success or fail).
        if (
            cfg.session_rest_seconds > 0
            and cfg.session_rest_every_n_checks > 0
            and self.state.checks_since_rest >= cfg.session_rest_every_n_checks
        ):
            delay = max(delay, cfg.session_rest_seconds + cfg.min_inter_job_delay_seconds // 2)
            self.state.checks_since_rest = 0

        self.state.last_delay_seconds = delay
        self.state.delays_imposed.append(delay)
        self.state.total_cooldown_seconds += delay
        self.state.cooldown_events += 1
        return delay

    def capacity_projection(
        self,
        *,
        expected_daily_workload: int,
        average_check_seconds: float | None = None,
    ) -> dict[str, Any]:
        snap = self.state.snapshot()
        avg_check = average_check_seconds
        if avg_check is None:
            avg_check = snap.get("average_check_seconds")
        avg_cool = snap.get("average_cooldown_seconds")
        if avg_cool is None:
            avg_cool = float(self.config.min_inter_job_delay_seconds)
        if avg_check is None:
            avg_check = 150.0
        total = float(avg_check) + float(avg_cool)
        per_hour = 3600.0 / total if total > 0 else 0.0
        window = int(self.config.operating_window_hours)
        daily = per_hour * window
        expected = max(0, int(expected_daily_workload))
        utilisation = (expected / daily) if daily > 0 else None
        margin = daily - expected
        if daily <= 0:
            status = "UNKNOWN"
        elif margin < 0:
            status = "INSUFFICIENT"
        elif utilisation is not None and utilisation > 0.85:
            status = "TIGHT"
        else:
            status = "SUFFICIENT"
        return {
            "average_check_seconds": round(float(avg_check), 1),
            "average_cooldown_seconds": round(float(avg_cool), 1),
            "average_total_seconds_per_job": round(total, 1),
            "effective_checks_per_hour": round(per_hour, 2),
            "operating_window_hours": window,
            "safe_daily_capacity": int(daily),
            "expected_daily_workload": expected,
            "utilisation": round(utilisation, 3) if utilisation is not None else None,
            "capacity_margin": int(margin),
            "capacity_status": status,
        }

    def observability_payload(
        self,
        *,
        browser_mode: str,
        expected_daily_workload: int,
    ) -> dict[str, Any]:
        snap = self.state.snapshot()
        cap = self.capacity_projection(expected_daily_workload=expected_daily_workload)
        return {
            "browser_mode": browser_mode,
            "inter_job_delay_seconds": self.config.min_inter_job_delay_seconds,
            "max_inter_job_delay_seconds": self.config.max_inter_job_delay_seconds,
            "failure_backoff_seconds": self.config.failure_base_delay_seconds,
            "session_rest_every_n_checks": self.config.session_rest_every_n_checks,
            "session_rest_seconds": self.config.session_rest_seconds,
            "backoff_state": {
                "consecutive_browser_failures": self.state.consecutive_browser_failures,
                "browser_halted": self.state.browser_halted,
                "halt_reason": self.state.halt_reason,
                "last_delay_seconds": self.state.last_delay_seconds,
            },
            "checks_completed_today": snap["checks_completed"],
            "estimates_updated_today": snap["estimates_updated"],
            "estimates_unchanged_today": snap["estimates_unchanged"],
            "no_new_evidence_today": snap["no_new_evidence"],
            "browser_failures_today": snap["browser_failures"],
            "challenges_today": snap["challenges"],
            "last_good_retained_today": snap["last_good_retained"],
            "average_check_seconds": snap["average_check_seconds"],
            "average_total_seconds_per_job": cap["average_total_seconds_per_job"],
            "effective_checks_per_hour": cap["effective_checks_per_hour"],
            "safe_daily_capacity": cap["safe_daily_capacity"],
            "expected_daily_workload": cap["expected_daily_workload"],
            "utilisation": cap["utilisation"],
            "capacity_status": cap["capacity_status"],
            "pacing_enabled": self.config.enabled,
        }
