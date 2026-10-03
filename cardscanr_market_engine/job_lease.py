#!/usr/bin/env python3
"""Deterministic rules for market_price_refresh_jobs running-lease recovery.

A live running lease must never be stolen. Interrupted jobs (process death,
aborted pilot, circuit breaker halt, controlled shutdown) eventually become
stale and recoverable without marking the printing successfully refreshed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


DEFAULT_STALE_LOCK_MINUTES = 90
MIN_STALE_LOCK_MINUTES = 15


def parse_job_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def running_lease_anchor(*, locked_at: Any = None, started_at: Any = None) -> datetime | None:
    """Prefer locked_at; fall back to started_at for older rows."""
    return parse_job_timestamp(locked_at) or parse_job_timestamp(started_at)


def is_running_job_stale(
    *,
    status: str | None,
    locked_at: Any = None,
    started_at: Any = None,
    now: datetime | None = None,
    stale_after_minutes: int = DEFAULT_STALE_LOCK_MINUTES,
) -> bool:
    if str(status or "").strip().lower() != "running":
        return False
    minutes = max(MIN_STALE_LOCK_MINUTES, int(stale_after_minutes))
    current = now or datetime.now(timezone.utc)
    anchor = running_lease_anchor(locked_at=locked_at, started_at=started_at)
    if anchor is None:
        # No lease timestamps — treat as recoverable stale to unblock pilots,
        # but only after the minimum threshold window from epoch is meaningless;
        # callers should still prefer explicit timestamps. Conservative: stale.
        return True
    return anchor <= (current - timedelta(minutes=minutes))


def may_recover_running_lease(
    *,
    status: str | None,
    locked_at: Any = None,
    started_at: Any = None,
    now: datetime | None = None,
    stale_after_minutes: int = DEFAULT_STALE_LOCK_MINUTES,
) -> bool:
    """True only when a running job is past the stale threshold."""
    return is_running_job_stale(
        status=status,
        locked_at=locked_at,
        started_at=started_at,
        now=now,
        stale_after_minutes=stale_after_minutes,
    )


def may_steal_running_lease(
    *,
    status: str | None,
    locked_at: Any = None,
    started_at: Any = None,
    now: datetime | None = None,
    stale_after_minutes: int = DEFAULT_STALE_LOCK_MINUTES,
) -> bool:
    """Active (non-stale) running leases must never be stolen by another worker."""
    if str(status or "").strip().lower() != "running":
        return False
    return may_recover_running_lease(
        status=status,
        locked_at=locked_at,
        started_at=started_at,
        now=now,
        stale_after_minutes=stale_after_minutes,
    )


def recovery_action_for_running_job(
    *,
    status: str | None,
    locked_at: Any = None,
    started_at: Any = None,
    now: datetime | None = None,
    stale_after_minutes: int = DEFAULT_STALE_LOCK_MINUTES,
) -> str:
    """
    Returns:
      leave_running | fail_stale_running | ignore_non_running
    Failing a stale running job must leave the printing due (not completed).
    """
    if str(status or "").strip().lower() != "running":
        return "ignore_non_running"
    if may_recover_running_lease(
        status=status,
        locked_at=locked_at,
        started_at=started_at,
        now=now,
        stale_after_minutes=stale_after_minutes,
    ):
        return "fail_stale_running"
    return "leave_running"
