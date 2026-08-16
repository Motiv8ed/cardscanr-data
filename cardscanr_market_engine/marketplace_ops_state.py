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

SUPPORTED_LIVE_MARKETS = ("AU", "US", "GB", "CA")
DEFAULT_AUTH_COOLDOWN_HOURS = 6
DEFAULT_CHALLENGE_COOLDOWN_HOURS = 12
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
    blob = " ".join([text, outcome, operational.lower(), json.dumps(diag, sort_keys=True).lower()])

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

    def is_active(self, *, now: datetime | None = None) -> bool:
        current = now or utc_now()
        return self.until > current

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "reason": self.reason,
            "until": utc_iso(self.until),
            "lastError": (self.last_error or "")[:300] or None,
            "recordedAt": utc_iso(self.recorded_at) if self.recorded_at else None,
        }


def state_path() -> Path:
    raw = os.getenv("MARKET_OPS_STATE_PATH", "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else (REPORTS_DIR.parent / path)
    return STATE_PATH


def load_ops_state(*, path: Path | None = None) -> dict[str, Any]:
    target = path or state_path()
    if not target.exists():
        return {"version": 1, "markets": {}}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "markets": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "markets": {}}
    markets = payload.get("markets")
    if not isinstance(markets, dict):
        payload["markets"] = {}
    return payload


def save_ops_state(payload: dict[str, Any], *, path: Path | None = None) -> Path:
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = {
        "version": 1,
        "updatedAtUtc": utc_iso(),
        "markets": payload.get("markets") if isinstance(payload.get("markets"), dict) else {},
    }
    target.write_text(json.dumps(clean, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def get_active_cooldown(
    market: str,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> MarketplaceCooldownState | None:
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
    now: datetime | None = None,
    path: Path | None = None,
) -> MarketplaceCooldownState:
    normalized = str(market or "").strip().upper()
    current = now or utc_now()
    category = reason.strip().upper()
    if hours is None:
        if category == "AUTH_REQUIRED":
            hours = float(os.getenv("MARKET_AUTH_COOLDOWN_HOURS", str(DEFAULT_AUTH_COOLDOWN_HOURS)))
        else:
            hours = float(os.getenv("MARKET_CHALLENGE_COOLDOWN_HOURS", str(DEFAULT_CHALLENGE_COOLDOWN_HOURS)))
    hours = max(0.25, float(hours))
    state = MarketplaceCooldownState(
        market=normalized,
        reason=category,
        until=current + timedelta(hours=hours),
        last_error=(message or "")[:300] or None,
        recorded_at=current,
    )
    payload = load_ops_state(path=path)
    markets = dict(payload.get("markets") or {})
    markets[normalized] = state.to_dict()
    payload["markets"] = markets
    save_ops_state(payload, path=path)
    return state


def clear_marketplace_cooldown(market: str, *, path: Path | None = None) -> None:
    normalized = str(market or "").strip().upper()
    payload = load_ops_state(path=path)
    markets = dict(payload.get("markets") or {})
    if normalized in markets:
        del markets[normalized]
        payload["markets"] = markets
        save_ops_state(payload, path=path)


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
        )
    return active


def maybe_record_failure_cooldown(
    *,
    market: str,
    message: str | None,
    diagnostics: dict[str, Any] | None = None,
    now: datetime | None = None,
    path: Path | None = None,
) -> MarketplaceCooldownState | None:
    diag = diagnostics or {}
    if str(diag.get("providerOutcome") or "") == "marketplace_ops_cooldown":
        return get_active_cooldown(market, now=now, path=path)
    category = classify_provider_failure(message, diagnostics=diagnostics)
    if category not in {"AUTH_REQUIRED", "CHALLENGE_REQUIRED"}:
        return None
    existing = get_active_cooldown(market, now=now, path=path)
    if existing is not None and existing.reason == category:
        return existing
    return record_marketplace_cooldown(
        market,
        reason=category,
        message=message,
        now=now,
        path=path,
    )
