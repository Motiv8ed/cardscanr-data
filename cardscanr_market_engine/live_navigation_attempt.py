"""Authoritative live eBay search-submission attempt accounting.

A live navigation is consumed only when SEARCH_SUBMISSION_STARTED is durably
emitted immediately before the keyboard/search-submit event. Failures before
that event do not consume an authorised attempt.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# Single production authority for SEARCH_SUBMISSION_STARTED across Windows + WSL.
DEFAULT_EVENTS_DIR = ROOT / "reports" / "runtime" / "live_nav_attempts"
SEARCH_SUBMISSION_STARTED = "SEARCH_SUBMISSION_STARTED"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_attempts_dir() -> Path:
    """Production canonical attempts directory (Windows path)."""
    path = DEFAULT_EVENTS_DIR.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def attempts_dir() -> Path:
    """Resolved attempts directory.

    Production reliability accounting MUST use the canonical default directory.
    ``CARDSCANR_LIVE_NAV_ATTEMPTS_DIR`` is reserved for isolated unit tests only
    and, when set, MUST also be propagated into WSL via ``attempts_dir_wsl()``.
    """
    override = (os.getenv("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR") or "").strip()
    path = Path(override).resolve() if override else canonical_attempts_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def attempts_dir_wsl() -> str:
    """WSL /mnt/... path for the same physical attempts directory."""
    from .wsl_path import windows_to_wsl_path

    return windows_to_wsl_path(attempts_dir())


def new_attempt_id() -> str:
    return str(uuid.uuid4())


def query_fingerprint(query: str) -> str:
    return hashlib.sha256((query or "").encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SearchSubmissionEvent:
    event: str
    attempt_id: str
    timestamp: str
    query_fingerprint: str
    query: str | None
    pid: int
    price_key_id: str | None
    path: str
    market: str | None = None
    currency: str | None = None
    fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "attemptId": self.attempt_id,
            "timestamp": self.timestamp,
            "queryFingerprint": self.query_fingerprint,
            "query": self.query,
            "pid": self.pid,
            "priceKeyId": self.price_key_id,
            "path": self.path,
            "market": self.market,
            "currency": self.currency,
            "fingerprint": self.fingerprint,
        }


def event_path(attempt_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in attempt_id)
    return attempts_dir() / f"{safe}.SEARCH_SUBMISSION_STARTED.json"


def has_search_submission_started(attempt_id: str) -> bool:
    if not attempt_id:
        return False
    return event_path(attempt_id).is_file()


def read_search_submission_event(attempt_id: str) -> dict[str, Any] | None:
    path = event_path(attempt_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def lookup_search_submission_event(
    attempt_id: str,
    *,
    expected_price_key_id: str | None = None,
) -> dict[str, Any]:
    """Lookup consumption by exact attemptId in the canonical attempts directory.

    Does not invent events. Validates event type + attemptId (+ optional priceKeyId).
    """
    aid = str(attempt_id or "").strip()
    path = event_path(aid) if aid else None
    out: dict[str, Any] = {
        "attemptId": aid or None,
        "found": False,
        "valid": False,
        "path": str(path) if path else None,
        "attemptsDir": str(attempts_dir()),
        "attemptsDirWsl": attempts_dir_wsl(),
        "event": None,
        "rejectionReasons": [],
    }
    if not aid or path is None:
        out["rejectionReasons"].append("attempt_id_missing")
        return out
    if not path.is_file():
        out["rejectionReasons"].append("event_file_missing")
        return out
    payload = read_search_submission_event(aid)
    out["found"] = payload is not None
    out["event"] = payload
    if not isinstance(payload, dict):
        out["rejectionReasons"].append("event_unreadable")
        return out
    if str(payload.get("event") or "") != SEARCH_SUBMISSION_STARTED:
        out["rejectionReasons"].append("event_type_mismatch")
        return out
    if str(payload.get("attemptId") or "").strip() != aid:
        out["rejectionReasons"].append("attemptId_mismatch")
        return out
    if expected_price_key_id:
        ev_pk = str(payload.get("priceKeyId") or "").strip()
        if ev_pk and ev_pk != str(expected_price_key_id).strip():
            out["rejectionReasons"].append("priceKeyId_mismatch")
            return out
    out["valid"] = True
    return out


def emit_search_submission_started(
    *,
    attempt_id: str,
    query: str,
    price_key_id: str | None = None,
    include_query_text: bool = True,
    market: str | None = None,
    currency: str | None = None,
    fingerprint: str | None = None,
) -> SearchSubmissionEvent:
    """Atomically record SEARCH_SUBMISSION_STARTED for attempt_id (once).

    If the event already exists for this attemptId, the existing event is returned
    and no second submission is recorded (duplicate-safe).
    """
    if not attempt_id or not str(attempt_id).strip():
        raise ValueError("attempt_id_required")
    path = event_path(attempt_id)
    if path.is_file():
        existing = read_search_submission_event(attempt_id) or {}
        return SearchSubmissionEvent(
            event=SEARCH_SUBMISSION_STARTED,
            attempt_id=attempt_id,
            timestamp=str(existing.get("timestamp") or ""),
            query_fingerprint=str(existing.get("queryFingerprint") or query_fingerprint(query)),
            query=existing.get("query"),
            pid=int(existing.get("pid") or os.getpid()),
            price_key_id=existing.get("priceKeyId"),
            path=str(path),
            market=existing.get("market"),
            currency=existing.get("currency"),
            fingerprint=existing.get("fingerprint"),
        )

    payload = {
        "event": SEARCH_SUBMISSION_STARTED,
        "attemptId": attempt_id,
        "timestamp": _utc(),
        "queryFingerprint": query_fingerprint(query),
        "query": (query if include_query_text else None),
        "pid": os.getpid(),
        "priceKeyId": price_key_id,
        "market": market,
        "currency": currency,
        "fingerprint": fingerprint,
        "monotonic": time.monotonic(),
    }
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return SearchSubmissionEvent(
        event=SEARCH_SUBMISSION_STARTED,
        attempt_id=attempt_id,
        timestamp=str(payload["timestamp"]),
        query_fingerprint=str(payload["queryFingerprint"]),
        query=payload.get("query"),
        pid=int(payload["pid"]),
        price_key_id=price_key_id,
        path=str(path),
        market=market,
        currency=currency,
        fingerprint=fingerprint,
    )


def count_consumed_live_navigations(attempt_ids: list[str]) -> int:
    """Count unique attemptIds that have SEARCH_SUBMISSION_STARTED (no double-count)."""
    seen: set[str] = set()
    for attempt_id in attempt_ids:
        aid = str(attempt_id or "").strip()
        if not aid or aid in seen:
            continue
        if has_search_submission_started(aid):
            seen.add(aid)
    return len(seen)


def count_search_submission_started() -> int:
    """Count durable SEARCH_SUBMISSION_STARTED event files on disk."""
    total = 0
    for path in attempts_dir().glob("*.SEARCH_SUBMISSION_STARTED.json"):
        if path.is_file():
            total += 1
    return total


def list_search_submission_attempt_ids() -> list[str]:
    """AttemptIds present in the canonical attempts directory (historical + current)."""
    ids: list[str] = []
    suffix = ".SEARCH_SUBMISSION_STARTED.json"
    for path in attempts_dir().glob(f"*{suffix}"):
        if path.is_file():
            ids.append(path.name[: -len(suffix)])
    return sorted(ids)


def capture_attempt_event_baseline() -> dict[str, Any]:
    """Snapshot historical canonical events before a fresh reliability run.

    Historical events are allowed and must never be deleted. Current-run
    consumption is counted only via exact attemptIds issued by this run.
    """
    ids = list_search_submission_attempt_ids()
    return {
        "count": len(ids),
        "attemptIds": ids,
        "canonicalDir": str(attempts_dir()),
        "canonicalDirWsl": attempts_dir_wsl(),
        "note": "historical events allowed; new consumption counted only via this-run attemptIds",
        "deleted": False,
    }


def current_run_consumed_count(attempt_ids: list[str], *, baseline_ids: list[str] | None = None) -> int:
    """Count SEARCH_SUBMISSION_STARTED among this-run attemptIds only."""
    _ = baseline_ids  # retained for callers/audits; filtering is by attempt_ids alone
    return count_consumed_live_navigations(attempt_ids)


__all__ = [
    "DEFAULT_EVENTS_DIR",
    "SEARCH_SUBMISSION_STARTED",
    "SearchSubmissionEvent",
    "attempts_dir",
    "attempts_dir_wsl",
    "canonical_attempts_dir",
    "capture_attempt_event_baseline",
    "count_consumed_live_navigations",
    "count_search_submission_started",
    "current_run_consumed_count",
    "emit_search_submission_started",
    "event_path",
    "has_search_submission_started",
    "list_search_submission_attempt_ids",
    "lookup_search_submission_event",
    "new_attempt_id",
    "query_fingerprint",
    "read_search_submission_event",
]
