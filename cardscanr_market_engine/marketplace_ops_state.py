#!/usr/bin/env python3
"""Operational marketplace health/cooldown state for live eBay pricing.

Does not change pricing math. Tracks per-market auth/challenge cooldowns so
one blocked marketplace does not get hammered every scheduler cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any

from .config import REPORTS_DIR
from .atomic_json_state import AtomicStateError, atomic_write_json, locked_json_state, read_json_object
from .control_plane_state_ownership import assert_canonical_state_writer_domain_allowed

SUPPORTED_LIVE_MARKETS = ("AU", "US", "GB", "CA")
DEFAULT_AUTH_COOLDOWN_HOURS = 6
DEFAULT_CHALLENGE_COOLDOWN_HOURS = 12
DEFAULT_TRANSIENT_EBAY_COOLDOWN_MINUTES = 15
STATE_PATH = REPORTS_DIR / "runtime" / "marketplace_ops_state.json"

AUTH_MARKERS = (
    "authentication",
    "sign-in",
    "signin",
    "provider_authentication_required",
    "ebay_auth_required",
    "authentication_redirect",
    "authentication_required",
)
CHALLENGE_MARKERS = (
    "challenge",
    "captcha",
    "splashui/challenge",
    "verification challenge",
    "marketplace_challenge",
    "access-block",
    "access_blocked",
)
NO_COMP_MARKERS = (
    "no_clean_exact_comps",
    "no_reliable_price",
    "currently no ebay pricing available",
    "insufficient",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def classify_provider_failure(message: str | None, *, diagnostics: dict[str, Any] | None = None) -> str:
    """Classify a provider/job failure into an operational category.

    Auth/challenge must never be classified as ordinary no-comps.
    """
    text = str(message or "").strip().lower()
    diag = diagnostics or {}
    outcome = str(diag.get("providerOutcome") or diag.get("provider_outcome") or "").strip().lower()
    operational = str(diag.get("operationalStatus") or "").strip().upper()
    page_state = ""
    browser_page_state = diag.get("browserPageState") or diag.get("browser_page_state") or {}
    if isinstance(browser_page_state, dict):
        page_state = str(browser_page_state.get("outcome") or browser_page_state.get("reason") or "").lower()
    # Do NOT json.dumps(diag): config keys like challengeStop falsely match "challenge".
    blob = " ".join([text, outcome, operational.lower(), page_state])

    # Chrome/Playwright launch failures are local infra, not marketplace challenges.
    if "could not be launched" in text or ("playwright" in text and "launch" in text):
        return "ERROR"

    if operational in {"EBAY_AUTH_REQUIRED", "AUTH_REQUIRED"} or outcome in {
        "authentication_required",
        "authentication_redirect",
    }:
        return "AUTH_REQUIRED"
    if operational in {"MARKETPLACE_CHALLENGE_REQUIRED", "CHALLENGE_REQUIRED"} or outcome in {
        "challenge_detected",
        "access_blocked",
        "marketplace_challenge_deferred",
    }:
        # Local ops deferral (not a live eBay challenge page) is a separate category.
        if outcome == "marketplace_challenge_deferred" or (
            "authentication was not attempted" in text and "challenge pages are not retried" in text
        ):
            return "DEFERRED"
        return "CHALLENGE_REQUIRED"
    if any(marker in blob for marker in CHALLENGE_MARKERS):
        if "authentication was not attempted" in text and "challenge pages are not retried" in text:
            return "DEFERRED"
        return "CHALLENGE_REQUIRED"
    if any(marker in blob for marker in AUTH_MARKERS):
        return "AUTH_REQUIRED"
    if "marketplace mismatch" in blob or "provider_marketplace_mismatch" in blob:
        return "ERROR"
    if any(marker in blob for marker in NO_COMP_MARKERS):
        return "NO_COMPS"
    if any(
        marker in blob
        for marker in (
            "ebay_sorry_error_page",
            "ebay_error_page",
            "marketplace_error_page",
            "temporary_ebay_server_failure",
            "ebay sorry",
            "pre_sold_sorry",
            "something went wrong on our end",
            "error page | ebay",
            "ebay_error_page",
        )
    ):
        return "TRANSIENT_EBAY"
    if fail_cls := str(diag.get("failureClass") or "").upper():
        if fail_cls in {"MARKETPLACE_ERROR_PAGE", "EBAY_ERROR_PAGE", "TARGET_REJECTED_UNHEALTHY_PAGE"}:
            return "TRANSIENT_EBAY"
    if not text:
        return "ERROR"
    return "ERROR"


@dataclass(frozen=True)
class MarketplaceCooldownState:
    market: str
    reason: str
    until: datetime
    last_error: str | None = None
    recorded_at: datetime | None = None
    incident_id: str | None = None

    def is_active(self, *, now: datetime | None = None) -> bool:
        current = now or utc_now()
        return self.until > current

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "market": self.market,
            "reason": self.reason,
            "until": utc_iso(self.until),
            "lastError": (self.last_error or "")[:300] or None,
            "recordedAt": utc_iso(self.recorded_at) if self.recorded_at else None,
        }
        if self.incident_id:
            payload["incidentId"] = self.incident_id
        return payload


def state_path() -> Path:
    raw = os.getenv("MARKET_OPS_STATE_PATH", "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else (REPORTS_DIR.parent / path)
    return STATE_PATH


def load_ops_state(*, path: Path | None = None) -> dict[str, Any]:
    """Load marketplace ops JSON.

    Missing file → empty markets bootstrap.
    Corrupt/unreadable → AtomicStateError (fail closed; never invent empty as success
    for operational gates that call get_active_cooldown after a hard read).
    """
    target = path or state_path()
    if not target.exists():
        return {"version": 1, "markets": {}}
    payload = read_json_object(target)
    markets = payload.get("markets")
    if not isinstance(markets, dict):
        payload["markets"] = {}
    return payload


def save_ops_state_unlocked(payload: dict[str, Any], *, path: Path | None = None) -> Path:
    """Replace ops JSON without acquiring the lock.

    ONLY call while the canonical ops FileLock is already held.
    Prefer save_ops_state() for public use.
    """
    target = path or state_path()
    clean = {
        "version": 1,
        "updatedAtUtc": utc_iso(),
        "markets": payload.get("markets") if isinstance(payload.get("markets"), dict) else {},
    }
    return atomic_write_json(target, clean)


def save_ops_state(
    payload: dict[str, Any],
    *,
    path: Path | None = None,
    expected_revision: int | None = None,
    force: bool = False,
) -> Path:
    """Public locked whole-document replacement of marketplace ops state.

    Blind replace of an existing revised document requires ``expected_revision``
    or ``force=True`` (tests/bootstrap).
    """
    assert_canonical_state_writer_domain_allowed()
    target = path or state_path()
    clean = {
        "version": 1,
        "updatedAtUtc": utc_iso(),
        "markets": payload.get("markets") if isinstance(payload.get("markets"), dict) else {},
    }
    with locked_json_state(target, default={"version": 1, "markets": {}}) as current:
        current_rev = int(current.get("revision") or 0)
        if current_rev > 0 and expected_revision is None and not force:
            raise AtomicStateError(
                f"stale_or_unversioned_ops_save_rejected:revision={current_rev}"
            )
        if expected_revision is not None and current_rev != int(expected_revision):
            raise AtomicStateError(
                f"stale_ops_revision:expected={expected_revision}:actual={current_rev}"
            )
        clean["revision"] = current_rev + 1
        current.clear()
        current.update(clean)
    return target


def _mutate_ops_state(
    mutator,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    assert_canonical_state_writer_domain_allowed()
    target = path or state_path()
    with locked_json_state(target, default={"version": 1, "markets": {}}) as payload:
        if not isinstance(payload.get("markets"), dict):
            payload["markets"] = {}
        mutator(payload)
        payload["version"] = 1
        payload["updatedAtUtc"] = utc_iso()
        payload["revision"] = int(payload.get("revision") or 0) + 1
        return payload


def get_active_cooldown(
    market: str,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> MarketplaceCooldownState | None:
    """Return active cooldown for market, or None if absent/expired.

    Corrupt ops JSON raises AtomicStateError (fail closed for preflight gates).
    """
    normalized = str(market or "").strip().upper()
    if not normalized:
        return None
    payload = load_ops_state(path=path)
    row = (payload.get("markets") or {}).get(normalized)
    if not isinstance(row, dict):
        return None
    until = parse_utc(row.get("until"))
    if until is None:
        return None
    state = MarketplaceCooldownState(
        market=normalized,
        reason=str(row.get("reason") or "DEFERRED"),
        until=until,
        last_error=str(row.get("lastError") or "") or None,
        recorded_at=parse_utc(row.get("recordedAt")),
        incident_id=str(row.get("incidentId") or "") or None,
    )
    if not state.is_active(now=now):
        return None
    return state


def record_marketplace_cooldown(
    market: str,
    *,
    reason: str,
    message: str | None = None,
    hours: float | None = None,
    minutes: float | None = None,
    now: datetime | None = None,
    path: Path | None = None,
    incident_id: str | None = None,
) -> MarketplaceCooldownState:
    normalized = str(market or "").strip().upper()
    current = now or utc_now()
    category = reason.strip().upper()
    if minutes is not None:
        delta = timedelta(minutes=max(1.0, float(minutes)))
    else:
        if hours is None:
            if category == "AUTH_REQUIRED":
                hours = float(os.getenv("MARKET_AUTH_COOLDOWN_HOURS", str(DEFAULT_AUTH_COOLDOWN_HOURS)))
            elif category == "TRANSIENT_EBAY":
                minutes = float(
                    os.getenv(
                        "EBAY_TRANSIENT_FAILURE_COOLDOWN_MINUTES",
                        str(DEFAULT_TRANSIENT_EBAY_COOLDOWN_MINUTES),
                    )
                )
                delta = timedelta(minutes=max(1.0, minutes))
                hours = None
            else:
                hours = float(
                    os.getenv("MARKET_CHALLENGE_COOLDOWN_HOURS", str(DEFAULT_CHALLENGE_COOLDOWN_HOURS))
                )
        if hours is not None:
            delta = timedelta(hours=max(0.25, float(hours)))
    state = MarketplaceCooldownState(
        market=normalized,
        reason=category,
        until=current + delta,
        last_error=(message or "")[:300] or None,
        recorded_at=current,
        incident_id=str(incident_id or "") or None,
    )

    def _apply(payload: dict[str, Any]) -> None:
        markets = dict(payload.get("markets") or {})
        markets[normalized] = state.to_dict()
        payload["markets"] = markets

    _mutate_ops_state(_apply, path=path)
    return state


def clear_marketplace_cooldown(market: str, *, path: Path | None = None) -> None:
    normalized = str(market or "").strip().upper()

    def _apply(payload: dict[str, Any]) -> None:
        markets = dict(payload.get("markets") or {})
        if normalized in markets:
            del markets[normalized]
            payload["markets"] = markets

    _mutate_ops_state(_apply, path=path)


def list_active_cooldowns(*, now: datetime | None = None, path: Path | None = None) -> dict[str, MarketplaceCooldownState]:
    current = now or utc_now()
    payload = load_ops_state(path=path)
    active: dict[str, MarketplaceCooldownState] = {}
    for market, row in (payload.get("markets") or {}).items():
        if not isinstance(row, dict):
            continue
        until = parse_utc(row.get("until"))
        if until is None or until <= current:
            continue
        active[str(market).upper()] = MarketplaceCooldownState(
            market=str(market).upper(),
            reason=str(row.get("reason") or "DEFERRED"),
            until=until,
            last_error=str(row.get("lastError") or "") or None,
            recorded_at=parse_utc(row.get("recordedAt")),
            incident_id=str(row.get("incidentId") or "") or None,
        )
    return active


def _incidents_path_for_ops(ops_path: Path | None) -> Path | None:
    """When ops state is redirected to an isolated path, co-locate incidents.

    Prevents tests that pass a temp ops path (but forget CONTROL_PLANE_INCIDENTS_PATH)
    from writing challenge rows into the canonical production ledger.
    """
    if ops_path is None:
        return None
    return Path(ops_path).resolve().parent / "control_plane_incidents.json"


def maybe_record_failure_cooldown(
    *,
    market: str,
    message: str | None,
    diagnostics: dict[str, Any] | None = None,
    now: datetime | None = None,
    path: Path | None = None,
    source_probe_id: str | None = None,
    source_attempt_id: str | None = None,
    incidents_path: Path | None = None,
) -> MarketplaceCooldownState | None:
    diag = diagnostics or {}
    provider_outcome = str(diag.get("providerOutcome") or "")
    # Gate reflections / preflight denials are NOT new observed marketplace challenges.
    # Recording them as CHALLENGE incidents pollutes the ledger (test and runtime).
    if provider_outcome in {
        "marketplace_ops_cooldown",
        "ebay_availability_cooldown",
        "ebay_availability_halt",
        "marketplace_ops_state_unreadable",
        "marketplace_challenge_deferred",
    }:
        return get_active_cooldown(market, now=now, path=path)
    msg_l = str(message or "").lower()
    if "eBay browser work deferred" in str(message or "") or "ebay_browser_work" in msg_l:
        return get_active_cooldown(market, now=now, path=path)
    if any(str(c).startswith("ACTIVE_CHALLENGE_INCIDENTS:") for c in (diag.get("ebayBrowserWorkGate") or {}).get("reasonCodes") or []):
        return get_active_cooldown(market, now=now, path=path)
    category = classify_provider_failure(message, diagnostics=diagnostics)
    if category not in {"AUTH_REQUIRED", "CHALLENGE_REQUIRED", "TRANSIENT_EBAY"}:
        return None
    existing = get_active_cooldown(market, now=now, path=path)
    # Challenge/auth take precedence over a shorter transient cooldown.
    if existing is not None and existing.reason in {"AUTH_REQUIRED", "CHALLENGE_REQUIRED"}:
        return existing
    if existing is not None and existing.reason == category:
        return existing
    from .control_plane_incidents import (
        INCIDENT_TYPE_AUTH,
        INCIDENT_TYPE_CHALLENGE,
        INCIDENT_TYPE_TRANSIENT_EBAY,
        new_incident_id,
        register_incident,
    )

    current = now or utc_now()
    incident_id = new_incident_id()
    if category == "AUTH_REQUIRED":
        incident_type = INCIDENT_TYPE_AUTH
    elif category == "TRANSIENT_EBAY":
        incident_type = INCIDENT_TYPE_TRANSIENT_EBAY
    else:
        incident_type = INCIDENT_TYPE_CHALLENGE
    kwargs: dict[str, Any] = {
        "reason": category,
        "message": message,
        "now": current,
        "path": path,
        "incident_id": incident_id,
    }
    if category == "TRANSIENT_EBAY":
        kwargs["minutes"] = float(
            os.getenv(
                "EBAY_TRANSIENT_FAILURE_COOLDOWN_MINUTES",
                str(DEFAULT_TRANSIENT_EBAY_COOLDOWN_MINUTES),
            )
        )
    state = record_marketplace_cooldown(market, **kwargs)
    # Isolation order:
    # 1) explicit incidents_path argument
    # 2) CONTROL_PLANE_INCIDENTS_PATH env (tests / redirects)
    # 3) co-locate beside redirected ops path (prevents production contamination)
    # 4) production default via register_incident(path=None)
    if incidents_path is not None:
        target_incidents = incidents_path
    elif os.environ.get("CONTROL_PLANE_INCIDENTS_PATH"):
        target_incidents = None
    else:
        target_incidents = _incidents_path_for_ops(path)
    register_incident(
        market=str(market or "").strip().upper(),
        incident_type=incident_type,
        classification=category,
        message=message,
        source_probe_id=source_probe_id,
        source_attempt_id=source_attempt_id,
        recorded_at=current,
        incident_id=incident_id,
        path=target_incidents,
        derived={
            "marketplaceCooldownReason": category,
            "marketplaceCooldownRecordedAt": utc_iso(state.recorded_at),
            "marketplaceCooldownUntil": utc_iso(state.until),
            "marketplaceCooldownIncidentId": incident_id,
            "availabilityState": "CHALLENGE_REQUIRED" if category == "CHALLENGE_REQUIRED" else None,
            "availabilityFailureReference": (message or "")[:300] or None,
        },
    )
    return state
