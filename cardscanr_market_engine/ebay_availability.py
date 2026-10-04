#!/usr/bin/env python3
"""Per-market eBay browser availability circuit breaker.

Marketplace-specific SORRY / TEMPORARY_EBAY_SERVER_FAILURE must not halt other
markets. A v1 single-snapshot file remains AU until a non-AU market is written.
True application integrity failures remain a separate global hard-stop path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Literal

from filelock import FileLock, Timeout

from .config import REPORTS_DIR
from .marketplace_ops_state import parse_utc, utc_iso, utc_now
from .atomic_json_state import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    AtomicStateError,
    atomic_write_json,
    lock_path_for,
    locked_json_state,
    read_json_object,
)
from .control_plane_state_ownership import assert_canonical_state_writer_domain_allowed

AvailabilityState = Literal[
    "HEALTHY",
    "COOLDOWN",
    "PROBE_REQUIRED",
    "CHALLENGE_REQUIRED",
]

STATE_PATH = REPORTS_DIR / "runtime" / "ebay_availability_state.json"

# Conservative defaults (configurable via env).
DEFAULT_FIRST_SORRY_COOLDOWN_MINUTES = 60
DEFAULT_FAILED_PROBE_COOLDOWN_HOURS = 6
DEFAULT_REPEATED_DEFER_HOURS = 18
DEFAULT_RECOVERY_STREAK_REQUIRED = 3

EBAY_AVAILABILITY_COOLDOWN = "EBAY_AVAILABILITY_COOLDOWN"
EBAY_AVAILABILITY_PROBE = "EBAY_AVAILABILITY_PROBE"
EBAY_AVAILABILITY_CONFIRMED_HEALTHY = "EBAY_AVAILABILITY_CONFIRMED_HEALTHY"
EBAY_CHALLENGE_REQUIRED = "EBAY_CHALLENGE_REQUIRED"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(1, int(str(raw).strip()))
    except ValueError:
        return default


def first_sorry_cooldown() -> timedelta:
    return timedelta(
        minutes=_env_int(
            "EBAY_AVAILABILITY_FIRST_SORRY_COOLDOWN_MINUTES",
            DEFAULT_FIRST_SORRY_COOLDOWN_MINUTES,
        )
    )


def failed_probe_cooldown() -> timedelta:
    return timedelta(
        hours=_env_int(
            "EBAY_AVAILABILITY_FAILED_PROBE_COOLDOWN_HOURS",
            DEFAULT_FAILED_PROBE_COOLDOWN_HOURS,
        )
    )


def repeated_defer_cooldown() -> timedelta:
    return timedelta(
        hours=_env_int(
            "EBAY_AVAILABILITY_REPEATED_DEFER_HOURS",
            DEFAULT_REPEATED_DEFER_HOURS,
        )
    )


def recovery_streak_required() -> int:
    return _env_int("EBAY_AVAILABILITY_RECOVERY_STREAK", DEFAULT_RECOVERY_STREAK_REQUIRED)


def state_path() -> Path:
    raw = os.getenv("EBAY_AVAILABILITY_STATE_PATH", "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else (REPORTS_DIR.parent / path)
    return STATE_PATH


@dataclass
class EbayAvailabilitySnapshot:
    state: AvailabilityState
    opened_at: datetime | None = None
    last_sorry_at: datetime | None = None
    consecutive_sorry_events: int = 0
    last_healthy_at: datetime | None = None
    next_probe_at: datetime | None = None
    last_failure_reference: str | None = None
    current_cooldown_seconds: int = 0
    browser_profile: str | None = None
    market: str = "AU"
    recovery_health_count: int = 0
    confirmed_healthy: bool = False
    probe_in_flight: bool = False
    last_outcome: str | None = None
    updated_at: datetime | None = None
    last_challenge_incident_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "version": 1,
            "state": self.state,
            "openedAt": utc_iso(self.opened_at) if self.opened_at else None,
            "lastSorryAt": utc_iso(self.last_sorry_at) if self.last_sorry_at else None,
            "consecutiveSorryEvents": int(self.consecutive_sorry_events),
            "lastHealthyAt": utc_iso(self.last_healthy_at) if self.last_healthy_at else None,
            "nextProbeAt": utc_iso(self.next_probe_at) if self.next_probe_at else None,
            "lastFailureReference": self.last_failure_reference,
            "currentCooldownSeconds": int(self.current_cooldown_seconds),
            "browserProfile": self.browser_profile,
            "market": self.market,
            "recoveryHealthCount": int(self.recovery_health_count),
            "confirmedHealthy": bool(self.confirmed_healthy),
            "probeInFlight": bool(self.probe_in_flight),
            "lastOutcome": self.last_outcome,
            "updatedAtUtc": utc_iso(self.updated_at or utc_now()),
        }
        if self.last_challenge_incident_id:
            payload["lastChallengeIncidentId"] = self.last_challenge_incident_id
        return payload


def _default_snapshot(*, market: str = "AU") -> EbayAvailabilitySnapshot:
    return EbayAvailabilitySnapshot(
        state="HEALTHY",
        market=str(market or "AU").upper(),
        browser_profile=os.getenv("CARDSCANR_CHROME_PROFILE", "~/.config/cardscanr-chrome"),
        confirmed_healthy=True,
        updated_at=utc_now(),
    )


def _from_dict(payload: dict[str, Any]) -> EbayAvailabilitySnapshot:
    state = str(payload.get("state") or "HEALTHY").upper()
    if state not in {"HEALTHY", "COOLDOWN", "PROBE_REQUIRED", "CHALLENGE_REQUIRED"}:
        state = "HEALTHY"
    return EbayAvailabilitySnapshot(
        state=state,  # type: ignore[arg-type]
        opened_at=parse_utc(payload.get("openedAt") or payload.get("opened_at")),
        last_sorry_at=parse_utc(payload.get("lastSorryAt") or payload.get("last_sorry_at")),
        consecutive_sorry_events=int(payload.get("consecutiveSorryEvents") or payload.get("consecutive_sorry_events") or 0),
        last_healthy_at=parse_utc(payload.get("lastHealthyAt") or payload.get("last_healthy_at")),
        next_probe_at=parse_utc(payload.get("nextProbeAt") or payload.get("next_probe_at")),
        last_failure_reference=(
            str(payload.get("lastFailureReference") or payload.get("last_failure_reference") or "") or None
        ),
        current_cooldown_seconds=int(payload.get("currentCooldownSeconds") or payload.get("current_cooldown_seconds") or 0),
        browser_profile=str(payload.get("browserProfile") or payload.get("browser_profile") or "") or None,
        market=str(payload.get("market") or "AU").upper(),
        recovery_health_count=int(payload.get("recoveryHealthCount") or payload.get("recovery_health_count") or 0),
        confirmed_healthy=bool(payload.get("confirmedHealthy") if "confirmedHealthy" in payload else payload.get("confirmed_healthy", False)),
        probe_in_flight=bool(payload.get("probeInFlight") or payload.get("probe_in_flight") or False),
        last_outcome=str(payload.get("lastOutcome") or payload.get("last_outcome") or "") or None,
        updated_at=parse_utc(payload.get("updatedAtUtc") or payload.get("updated_at")),
        last_challenge_incident_id=(
            str(payload.get("lastChallengeIncidentId") or payload.get("last_challenge_incident_id") or "") or None
        ),
    )


def _normalize_market(market: str | None) -> str:
    return str(market or "AU").strip().upper() or "AU"


def _slot_from_payload(payload: dict[str, Any], market: str) -> dict[str, Any] | None:
    code = _normalize_market(market)
    markets = payload.get("markets")
    if isinstance(markets, dict):
        slot = markets.get(code) or markets.get(code.lower())
        return slot if isinstance(slot, dict) else None
    if payload.get("state"):
        doc_market = str(payload.get("market") or "AU").upper()
        if code == doc_market:
            return payload
        if code == "AU" and doc_market in {"", "AU"}:
            return payload
    return None


def load_availability(*, path: Path | None = None, market: str | None = None) -> EbayAvailabilitySnapshot:
    """Load availability snapshot for one market.

    Missing file / missing market slot → default HEALTHY bootstrap snapshot.
    Corrupt/unreadable JSON → AtomicStateError (fail closed; never treat as HEALTHY).
    """
    target = path or state_path()
    code = _normalize_market(market)
    if not target.exists():
        return _default_snapshot(market=code)
    payload = read_json_object(target)
    if not payload:
        return _default_snapshot(market=code)
    slot = _slot_from_payload(payload, code)
    if not slot:
        return _default_snapshot(market=code)
    snap = _from_dict(slot)
    snap.market = code
    return snap


def save_availability_unlocked(
    snapshot: EbayAvailabilitySnapshot,
    *,
    path: Path | None = None,
) -> Path:
    """Replace availability JSON without acquiring the lock.

    ONLY call while the canonical availability FileLock is already held
    (e.g. inside locked_json_state). Prefer save_availability() for public use.
    """
    target = path or state_path()
    snapshot.updated_at = utc_now()
    return atomic_write_json(target, snapshot.to_dict())


def save_availability(
    snapshot: EbayAvailabilitySnapshot,
    *,
    path: Path | None = None,
    expected_revision: int | None = None,
    force: bool = False,
) -> Path:
    """Public locked whole-document replacement of availability state.

    Runtime mutations must prefer ``_mutate_availability``. Blind whole-document
    replacement of an existing revised document requires ``expected_revision``
    CAS or ``force=True`` (tests/bootstrap only).
    """
    assert_canonical_state_writer_domain_allowed()
    target = path or state_path()
    snapshot.updated_at = utc_now()
    with locked_json_state(target, default=_default_snapshot().to_dict()) as payload:
        current_rev = int(payload.get("revision") or 0)
        if current_rev > 0 and expected_revision is None and not force:
            raise AtomicStateError(
                f"stale_or_unversioned_availability_save_rejected:revision={current_rev}"
            )
        if expected_revision is not None and current_rev != int(expected_revision):
            raise AtomicStateError(
                f"stale_availability_revision:expected={expected_revision}:actual={current_rev}"
            )
        new_payload = snapshot.to_dict()
        new_payload["revision"] = current_rev + 1
        payload.clear()
        payload.update(new_payload)
    return target


def _write_market_slot(payload: dict[str, Any], market: str, snap_dict: dict[str, Any], revision: int) -> None:
    code = _normalize_market(market)
    snap_dict = dict(snap_dict)
    snap_dict["market"] = code
    markets = payload.get("markets")
    has_multi = isinstance(markets, dict)
    if not has_multi and code == "AU":
        payload.clear()
        payload.update(snap_dict)
        payload["revision"] = revision
        return
    preserved: dict[str, Any] = {}
    if has_multi:
        preserved = {str(k).upper(): dict(v) for k, v in markets.items() if isinstance(v, dict)}
    elif payload.get("state"):
        existing_market = str(payload.get("market") or "AU").upper()
        preserved[existing_market] = {k: v for k, v in payload.items() if k not in {"markets", "revision"}}
    preserved[code] = snap_dict
    payload.clear()
    payload.update({"version": 2, "revision": revision, "markets": preserved})


def _mutate_availability(
    mutator,
    *,
    path: Path | None = None,
    now: datetime | None = None,
    market: str | None = None,
) -> EbayAvailabilitySnapshot:
    """Locked read→mutate→atomic write for one market's availability slot."""
    assert_canonical_state_writer_domain_allowed()
    target = path or state_path()
    current = now or utc_now()
    code = _normalize_market(market)
    with locked_json_state(target, default=_default_snapshot(market=code).to_dict()) as payload:
        slot = _slot_from_payload(payload, code)
        snap = _from_dict(slot) if slot else _default_snapshot(market=code)
        snap.market = code
        snap = refresh_transitions(snap, now=current)
        snap = mutator(snap) or snap
        snap.market = code
        snap.updated_at = utc_now()
        rev = int(payload.get("revision") or 0) + 1
        _write_market_slot(payload, code, snap.to_dict(), rev)
        return snap


def refresh_transitions(
    snapshot: EbayAvailabilitySnapshot,
    *,
    now: datetime | None = None,
) -> EbayAvailabilitySnapshot:
    """Advance COOLDOWN → PROBE_REQUIRED when next_probe_at elapses."""
    current = now or utc_now()
    if snapshot.state == "CHALLENGE_REQUIRED":
        return snapshot
    if snapshot.state == "COOLDOWN" and snapshot.next_probe_at is not None and current >= snapshot.next_probe_at:
        snapshot.state = "PROBE_REQUIRED"
        snapshot.probe_in_flight = False
        snapshot.last_outcome = "cooldown_expired_probe_required"
    return snapshot


def peek_availability(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    market: str | None = None,
) -> EbayAvailabilitySnapshot:
    """Read availability under lock without persisting.

    For gate evaluation / stop accounting: must not create avoidable write races.
    Applies COOLDOWN→PROBE_REQUIRED in-memory only (does not mutate durable state).
    """
    current = now or utc_now()
    target = path or state_path()
    code = _normalize_market(market)
    with locked_json_state(target, default=_default_snapshot(market=code).to_dict(), write=False) as payload:
        slot = _slot_from_payload(payload, code)
        snap = _from_dict(slot) if slot else _default_snapshot(market=code)
        snap.market = code
        return refresh_transitions(snap, now=current)


def get_availability(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    persist_transitions: bool = True,
    market: str | None = None,
) -> EbayAvailabilitySnapshot:
    """Locked availability read with optional transition persistence.

    When ``persist_transitions`` is False (gate inspection), behaves like
    ``peek_availability`` — no durable write.

    When True, persists only if ``refresh_transitions`` changes durable state
    (avoids needless HEALTHY rewrite races during stop accounting).
    """
    current = now or utc_now()
    target = path or state_path()
    code = _normalize_market(market)
    if not persist_transitions:
        return peek_availability(now=current, path=target, market=code)
    # Locked read; write only when a transition must be durably recorded.
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path_for(target)), timeout=DEFAULT_LOCK_TIMEOUT_SECONDS)
    try:
        lock.acquire()
    except Timeout as exc:
        raise AtomicStateError(f"state_lock_timeout:{target.name}") from exc
    try:
        payload = read_json_object(target, default=_default_snapshot(market=code).to_dict())
        slot = _slot_from_payload(payload, code)
        snap = _from_dict(slot) if slot else _default_snapshot(market=code)
        snap.market = code
        snap = refresh_transitions(snap, now=current)
        new_payload = snap.to_dict()
        if slot is None:
            return snap
        meaningful = (
            new_payload.get("state") != slot.get("state")
            or new_payload.get("probeInFlight") != slot.get("probeInFlight")
            or new_payload.get("nextProbeAt") != slot.get("nextProbeAt")
        )
        if meaningful:
            assert_canonical_state_writer_domain_allowed()
            rev = int(payload.get("revision") or 0) + 1
            _write_market_slot(payload, code, new_payload, rev)
            atomic_write_json(target, payload)
        return snap
    finally:
        try:
            lock.release()
        except Exception:
            pass


def browser_work_allowed(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    for_probe: bool = False,
    persist_transitions: bool = False,
    market: str | None = None,
) -> tuple[bool, str, EbayAvailabilitySnapshot]:
    """Whether an eBay browser pricing attempt may start.

    Fail closed: corrupt/unreadable state or lock timeout never authorises work.

    Default ``persist_transitions=False`` so gate evaluation / stop accounting does
    not mutate availability merely to answer allowed/denied. Callers that own
    transition persistence (schedulers advancing COOLDOWN) may pass True.
    """
    try:
        snap = get_availability(
            now=now,
            path=path,
            persist_transitions=persist_transitions,
            market=market,
        )
    except AtomicStateError:
        # Unreadable / lock failure — do not treat as HEALTHY.
        blocked = _default_snapshot()
        blocked.state = "CHALLENGE_REQUIRED"
        blocked.last_outcome = "availability_state_unreadable_or_locked"
        blocked.confirmed_healthy = False
        return False, EBAY_CHALLENGE_REQUIRED, blocked
    if snap.state == "CHALLENGE_REQUIRED":
        return False, EBAY_CHALLENGE_REQUIRED, snap
    if snap.state == "COOLDOWN":
        return False, EBAY_AVAILABILITY_COOLDOWN, snap
    if snap.state == "PROBE_REQUIRED":
        if for_probe and not snap.probe_in_flight:
            return True, EBAY_AVAILABILITY_PROBE, snap
        if for_probe and snap.probe_in_flight:
            return False, "EBAY_AVAILABILITY_PROBE_IN_FLIGHT", snap
        return False, EBAY_AVAILABILITY_COOLDOWN, snap
    # HEALTHY
    return True, "EBAY_AVAILABILITY_HEALTHY", snap


def begin_probe(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    market: str | None = None,
) -> EbayAvailabilitySnapshot:
    """Mark the single allowed recovery probe as in-flight."""
    current = now or utc_now()

    def _apply(snap: EbayAvailabilitySnapshot) -> EbayAvailabilitySnapshot:
        if snap.state != "PROBE_REQUIRED":
            raise RuntimeError(f"begin_probe_requires_PROBE_REQUIRED got={snap.state}")
        if snap.probe_in_flight:
            raise RuntimeError("begin_probe_already_in_flight")
        snap.probe_in_flight = True
        snap.last_outcome = "probe_started"
        return snap

    return _mutate_availability(_apply, path=path, now=current, market=market)


def release_probe_local_failure(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    reference: str | None = None,
    market: str | None = None,
) -> EbayAvailabilitySnapshot:
    """Clear probe_in_flight after a local finalize failure (not SORRY, not healthy).

    Leaves state as PROBE_REQUIRED so a later eligible probe can retry without
    falsely confirming health or extending an eBay SORRY cooldown.
    """
    current = now or utc_now()

    def _apply(snap: EbayAvailabilitySnapshot) -> EbayAvailabilitySnapshot:
        snap.probe_in_flight = False
        if snap.state in {"PROBE_REQUIRED", "COOLDOWN"}:
            snap.state = "PROBE_REQUIRED"
            snap.next_probe_at = current
        snap.last_outcome = "probe_finalize_timeout_safe"
        if reference:
            snap.last_failure_reference = reference[:300]
        return snap

    return _mutate_availability(_apply, path=path, now=current, market=market)


def _open_cooldown(
    snap: EbayAvailabilitySnapshot,
    *,
    cooldown: timedelta,
    now: datetime,
    reference: str | None,
    market: str | None,
) -> EbayAvailabilitySnapshot:
    snap.state = "COOLDOWN"
    snap.opened_at = snap.opened_at or now
    snap.last_sorry_at = now
    snap.next_probe_at = now + cooldown
    snap.current_cooldown_seconds = int(cooldown.total_seconds())
    snap.recovery_health_count = 0
    snap.confirmed_healthy = False
    snap.probe_in_flight = False
    if reference:
        snap.last_failure_reference = reference[:300]
    if market:
        snap.market = str(market).upper()
    snap.last_outcome = "TEMPORARY_EBAY_SERVER_FAILURE"
    return snap


def record_sorry(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    reference: str | None = None,
    market: str | None = None,
    from_probe: bool | None = None,
) -> EbayAvailabilitySnapshot:
    """Open/extend worker-wide COOLDOWN after TEMPORARY_EBAY_SERVER_FAILURE."""
    current = now or utc_now()

    def _apply(snap: EbayAvailabilitySnapshot) -> EbayAvailabilitySnapshot:
        was_probe = (
            bool(from_probe)
            if from_probe is not None
            else bool(snap.probe_in_flight or snap.state == "PROBE_REQUIRED")
        )
        snap.consecutive_sorry_events = int(snap.consecutive_sorry_events) + 1
        n = int(snap.consecutive_sorry_events)
        if n <= 1:
            cooldown = first_sorry_cooldown()
        elif n == 2 or was_probe:
            cooldown = failed_probe_cooldown()
            if n < 2:
                snap.consecutive_sorry_events = 2
        else:
            cooldown = repeated_defer_cooldown()
        return _open_cooldown(snap, cooldown=cooldown, now=current, reference=reference, market=market)

    return _mutate_availability(_apply, path=path, now=current, market=market)


def record_challenge(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    reference: str | None = None,
    market: str | None = None,
    incident_id: str | None = None,
) -> EbayAvailabilitySnapshot:
    current = now or utc_now()

    def _apply(snap: EbayAvailabilitySnapshot) -> EbayAvailabilitySnapshot:
        snap.state = "CHALLENGE_REQUIRED"
        snap.opened_at = snap.opened_at or current
        snap.probe_in_flight = False
        snap.recovery_health_count = 0
        snap.confirmed_healthy = False
        snap.next_probe_at = None
        snap.current_cooldown_seconds = 0
        snap.last_outcome = EBAY_CHALLENGE_REQUIRED
        if reference:
            snap.last_failure_reference = reference[:300]
        if market:
            snap.market = str(market).upper()
        if incident_id:
            snap.last_challenge_incident_id = str(incident_id)
        return snap

    return _mutate_availability(_apply, path=path, now=current, market=market)


def record_healthy_browser_check(
    *,
    now: datetime | None = None,
    path: Path | None = None,
    from_probe: bool | None = None,
    market: str | None = None,
) -> EbayAvailabilitySnapshot:
    """Record a successful SOLD/pricing browser check (not a synthetic ping)."""
    current = now or utc_now()

    def _apply(snap: EbayAvailabilitySnapshot) -> EbayAvailabilitySnapshot:
        was_probe = bool(from_probe) if from_probe is not None else bool(snap.probe_in_flight)
        # Fail closed: never let a stale healthy reconciliation erase a newer
        # challenge/SORRY protection that won the lock race.
        if snap.state == "CHALLENGE_REQUIRED":
            return snap
        if snap.state == "COOLDOWN" and not snap.probe_in_flight:
            return snap
        snap.last_healthy_at = current
        snap.probe_in_flight = False
        snap.consecutive_sorry_events = 0
        snap.last_outcome = "HEALTHY_BROWSER_CHECK"
        if was_probe or snap.state in {"PROBE_REQUIRED", "COOLDOWN"}:
            snap.state = "HEALTHY"
            snap.recovery_health_count = 1
            snap.confirmed_healthy = False
            snap.current_cooldown_seconds = 0
            snap.next_probe_at = None
        elif snap.state == "HEALTHY":
            snap.recovery_health_count = int(snap.recovery_health_count) + 1
            if snap.recovery_health_count >= recovery_streak_required():
                snap.confirmed_healthy = True
                snap.last_outcome = EBAY_AVAILABILITY_CONFIRMED_HEALTHY
        else:
            snap.state = "HEALTHY"
            snap.recovery_health_count = max(1, int(snap.recovery_health_count))
        return snap

    return _mutate_availability(_apply, path=path, now=current, market=market)


def clear_challenge_for_manual_restore(
    *,
    path: Path | None = None,
    now: datetime | None = None,
    market: str | None = None,
) -> EbayAvailabilitySnapshot:
    """Andrew-only restore after challenge resolution (not automatic)."""
    current = now or utc_now()

    def _apply(snap: EbayAvailabilitySnapshot) -> EbayAvailabilitySnapshot:
        snap.state = "PROBE_REQUIRED"
        snap.probe_in_flight = False
        snap.next_probe_at = current
        snap.confirmed_healthy = False
        snap.recovery_health_count = 0
        snap.last_outcome = "manual_challenge_cleared_probe_required"
        snap.last_challenge_incident_id = None
        return snap

    return _mutate_availability(_apply, path=path, now=current, market=market)


def seed_from_observed_sorrys(
    *,
    last_sorry_at: datetime,
    consecutive_sorry_events: int,
    path: Path | None = None,
    reference: str | None = None,
    market: str = "AU",
    now: datetime | None = None,
) -> EbayAvailabilitySnapshot:
    """One-time bootstrap from a completed pilot (no new network requests).

    Fail closed: refuses to overwrite state that has already advanced beyond a
    blank HEALTHY bootstrap (CHALLENGE, COOLDOWN, PROBE_REQUIRED, probeInFlight,
    prior sorry counters, challenge incident ids, etc.). Entire check+write runs
    under the canonical availability lock.
    """
    current = now or utc_now()
    consecutive = max(1, int(consecutive_sorry_events))
    if consecutive <= 1:
        cooldown = first_sorry_cooldown()
    elif consecutive == 2:
        cooldown = failed_probe_cooldown()
    else:
        cooldown = repeated_defer_cooldown()
    target = path or state_path()

    def _apply(existing: EbayAvailabilitySnapshot) -> EbayAvailabilitySnapshot:
        # Seedable only when there is no operational history. The blank default
        # snapshot uses confirmed_healthy=True as bootstrap optimism — that alone
        # must not block one-time pilot seeding.
        has_history = (
            existing.state != "HEALTHY"
            or bool(existing.probe_in_flight)
            or int(existing.consecutive_sorry_events or 0) > 0
            or existing.last_sorry_at is not None
            or existing.opened_at is not None
            or existing.next_probe_at is not None
            or bool(existing.last_challenge_incident_id)
            or bool(existing.last_failure_reference)
            or int(existing.current_cooldown_seconds or 0) > 0
            or existing.last_healthy_at is not None
            or int(existing.recovery_health_count or 0) > 0
        )
        if has_history:
            raise AtomicStateError(
                f"seed_from_observed_sorrys_refused:existing_state={existing.state}"
                f":probeInFlight={existing.probe_in_flight}"
                f":sorryEvents={existing.consecutive_sorry_events}"
            )
        snap = _default_snapshot(market=market)
        snap.confirmed_healthy = False
        snap.consecutive_sorry_events = consecutive
        snap.opened_at = last_sorry_at
        snap.last_sorry_at = last_sorry_at
        snap.last_failure_reference = (reference or "")[:300] or None
        snap = _open_cooldown(
            snap,
            cooldown=cooldown,
            now=last_sorry_at,
            reference=reference,
            market=market,
        )
        # If natural time already passed, transition to PROBE_REQUIRED without probing.
        return refresh_transitions(snap, now=current)

    return _mutate_availability(_apply, path=target, now=current, market=market)
