"""Recover INTER_CARD navigation when Chrome/CDP target sets change or rejections persist."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .atomic_json_state import atomic_write_json
from .browser_lifecycle_policy import RUNTIME_COLD_START, PriorCardContext
from .config import REPORTS_DIR
from .navigation_runtime_context import (
    INTER_CARD_EXPECTED_TARGET_REJECTED,
    NavigationRuntimeContext,
    _prior_without_target_id,
    bind_persisted_nav_context_path,
)

CDP_RECOVERY_STATE_PATH = REPORTS_DIR / "runtime" / "nav_cdp_recovery_state.json"
INTER_CARD_NO_REQUEST_STUCK_ALERT_HOURS = 2


def _fingerprint_cdp_targets(raw_targets: list[dict[str, Any]]) -> str:
    try:
        from .providers.ebay_browser_provider import browser_session_recycle_events
    except Exception:
        browser_session_recycle_events = lambda: 0  # type: ignore[assignment]
    ids = sorted(str(row.get("id") or "") for row in raw_targets if isinstance(row, dict) and row.get("id"))
    return f"r{int(browser_session_recycle_events())}:{','.join(ids)}"


def _load_state(path: Path | None = None) -> dict[str, Any]:
    target = path or CDP_RECOVERY_STATE_PATH
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(payload: dict[str, Any], path: Path | None = None) -> None:
    target = path or CDP_RECOVERY_STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, payload)


def reconcile_nav_context_for_cdp_change(ctx: NavigationRuntimeContext) -> NavigationRuntimeContext:
    """Clear stale expected CDP target when Chrome session/target set changes."""
    try:
        from .local_browser_runtime import probe_local_browser_runtime
    except Exception:
        return ctx
    try:
        browser = probe_local_browser_runtime()
        fingerprint = _fingerprint_cdp_targets(list(browser.raw_targets or []))
    except Exception:
        return ctx
    state = _load_state()
    previous = str(state.get("cdpTargetSetFingerprint") or "")
    if (
        previous
        and previous != fingerprint
        and ctx.expected_prior is not None
        and ctx.expected_prior.target_id
    ):
        ctx.expected_prior = _prior_without_target_id(ctx.expected_prior)
        ctx.prior_handoff_stale = True
    state["cdpTargetSetFingerprint"] = fingerprint
    _save_state(state)
    return ctx


def observe_inter_card_no_request_outcome(
    ctx: NavigationRuntimeContext,
    *,
    runtime_error: str | None,
    search_submission_started: bool,
) -> NavigationRuntimeContext:
    """Track rejections that never reached eBay; force COLD_START after two in a row."""
    state = _load_state()
    if search_submission_started:
        state["consecutiveInterCardNoRequestRejections"] = 0
        state.pop("stuckNoRequestSinceUtc", None)
        _save_state(state)
        return ctx
    code = str(runtime_error or "").strip().upper()
    if code != INTER_CARD_EXPECTED_TARGET_REJECTED:
        return ctx
    count = int(state.get("consecutiveInterCardNoRequestRejections") or 0) + 1
    state["consecutiveInterCardNoRequestRejections"] = count
    if count >= 2:
        ctx.runtime_mode = RUNTIME_COLD_START
        ctx.expected_prior = None
        ctx.prior_handoff_stale = True
        state["consecutiveInterCardNoRequestRejections"] = 0
        dest = bind_persisted_nav_context_path()
        payload = ctx.to_dict()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _save_state(state)
    return ctx


def inter_card_stuck_no_request_alert(*, now_iso: str, submissions_1h: int) -> dict[str, Any] | None:
    """Return alert payload when no live requests for >2h while rejections accumulate."""
    state = _load_state()
    since = str(state.get("stuckNoRequestSinceUtc") or "").strip()
    if submissions_1h > 0:
        state.pop("stuckNoRequestSinceUtc", None)
        _save_state(state)
        return None
    rejections = int(state.get("consecutiveInterCardNoRequestRejections") or 0)
    if rejections < 1:
        return None
    if not since:
        state["stuckNoRequestSinceUtc"] = now_iso
        _save_state(state)
        return None
    return {
        "kind": "INTER_CARD_NO_REQUEST_STUCK",
        "sinceUtc": since,
        "rejections": rejections,
        "alertAfterHours": INTER_CARD_NO_REQUEST_STUCK_ALERT_HOURS,
    }


def prior_from_context(ctx: NavigationRuntimeContext) -> PriorCardContext | None:
    return ctx.expected_prior