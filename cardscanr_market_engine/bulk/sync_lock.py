"""Cross-process lock for bulk reference sync overlap prevention."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import REPORTS_DIR

LOCK_PATH = REPORTS_DIR / "runtime" / "bulk_reference_sync.lock.json"
DEFAULT_STALE_MINUTES = 90


@dataclass(frozen=True)
class BulkSyncLock:
    acquired: bool
    reason: str
    holder_pid: int | None = None
    started_at_utc: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_lock() -> dict | None:
    if not LOCK_PATH.is_file():
        return None
    try:
        payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_stale(payload: dict, *, stale_minutes: int) -> bool:
    started_raw = payload.get("startedAtUtc")
    if not started_raw:
        return True
    try:
        started = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    return started < (_utc_now() - timedelta(minutes=max(5, stale_minutes)))


def acquire_bulk_sync_lock(*, stale_minutes: int = DEFAULT_STALE_MINUTES) -> BulkSyncLock:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_lock()
    if existing and not _is_stale(existing, stale_minutes=stale_minutes):
        return BulkSyncLock(
            acquired=False,
            reason="lock_held",
            holder_pid=int(existing.get("pid") or 0) or None,
            started_at_utc=str(existing.get("startedAtUtc") or "") or None,
        )
    payload = {
        "pid": os.getpid(),
        "startedAtUtc": _utc_now().isoformat().replace("+00:00", "Z"),
        "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "unknown",
    }
    LOCK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return BulkSyncLock(acquired=True, reason="acquired", holder_pid=os.getpid(), started_at_utc=payload["startedAtUtc"])


def release_bulk_sync_lock() -> None:
    if LOCK_PATH.is_file():
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass
