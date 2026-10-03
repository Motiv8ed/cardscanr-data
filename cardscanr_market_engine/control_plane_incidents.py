#!/usr/bin/env python3
"""Control-plane incident ledger for attributable challenge/SORRY protections.

Challenge/cooldown protections must be attributable to a source incident so that
invalidating a proven-false incident reconciles ONLY that incident's derived
state, preserving newer or independent protections.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import uuid
from pathlib import Path
from typing import Any

from .atomic_json_state import AtomicStateError, atomic_write_json, locked_json_state, read_json_object
from .config import REPORTS_DIR
from .control_plane_state_ownership import assert_canonical_state_writer_domain_allowed
from .marketplace_ops_state import (
    get_active_cooldown,
    load_ops_state,
    parse_utc,
    state_path as marketplace_ops_state_path,
    utc_iso,
    utc_now,
)

INCIDENT_STATE_PATH = REPORTS_DIR / "runtime" / "control_plane_incidents.json"

INCIDENT_TYPE_CHALLENGE = "CHALLENGE"
INCIDENT_TYPE_TRANSIENT_EBAY = "TRANSIENT_EBAY_SORRY"
INCIDENT_TYPE_AUTH = "AUTH_REQUIRED"

STATUS_ACTIVE = "active"
STATUS_INVALIDATED = "invalidated"


def incidents_path() -> Path:
    raw = os.getenv("CONTROL_PLANE_INCIDENTS_PATH", "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else (REPORTS_DIR.parent / path)
    return INCIDENT_STATE_PATH


def _load_ledger(*, path: Path | None = None) -> dict[str, Any]:
    target = path or incidents_path()
    if not target.exists():
        return {"version": 1, "incidents": {}}
    payload = read_json_object(target, default={"version": 1, "incidents": {}})
    if not isinstance(payload.get("incidents"), dict):
        payload["incidents"] = {}
    return payload


def _save_ledger_unlocked(payload: dict[str, Any], *, path: Path | None = None) -> Path:
    """Replace incident ledger without acquiring the lock.

    ONLY call while the canonical incidents FileLock is already held.
    Prefer _save_ledger() for public/test use.
    """
    target = path or incidents_path()
    clean = {
        "version": 1,
        "updatedAtUtc": utc_iso(),
        "incidents": payload.get("incidents") if isinstance(payload.get("incidents"), dict) else {},
    }
    return atomic_write_json(target, clean)


def _save_ledger(
    payload: dict[str, Any],
    *,
    path: Path | None = None,
    expected_revision: int | None = None,
    force: bool = False,
) -> Path:
    """Public locked whole-document replacement of the incident ledger."""
    assert_canonical_state_writer_domain_allowed()
    target = path or incidents_path()
    clean = {
        "version": 1,
        "updatedAtUtc": utc_iso(),
        "incidents": payload.get("incidents") if isinstance(payload.get("incidents"), dict) else {},
    }
    with locked_json_state(target, default={"version": 1, "incidents": {}}) as current:
        current_rev = int(current.get("revision") or 0)
        if current_rev > 0 and expected_revision is None and not force:
            raise AtomicStateError(
                f"stale_or_unversioned_ledger_save_rejected:revision={current_rev}"
            )
        if expected_revision is not None and current_rev != int(expected_revision):
            raise AtomicStateError(
                f"stale_ledger_revision:expected={expected_revision}:actual={current_rev}"
            )
        clean["revision"] = current_rev + 1
        current.clear()
        current.update(clean)
    return target


def new_incident_id() -> str:
    return f"cpi_{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class ControlPlaneIncident:
    incident_id: str
    market: str
    incident_type: str
    recorded_at: datetime
    classification: str
    status: str = STATUS_ACTIVE
    source_probe_id: str | None = None
    source_attempt_id: str | None = None
    message: str | None = None
    derived: dict[str, Any] | None = None
    invalidated_at: datetime | None = None
    invalidation_reason: str | None = None
    evidence: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "incidentId": self.incident_id,
            "market": self.market,
            "incidentType": self.incident_type,
            "recordedAt": utc_iso(self.recorded_at),
            "classification": self.classification,
            "status": self.status,
            "sourceProbeId": self.source_probe_id,
            "sourceAttemptId": self.source_attempt_id,
            "message": (self.message or "")[:300] or None,
            "derived": self.derived or {},
            "invalidatedAt": utc_iso(self.invalidated_at) if self.invalidated_at else None,
            "invalidationReason": self.invalidation_reason,
            "evidence": self.evidence or {},
        }


def get_incident(incident_id: str, *, path: Path | None = None) -> dict[str, Any] | None:
    ledger = _load_ledger(path=path)
    row = (ledger.get("incidents") or {}).get(str(incident_id))
    return dict(row) if isinstance(row, dict) else None


def list_active_incidents(
    *,
    market: str | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    ledger = _load_ledger(path=path)
    market_n = str(market or "").strip().upper() or None
    out: list[dict[str, Any]] = []
    for row in (ledger.get("incidents") or {}).values():
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "") != STATUS_ACTIVE:
            continue
        if market_n and str(row.get("market") or "").upper() != market_n:
            continue
        out.append(dict(row))
    out.sort(key=lambda r: str(r.get("recordedAt") or ""))
    return out


def register_incident(
    *,
    market: str,
    incident_type: str,
    classification: str,
    message: str | None = None,
    source_probe_id: str | None = None,
    source_attempt_id: str | None = None,
    derived: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    recorded_at: datetime | None = None,
    incident_id: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    current = recorded_at or utc_now()
    iid = incident_id or new_incident_id()
    incident = ControlPlaneIncident(
        incident_id=iid,
        market=str(market or "").strip().upper(),
        incident_type=str(incident_type or "").strip().upper(),
        recorded_at=current,
        classification=str(classification or "").strip(),
        status=STATUS_ACTIVE,
        source_probe_id=source_probe_id,
        source_attempt_id=source_attempt_id,
        message=message,
        derived=derived or {},
        evidence=evidence or {},
    )
    target = path or incidents_path()
    row = incident.to_dict()

    def _apply(payload: dict[str, Any]) -> None:
        incidents = dict(payload.get("incidents") or {})
        incidents[iid] = row
        payload["incidents"] = incidents
        payload["version"] = 1
        payload["updatedAtUtc"] = utc_iso()

    try:
        with locked_json_state(target, default={"version": 1, "incidents": {}}) as payload:
            _apply(payload)
            payload["revision"] = int(payload.get("revision") or 0) + 1
    except AtomicStateError:
        raise
    return dict(row)


def _recorded_at_matches(actual: str | None, expected: str | None) -> bool:
    a = parse_utc(actual)
    e = parse_utc(expected)
    if a is None or e is None:
        return str(actual or "").strip() == str(expected or "").strip()
    # Allow microsecond formatting differences.
    return abs((a - e).total_seconds()) < 0.001


def clear_marketplace_cooldown_if_matches(
    market: str,
    *,
    expected_reason: str,
    expected_recorded_at: str,
    expected_until: str | None = None,
    expected_incident_id: str | None = None,
    expected_last_error_contains: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Compare-and-swap clear of a marketplace cooldown by incident provenance.

    Entire read/compare/mutate/write runs under the interprocess ops-state lock.
    Aborts if reason/recordedAt/until/incidentId diverge (including concurrent writers).
    """
    normalized = str(market or "").strip().upper()
    target = path or marketplace_ops_state_path()
    result: dict[str, Any] = {
        "cleared": False,
        "reason": "absent",
        "market": normalized,
        "before": None,
        "after": None,
    }

    try:
        with locked_json_state(target, default={"version": 1, "markets": {}}) as payload:
            if not isinstance(payload.get("markets"), dict):
                payload["markets"] = {}
            markets = dict(payload.get("markets") or {})
            row = markets.get(normalized)
            if not isinstance(row, dict):
                result = {
                    "cleared": False,
                    "reason": "absent",
                    "market": normalized,
                    "before": None,
                    "after": dict(payload),
                }
                return result

            mismatches: list[str] = []
            if str(row.get("reason") or "") != str(expected_reason):
                mismatches.append("reason_mismatch")
            if not _recorded_at_matches(str(row.get("recordedAt") or ""), expected_recorded_at):
                mismatches.append("recorded_at_mismatch")
            if expected_until is not None and not _recorded_at_matches(
                str(row.get("until") or ""), expected_until
            ):
                mismatches.append("until_mismatch")
            if expected_incident_id is not None and str(row.get("incidentId") or "") != str(
                expected_incident_id
            ):
                mismatches.append("incident_id_mismatch")
            if expected_last_error_contains and expected_last_error_contains not in str(
                row.get("lastError") or ""
            ):
                mismatches.append("last_error_mismatch")
            if mismatches:
                result = {
                    "cleared": False,
                    "reason": ",".join(mismatches),
                    "market": normalized,
                    "before": dict(row),
                    "after": dict(payload),
                }
                return result

            markets2 = dict(markets)
            del markets2[normalized]
            payload["markets"] = markets2
            payload["version"] = 1
            payload["updatedAtUtc"] = utc_iso()
            result = {
                "cleared": True,
                "reason": "matched_provenance_cleared",
                "market": normalized,
                "before": dict(row),
                "after": dict(payload),
            }
            return result
    except AtomicStateError as exc:
        return {
            "cleared": False,
            "reason": f"state_lock_or_write_failed:{exc}",
            "market": normalized,
            "before": None,
            "after": load_ops_state(path=target),
        }


def invalidate_control_plane_incident(
    incident_id: str,
    *,
    invalidation_reason: str,
    evidence: dict[str, Any] | None = None,
    now: datetime | None = None,
    incidents_file: Path | None = None,
    marketplace_ops_path: Path | None = None,
    availability_path: Path | None = None,
) -> dict[str, Any]:
    """Invalidate incident X and reconcile ONLY derived protections created by X.

    Preserves independent/newer incidents and their derived state.
    Idempotent: second call is a safe no-op once already invalidated.

    Locking note: ops/availability reconciliation uses their own interprocess locks.
    The incidents ledger lock is held only for the final status transition so we do
    not nest cross-file locks (deadlock risk with register_incident / cooldown writers).
    """
    from .ebay_availability import (
        clear_challenge_for_manual_restore,
        get_availability,
        load_availability,
    )

    current = now or utc_now()
    target_inc = incidents_file or incidents_path()
    ledger = _load_ledger(path=target_inc)
    incidents = dict(ledger.get("incidents") or {})
    row = incidents.get(str(incident_id))
    if not isinstance(row, dict):
        return {"ok": False, "reason": "incident_not_found", "incidentId": incident_id}

    market = str(row.get("market") or "").upper()
    before_ops = get_active_cooldown(market, now=current, path=marketplace_ops_path)
    before_avail = get_availability(now=current, path=availability_path)

    if str(row.get("status") or "") == STATUS_INVALIDATED:
        return {
            "ok": True,
            "reason": "already_invalidated",
            "incidentId": incident_id,
            "idempotent": True,
            "market": market,
            "marketplaceCooldownAfter": None if before_ops is None else before_ops.to_dict(),
            "availabilityAfter": before_avail.to_dict(),
        }

    derived = row.get("derived") if isinstance(row.get("derived"), dict) else {}
    actions: list[str] = []

    ops_result: dict[str, Any] | None = None
    expected_recorded = str(derived.get("marketplaceCooldownRecordedAt") or row.get("recordedAt") or "")
    expected_reason = str(
        derived.get("marketplaceCooldownReason") or row.get("classification") or "CHALLENGE_REQUIRED"
    )
    expected_until = derived.get("marketplaceCooldownUntil")
    evidence_blob = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    is_legacy = bool(evidence_blob.get("legacyProvenanceMatch"))
    stored_ops_iid = derived.get("marketplaceCooldownIncidentId")
    # Clear by provenance even when the cooldown window has already expired, so
    # invalidation still removes attributable rows (active OR stale).
    ops_payload = load_ops_state(path=marketplace_ops_path)
    raw_ops_row = (ops_payload.get("markets") or {}).get(market)
    should_attempt_ops_clear = before_ops is not None or isinstance(raw_ops_row, dict)
    if should_attempt_ops_clear and (
        before_ops is not None
        or str((raw_ops_row or {}).get("incidentId") or "") in {str(incident_id), str(stored_ops_iid or "")}
        or str((raw_ops_row or {}).get("reason") or "") == expected_reason
    ):
        expected_iid: str | None = None
        if stored_ops_iid:
            expected_iid = str(stored_ops_iid)
        elif not is_legacy and (
            (before_ops is not None and before_ops.incident_id) or (raw_ops_row or {}).get("incidentId")
        ):
            expected_iid = incident_id
        ops_result = clear_marketplace_cooldown_if_matches(
            market,
            expected_reason=expected_reason,
            expected_recorded_at=expected_recorded,
            expected_until=str(expected_until) if expected_until else None,
            expected_incident_id=expected_iid,
            path=marketplace_ops_path,
        )
        if ops_result.get("cleared"):
            actions.append("cleared_marketplace_cooldown")
        else:
            actions.append(f"marketplace_cooldown_untouched:{ops_result.get('reason')}")

    avail_actions: list[str] = []
    snap = load_availability(path=availability_path)
    derived_avail_state = str(derived.get("availabilityState") or "")
    derived_ref = str(derived.get("availabilityFailureReference") or row.get("message") or "")
    if snap.state == "CHALLENGE_REQUIRED":
        same_incident = (
            str(
                getattr(snap, "last_challenge_incident_id", None)
                or snap.__dict__.get("last_challenge_incident_id")
                or ""
            )
            == incident_id
        )
        snap_dict = snap.to_dict()
        same_incident = same_incident or str(snap_dict.get("lastChallengeIncidentId") or "") == incident_id
        same_ref = bool(derived_ref) and derived_ref[:80] in str(snap.last_failure_reference or "")
        if same_incident or (derived_avail_state == "CHALLENGE_REQUIRED" and same_ref):
            other_active = [
                i
                for i in list_active_incidents(market=market, path=incidents_file)
                if str(i.get("incidentId")) != incident_id
                and str(i.get("incidentType")) in {INCIDENT_TYPE_CHALLENGE, INCIDENT_TYPE_AUTH}
            ]
            if other_active:
                avail_actions.append("availability_preserved_newer_challenge")
            else:
                clear_challenge_for_manual_restore(path=availability_path, now=current)
                avail_actions.append("availability_restored_probe_required")
        else:
            avail_actions.append("availability_untouched_unrelated")
    else:
        avail_actions.append(f"availability_already_{snap.state}")

    try:
        with locked_json_state(target_inc, default={"version": 1, "incidents": {}}) as locked:
            live = dict(locked.get("incidents") or {})
            live_row = live.get(str(incident_id))
            if not isinstance(live_row, dict):
                return {"ok": False, "reason": "incident_not_found", "incidentId": incident_id}
            if str(live_row.get("status") or "") == STATUS_INVALIDATED:
                after_ops = get_active_cooldown(market, now=current, path=marketplace_ops_path)
                after_avail = get_availability(now=current, path=availability_path)
                return {
                    "ok": True,
                    "reason": "already_invalidated",
                    "incidentId": incident_id,
                    "idempotent": True,
                    "market": market,
                    "actions": actions + avail_actions,
                    "marketplaceCooldownClear": ops_result,
                    "marketplaceCooldownAfter": None if after_ops is None else after_ops.to_dict(),
                    "availabilityAfter": after_avail.to_dict(),
                }
            updated = dict(live_row)
            # Preserve original classification/timestamps; only transition status.
            updated["status"] = STATUS_INVALIDATED
            updated["invalidatedAt"] = utc_iso(current)
            updated["invalidationReason"] = invalidation_reason
            merged = dict(updated.get("evidence") or {})
            if evidence:
                merged.update(evidence)
            # Auditable reconciliation trail (never deletes the original row).
            hist = list(merged.get("reconciliationHistory") or [])
            hist.append(
                {
                    "atUtc": utc_iso(current),
                    "action": "invalidate",
                    "reason": invalidation_reason,
                    "source": (evidence or {}).get("reconciliationSource")
                    or (evidence or {}).get("provenanceClassification")
                    or "invalidate_control_plane_incident",
                }
            )
            merged["reconciliationHistory"] = hist
            updated["evidence"] = merged
            live[str(incident_id)] = updated
            locked["incidents"] = live
            locked["version"] = 1
            locked["updatedAtUtc"] = utc_iso(current)
            locked["revision"] = int(locked.get("revision") or 0) + 1
    except AtomicStateError as exc:
        return {
            "ok": False,
            "reason": f"state_lock_or_write_failed:{exc}",
            "incidentId": incident_id,
            "actions": actions + avail_actions,
            "marketplaceCooldownClear": ops_result,
        }

    after_ops = get_active_cooldown(market, now=current, path=marketplace_ops_path)
    after_avail = get_availability(now=current, path=availability_path)
    return {
        "ok": True,
        "reason": "invalidated",
        "incidentId": incident_id,
        "market": market,
        "actions": actions + avail_actions,
        "marketplaceCooldownClear": ops_result,
        "marketplaceCooldownAfter": None if after_ops is None else after_ops.to_dict(),
        "availabilityAfter": after_avail.to_dict(),
        "idempotent": False,
    }


def register_legacy_false_challenge_incident(
    *,
    market: str,
    recorded_at: str,
    until: str,
    message: str,
    source_probe_id: str,
    evidence: dict[str, Any],
    incident_id: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Register a pre-ledger false challenge so it can be invalidated atomically."""
    recorded = parse_utc(recorded_at) or utc_now()
    iid = incident_id or f"cpi_legacy_{market.lower()}_{recorded.strftime('%Y%m%dT%H%M%SZ')}"
    existing = get_incident(iid, path=path)
    if existing is not None:
        return existing
    return register_incident(
        market=market,
        incident_type=INCIDENT_TYPE_CHALLENGE,
        classification="CHALLENGE_REQUIRED",
        message=message,
        source_probe_id=source_probe_id,
        recorded_at=recorded,
        incident_id=iid,
        derived={
            "marketplaceCooldownReason": "CHALLENGE_REQUIRED",
            "marketplaceCooldownRecordedAt": recorded_at,
            "marketplaceCooldownUntil": until,
            "availabilityState": "CHALLENGE_REQUIRED",
            "availabilityFailureReference": message,
        },
        evidence={**evidence, "legacyProvenanceMatch": True},
        path=path,
    )


# Re-export helper used by callers that only need clear without inventing paths.
__all__ = [
    "INCIDENT_TYPE_CHALLENGE",
    "INCIDENT_TYPE_TRANSIENT_EBAY",
    "INCIDENT_TYPE_AUTH",
    "STATUS_ACTIVE",
    "STATUS_INVALIDATED",
    "clear_marketplace_cooldown_if_matches",
    "get_incident",
    "incidents_path",
    "invalidate_control_plane_incident",
    "list_active_incidents",
    "new_incident_id",
    "register_incident",
    "register_legacy_false_challenge_incident",
]
