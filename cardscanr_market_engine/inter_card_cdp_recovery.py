"""Recover INTER_CARD navigation when Chrome/CDP target sets change or rejections persist."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .atomic_json_state import atomic_write_json
from .browser_lifecycle_policy import (
    REASON_CHALLENGE,
    RUNTIME_COLD_START,
    PriorCardContext,
)
from .config import REPORTS_DIR
from .navigation_runtime_context import (
    INTER_CARD_EXPECTED_TARGET_REJECTED,
    NavigationRuntimeContext,
    _prior_without_target_id,
    bind_persisted_nav_context_path,
)

CDP_RECOVERY_STATE_PATH = REPORTS_DIR / "runtime" / "nav_cdp_recovery_state.json"
INTER_CARD_NO_REQUEST_STUCK_ALERT_HOURS = 2

MARKETPLACE_GATE_REASON_CODES = frozenset(
    {
        REASON_CHALLENGE,
        "INTER_CARD_CHALLENGE_TARGET",
        "CHALLENGE_REQUIRED",
        "EBAY_CHALLENGE_REQUIRED",
        "EBAY_AUTH_REQUIRED",
        "INTER_CARD_CHALLENGE",
    }
)

MARKETPLACE_GATE_URL_MARKERS = (
    "splashui/challenge",
    "challenge",
    "captcha",
    "ebay_sorry",
    "/sorry",
    "signin.ebay",
    "signin",
    "auth",
)


def extract_policy_reason_codes(
    *,
    result_code: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> list[str]:
    codes: list[str] = []
    if result_code:
        codes.extend(part.strip() for part in str(result_code).split(",") if part.strip())
    diag = diagnostics if isinstance(diagnostics, dict) else {}
    nested = diag.get("diagnostics") if isinstance(diag.get("diagnostics"), dict) else diag
    if isinstance(nested, dict):
        rc = nested.get("resultCode")
        if rc:
            codes.extend(part.strip() for part in str(rc).split(",") if part.strip())
        policy = nested.get("targetPolicy")
        if isinstance(policy, dict):
            for item in policy.get("reason_codes") or policy.get("reasonCodes") or []:
                text = str(item or "").strip()
                if text:
                    codes.append(text)
    return sorted(set(codes))


def _top_level_page_ids(raw_targets: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for row in raw_targets:
        if not isinstance(row, dict):
            continue
        if str(row.get("type") or "page").lower() != "page":
            continue
        tid = str(row.get("id") or "").strip()
        if tid:
            ids.append(tid)
    return sorted(ids)


def _recycle_generation(fingerprint: str) -> int:
    text = str(fingerprint or "")
    if not text.startswith("r") or ":" not in text:
        return 0
    head = text.split(":", 1)[0]
    try:
        return int(head[1:])
    except ValueError:
        return 0


def _fingerprint_cdp_targets(raw_targets: list[dict[str, Any]]) -> str:
    try:
        from .providers.ebay_browser_provider import browser_session_recycle_events
    except Exception:
        browser_session_recycle_events = lambda: 0  # type: ignore[assignment]
    ids = _top_level_page_ids(raw_targets)
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
    """Clear stale expected CDP target only after browser recycle with missing expected tab."""
    try:
        from .local_browser_runtime import probe_local_browser_runtime
    except Exception:
        return ctx
    try:
        browser = probe_local_browser_runtime()
        raw = list(browser.raw_targets or [])
        fingerprint = _fingerprint_cdp_targets(raw)
        top_ids = set(_top_level_page_ids(raw))
    except Exception:
        return ctx
    state = _load_state()
    previous = str(state.get("cdpTopLevelFingerprint") or state.get("cdpTargetSetFingerprint") or "")
    recycled = _recycle_generation(fingerprint) > _recycle_generation(previous)
    if (
        previous
        and previous != fingerprint
        and recycled
        and ctx.expected_prior is not None
        and ctx.expected_prior.target_id
    ):
        expected_id = str(ctx.expected_prior.target_id)
        if expected_id not in top_ids:
            ctx.expected_prior = _prior_without_target_id(ctx.expected_prior)
            ctx.prior_handoff_stale = True
    state["cdpTopLevelFingerprint"] = fingerprint
    _save_state(state)
    return ctx


def _marketplace_gate_blocks_progress(
    *,
    runtime_error: str | None,
    raw_error: str | None,
    policy_reason_codes: list[str] | None = None,
    page_url: str | None = None,
    page_title: str | None = None,
) -> bool:
    codes = {str(c).strip().upper() for c in (policy_reason_codes or []) if str(c).strip()}
    if codes & {c.upper() for c in MARKETPLACE_GATE_REASON_CODES}:
        return True
    blob = f"{runtime_error or ''} {raw_error or ''}".lower()
    if any(marker in blob for marker in ("inter_card_challenge_target", "challenge_required", "ebay_auth")):
        return True
    page_blob = f"{page_url or ''} {page_title or ''}".lower()
    if any(marker in page_blob for marker in MARKETPLACE_GATE_URL_MARKERS):
        return True
    return False


def _is_stuck_tab_rejection(
    *,
    runtime_error: str | None,
    raw_error: str | None,
    policy_reason_codes: list[str] | None = None,
    page_url: str | None = None,
    page_title: str | None = None,
) -> bool:
    if _marketplace_gate_blocks_progress(
        runtime_error=runtime_error,
        raw_error=raw_error,
        policy_reason_codes=policy_reason_codes,
        page_url=page_url,
        page_title=page_title,
    ):
        return False
    code = str(runtime_error or "").strip().upper()
    if code == INTER_CARD_EXPECTED_TARGET_REJECTED:
        return True
    blob = str(raw_error or "").lower()
    return "inter_card_expected_target_rejected" in blob


def observe_inter_card_no_request_outcome(
    ctx: NavigationRuntimeContext,
    *,
    runtime_error: str | None,
    search_submission_started: bool,
    raw_error: str | None = None,
    result_code: str | None = None,
    diagnostics: dict[str, Any] | None = None,
    page_url: str | None = None,
    page_title: str | None = None,
) -> tuple[NavigationRuntimeContext, str | None]:
    """Track no-request rejections; force COLD_START after two stuck-tab rejections in a row."""
    policy_codes = extract_policy_reason_codes(result_code=result_code, diagnostics=diagnostics)
    state = _load_state()
    if search_submission_started:
        state["consecutiveInterCardNoRequestRejections"] = 0
        state.pop("stuckNoRequestSinceUtc", None)
        _save_state(state)
        return ctx, None
    if _marketplace_gate_blocks_progress(
        runtime_error=runtime_error,
        raw_error=raw_error,
        policy_reason_codes=policy_codes,
        page_url=page_url,
        page_title=page_title,
    ):
        _save_state(state)
        return ctx, "CHALLENGE_REQUIRED"
    if not _is_stuck_tab_rejection(
        runtime_error=runtime_error,
        raw_error=raw_error,
        policy_reason_codes=policy_codes,
        page_url=page_url,
        page_title=page_title,
    ):
        return ctx, None
    count = int(state.get("consecutiveInterCardNoRequestRejections") or 0) + 1
    state["consecutiveInterCardNoRequestRejections"] = count
    latch: str | None = None
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
    return ctx, latch


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