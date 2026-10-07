"""Canary / probe / continuous operation modes and market-scoped canary ledger.

Marketplace incidents (Sorry/Error) must persist even when continuous enablement
is false. Local runtime failures never consume canary search budget and never
open marketplace cooldown.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from .config import REPORTS_DIR
from .ebay_availability import peek_availability
from .owned_daily_outcomes import (
    CHALLENGE_REQUIRED,
    EBAY_ACCESS_DENIED_403,
    EBAY_AUTH_REQUIRED,
    TEMPORARY_BROWSER_FAILURE,
    TEMPORARY_EBAY_SERVER_FAILURE,
)
from .region_pricing_registry import is_region_dispatchable
from .scheduler import utc_iso, utc_now
from .x11_chrome_focus import is_local_runtime_failure_message

OperationMode = Literal["CONTINUOUS", "CANARY", "PROBE"]
OPERATION_MODE_ENV = "CARDSCANR_OPERATION_MODE"
CANARY_LEDGER_PATH = REPORTS_DIR / "runtime" / "canary_presubmit_transient_ledger.json"
MAX_PRESUBMIT_TRANSIENT_EPISODES_24H = 3

HARD_STOP_OUTCOMES = frozenset(
    {
        CHALLENGE_REQUIRED,
        EBAY_AUTH_REQUIRED,
        EBAY_ACCESS_DENIED_403,
        "UNACCOUNTED_SEARCH_URL_NAVIGATION",
        "STATE_INTEGRITY_FAILURE",
        "ACTIVE_CHALLENGE",
    }
)


def parse_operation_mode(raw: str | None = None) -> OperationMode:
    text = str(raw if raw is not None else os.getenv(OPERATION_MODE_ENV, "CONTINUOUS")).strip().upper()
    if text in {"CANARY", "PROBE", "CONTINUOUS"}:
        return text  # type: ignore[return-value]
    return "CONTINUOUS"


def set_operation_mode(mode: OperationMode) -> OperationMode:
    resolved = parse_operation_mode(mode)
    os.environ[OPERATION_MODE_ENV] = resolved
    return resolved


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


@dataclass
class CanaryMarketLedger:
    market: str
    pre_submit_transients: list[datetime]
    post_submit_transients: list[datetime]
    hard_stop: str | None = None
    last_outcome: str | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "preSubmitTransients": [utc_iso(t) for t in self.pre_submit_transients],
            "postSubmitTransients": [utc_iso(t) for t in self.post_submit_transients],
            "hardStop": self.hard_stop,
            "lastOutcome": self.last_outcome,
            "updatedAtUtc": utc_iso(self.updated_at or utc_now()),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None, *, market: str) -> "CanaryMarketLedger":
        data = payload or {}
        return cls(
            market=str(market).upper(),
            pre_submit_transients=[
                d for d in (_parse_dt(x) for x in data.get("preSubmitTransients") or []) if d
            ],
            post_submit_transients=[
                d for d in (_parse_dt(x) for x in data.get("postSubmitTransients") or []) if d
            ],
            hard_stop=str(data.get("hardStop") or "") or None,
            last_outcome=str(data.get("lastOutcome") or "") or None,
            updated_at=_parse_dt(data.get("updatedAtUtc")),
        )


def load_canary_ledger(*, path: Path | None = None) -> dict[str, CanaryMarketLedger]:
    target = path or CANARY_LEDGER_PATH
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    markets = payload.get("markets") if isinstance(payload, dict) else None
    if not isinstance(markets, dict):
        return {}
    out: dict[str, CanaryMarketLedger] = {}
    for key, row in markets.items():
        code = str(key).upper()
        if isinstance(row, dict):
            out[code] = CanaryMarketLedger.from_dict(row, market=code)
    return out


def save_canary_ledger(
    ledgers: dict[str, CanaryMarketLedger],
    *,
    path: Path | None = None,
) -> Path:
    target = path or CANARY_LEDGER_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "markets": {code: ledger.to_dict() for code, ledger in sorted(ledgers.items())},
        "updatedAtUtc": utc_iso(),
    }
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return target


def _prune(events: list[datetime], *, now: datetime, hours: int = 24) -> list[datetime]:
    cutoff = now - timedelta(hours=hours)
    return [t for t in events if t >= cutoff]


def pre_submit_transient_count(market: str, *, now: datetime | None = None, path: Path | None = None) -> int:
    current = now or utc_now()
    ledger = load_canary_ledger(path=path).get(str(market).upper())
    if ledger is None:
        return 0
    return len(_prune(ledger.pre_submit_transients, now=current))


def canary_may_continue(market: str, *, now: datetime | None = None, path: Path | None = None) -> dict[str, Any]:
    code = str(market).upper()
    if not is_region_dispatchable(code):
        return {
            "ok": False,
            "market": code,
            "reason": "BLOCKED_NEEDS_PROVIDER",
            "hardStop": True,
        }
    current = now or utc_now()
    ledger = load_canary_ledger(path=path).get(code) or CanaryMarketLedger(code, [], [])
    if ledger.hard_stop:
        return {
            "ok": False,
            "market": code,
            "reason": ledger.hard_stop,
            "hardStop": True,
            "preSubmitTransientEpisodes24h": len(_prune(ledger.pre_submit_transients, now=current)),
        }
    episodes = _prune(ledger.pre_submit_transients, now=current)
    if len(episodes) >= MAX_PRESUBMIT_TRANSIENT_EPISODES_24H:
        return {
            "ok": False,
            "market": code,
            "reason": "MAX_PRESUBMIT_TRANSIENT_EPISODES_24H",
            "hardStop": True,
            "preSubmitTransientEpisodes24h": len(episodes),
        }
    snap = peek_availability(market=code, now=current)
    if snap.state == "CHALLENGE_REQUIRED":
        return {
            "ok": False,
            "market": code,
            "reason": "CHALLENGE_REQUIRED",
            "hardStop": True,
            "availability": snap.state,
        }
    if snap.state == "COOLDOWN":
        return {
            "ok": False,
            "market": code,
            "reason": "COOLDOWN",
            "hardStop": False,
            "cooldown": True,
            "nextProbeAt": utc_iso(snap.next_probe_at) if snap.next_probe_at else None,
            "availability": snap.state,
            "preSubmitTransientEpisodes24h": len(episodes),
        }
    if snap.state == "PROBE_REQUIRED":
        return {
            "ok": True,
            "market": code,
            "reason": "PROBE_REQUIRED",
            "needsProbe": True,
            "availability": snap.state,
            "preSubmitTransientEpisodes24h": len(episodes),
        }
    return {
        "ok": True,
        "market": code,
        "reason": "READY",
        "needsProbe": False,
        "availability": snap.state,
        "preSubmitTransientEpisodes24h": len(episodes),
    }


def classify_canary_failure(
    *,
    outcome: str | None,
    error_message: str | None,
    search_submission_started: bool,
) -> dict[str, Any]:
    text = str(error_message or "")
    owned = str(outcome or "").strip().upper()
    local = is_local_runtime_failure_message(text) or owned == TEMPORARY_BROWSER_FAILURE and (
        "display :99" in text.lower()
        or "linux_chrome_cdp_failed" in text.lower()
        or "x11_" in text.lower()
    )
    text_l = text.lower()
    if (
        owned == EBAY_AUTH_REQUIRED
        or "ebay_auth_required" in text_l
        or "signin.ebay" in text_l
        or "authentication_required" in text_l
        or "provider_authentication_required" in text_l
    ):
        return {
            "kind": "HARD_STOP",
            "consumed": bool(search_submission_started),
            "opensMarketplaceCooldown": False,
            "localRuntime": False,
            "outcome": EBAY_AUTH_REQUIRED,
        }
    if owned in HARD_STOP_OUTCOMES or any(h.lower() in text_l for h in ("captcha", "challenge_required", "access_denied_403")):
        return {
            "kind": "HARD_STOP",
            "consumed": bool(search_submission_started),
            "opensMarketplaceCooldown": False,
            "localRuntime": False,
            "outcome": owned or CHALLENGE_REQUIRED,
        }
    if local and not search_submission_started:
        return {
            "kind": "LOCAL_RUNTIME_FAILURE",
            "consumed": False,
            "opensMarketplaceCooldown": False,
            "localRuntime": True,
            "outcome": TEMPORARY_BROWSER_FAILURE,
        }
    if owned == TEMPORARY_EBAY_SERVER_FAILURE or "ebay_sorry" in text.lower() or "error page" in text.lower():
        return {
            "kind": "PRE_SUBMIT_TRANSIENT" if not search_submission_started else "POST_SUBMIT_TRANSIENT",
            "consumed": bool(search_submission_started),
            "opensMarketplaceCooldown": True,
            "localRuntime": False,
            "outcome": TEMPORARY_EBAY_SERVER_FAILURE,
        }
    if search_submission_started:
        return {
            "kind": "POST_SUBMIT_TRANSIENT",
            "consumed": True,
            "opensMarketplaceCooldown": owned == TEMPORARY_EBAY_SERVER_FAILURE,
            "localRuntime": False,
            "outcome": owned or TEMPORARY_BROWSER_FAILURE,
        }
    return {
        "kind": "PRE_SUBMIT_TRANSIENT" if owned == TEMPORARY_EBAY_SERVER_FAILURE else "LOCAL_OR_OTHER",
        "consumed": False,
        "opensMarketplaceCooldown": owned == TEMPORARY_EBAY_SERVER_FAILURE,
        "localRuntime": owned == TEMPORARY_BROWSER_FAILURE,
        "outcome": owned or TEMPORARY_BROWSER_FAILURE,
    }


def record_canary_episode(
    market: str,
    *,
    kind: str,
    outcome: str | None,
    now: datetime | None = None,
    path: Path | None = None,
) -> CanaryMarketLedger:
    code = str(market).upper()
    current = now or utc_now()
    ledgers = load_canary_ledger(path=path)
    ledger = ledgers.get(code) or CanaryMarketLedger(code, [], [])
    ledger.pre_submit_transients = _prune(ledger.pre_submit_transients, now=current)
    ledger.post_submit_transients = _prune(ledger.post_submit_transients, now=current)
    ledger.last_outcome = outcome
    ledger.updated_at = current
    if kind == "HARD_STOP":
        ledger.hard_stop = outcome or "HARD_STOP"
    elif kind == "PRE_SUBMIT_TRANSIENT":
        ledger.pre_submit_transients.append(current)
    elif kind == "POST_SUBMIT_TRANSIENT":
        ledger.post_submit_transients.append(current)
    ledgers[code] = ledger
    save_canary_ledger(ledgers, path=path)
    return ledger


def operation_mode_allows_marketplace_persistence(mode: OperationMode | str | None = None) -> bool:
    """Sorry/cooldown persistence is required for CONTINUOUS, CANARY, and PROBE."""
    resolved = parse_operation_mode(mode if isinstance(mode, str) else None)
    return resolved in {"CONTINUOUS", "CANARY", "PROBE"}


__all__ = [
    "CANARY_LEDGER_PATH",
    "HARD_STOP_OUTCOMES",
    "MAX_PRESUBMIT_TRANSIENT_EPISODES_24H",
    "OPERATION_MODE_ENV",
    "CanaryMarketLedger",
    "canary_may_continue",
    "classify_canary_failure",
    "load_canary_ledger",
    "operation_mode_allows_marketplace_persistence",
    "parse_operation_mode",
    "pre_submit_transient_count",
    "record_canary_episode",
    "save_canary_ledger",
    "set_operation_mode",
]
