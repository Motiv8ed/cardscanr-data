"""Recover INTER_CARD navigation when Chrome/CDP target sets change or rejections persist."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .atomic_json_state import atomic_write_json
from .browser_lifecycle_policy import (
    REASON_CHALLENGE,
    RUNTIME_COLD_START,
    PriorCardContext,
)
from .config import REPORTS_DIR
from .marketplace_gate_url import is_marketplace_gate_page
from .navigation_runtime_context import (
    INTER_CARD_EXPECTED_TARGET_REJECTED,
    NavigationRuntimeContext,
    _prior_without_target_id,
    bind_persisted_nav_context_path,
)

CDP_RECOVERY_STATE_PATH = REPORTS_DIR / "runtime" / "nav_cdp_recovery_state.json"
INTER_CARD_SELF_HEAL_RECEIPTS_PATH = REPORTS_DIR / "runtime" / "inter_card_self_heal_receipts.jsonl"
INTER_CARD_NO_REQUEST_STUCK_ALERT_HOURS = 2
INTER_CARD_NO_REQUEST_REJECTIONS_BEFORE_SELF_HEAL = max(
    1,
    int(os.getenv("INTER_CARD_NO_REQUEST_REJECTIONS_BEFORE_SELF_HEAL", "2")),
)
INTER_CARD_SELF_HEAL_MAX_PER_HOUR = max(
    1,
    int(os.getenv("INTER_CARD_SELF_HEAL_MAX_PER_HOUR", "3")),
)

MARKETPLACE_GATE_REASON_CODES = frozenset(
    {
        REASON_CHALLENGE,
        "INTER_CARD_CHALLENGE_TARGET",
        "CHALLENGE_REQUIRED",
        "EBAY_CHALLENGE_REQUIRED",
        "EBAY_AUTH_REQUIRED",
        "INTER_CARD_CHALLENGE",
        "EBAY_RATE_LIMITED",
    }
)

_SELF_HEAL_RATE_LIMIT_MARKERS = frozenset({"429", "RATE_LIMIT", "TOO_MANY_REQUESTS"})


def enrich_policy_diagnostics(diagnostics: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten desktopNav.search policy fields for job_runner / inter-card observers."""
    diag = dict(diagnostics or {})
    for container_key in ("desktopNav",):
        container = diag.get(container_key)
        if not isinstance(container, dict):
            continue
        search = container.get("search")
        if not isinstance(search, dict):
            continue
        for key in ("resultCode", "urlBeforeSearch", "titleBeforeSearch", "targetPolicy"):
            if diag.get(key) in (None, "") and search.get(key) not in (None, ""):
                diag[key] = search.get(key)
    search = diag.get("search")
    if isinstance(search, dict):
        for key in ("resultCode", "urlBeforeSearch", "titleBeforeSearch", "targetPolicy"):
            if diag.get(key) in (None, "") and search.get(key) not in (None, ""):
                diag[key] = search.get(key)
    return diag


def extract_policy_reason_codes(
    *,
    result_code: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> list[str]:
    codes: list[str] = []
    if result_code:
        codes.extend(part.strip() for part in str(result_code).split(",") if part.strip())
    diag = enrich_policy_diagnostics(diagnostics if isinstance(diagnostics, dict) else {})
    nested = diag.get("diagnostics") if isinstance(diag.get("diagnostics"), dict) else diag
    for key in ("resultCode",):
        rc = diag.get(key)
        if rc:
            codes.extend(part.strip() for part in str(rc).split(",") if part.strip())
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
    target_policy = diag.get("targetPolicy")
    if isinstance(target_policy, dict):
        for item in target_policy.get("reason_codes") or target_policy.get("reasonCodes") or []:
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime | None = None) -> str:
    when = dt or _utc_now()
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc_iso(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _self_heals_in_last_hour(state: dict[str, Any], *, now: datetime) -> int:
    window_start = now - timedelta(hours=1)
    count = 0
    for row in state.get("selfHealReceipts") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "") != "EXECUTED":
            continue
        at = _parse_utc_iso(str(row.get("atUtc") or ""))
        if at is not None and at >= window_start:
            count += 1
    return count


def _append_self_heal_receipt(payload: dict[str, Any]) -> None:
    from .smoke_utils import append_jsonl

    INTER_CARD_SELF_HEAL_RECEIPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(INTER_CARD_SELF_HEAL_RECEIPTS_PATH, payload)


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
    err = str(runtime_error or "").strip().upper()
    if err in {"EBAY_RATE_LIMITED", "CHALLENGE_REQUIRED"}:
        return True
    blob = f"{raw_error or ''} {page_url or ''} {page_title or ''}".upper()
    if any(marker in blob for marker in _SELF_HEAL_RATE_LIMIT_MARKERS):
        return True
    return is_marketplace_gate_page(page_url, title=page_title)


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


def _persist_cold_start_nav_context(ctx: NavigationRuntimeContext) -> None:
    ctx.runtime_mode = RUNTIME_COLD_START
    ctx.expected_prior = None
    ctx.prior_handoff_stale = True
    dest = bind_persisted_nav_context_path()
    payload = ctx.to_dict()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _close_pricer_ebay_tabs_for_cold_start() -> dict[str, Any]:
    """Close eBay pages only on the CardScanR pricer CDP endpoint (single profile/port)."""
    from .local_browser_runtime import DEFAULT_CDP_PORT, cold_start_close_leftover_ebay_tabs

    cdp_port = int(os.getenv("EBAY_BROWSER_CDP_PORT", str(DEFAULT_CDP_PORT)))
    return cold_start_close_leftover_ebay_tabs(cdp_port=cdp_port)


def execute_inter_card_leftover_tab_self_heal(
    ctx: NavigationRuntimeContext,
    *,
    state: dict[str, Any],
    trigger: str = INTER_CARD_EXPECTED_TARGET_REJECTED,
) -> dict[str, Any] | None:
    """Close pricer eBay tabs, persist COLD_START, emit a durable receipt (hourly cap)."""
    now = _utc_now()
    now_iso = _utc_iso(now)
    heals_1h = _self_heals_in_last_hour(state, now=now)
    job_id = str(ctx.current_job_id or "").strip() or None
    idempotency_key = str(state.get("lastSelfHealIdempotencyKey") or "")
    fingerprint = "unknown"
    try:
        from .local_browser_runtime import DEFAULT_CDP_PORT, probe_local_browser_runtime

        cdp_port = int(os.getenv("EBAY_BROWSER_CDP_PORT", str(DEFAULT_CDP_PORT)))
        browser = probe_local_browser_runtime(cdp_port=cdp_port)
        fingerprint = _fingerprint_cdp_targets(list(browser.raw_targets or []))
    except Exception:
        fingerprint = "unknown"
    new_key = f"{trigger}:{job_id}:{fingerprint}"
    if idempotency_key == new_key:
        return None
    if heals_1h >= INTER_CARD_SELF_HEAL_MAX_PER_HOUR:
        capped = {
            "receiptId": f"inter-card-self-heal-cap-{uuid.uuid4().hex[:12]}",
            "kind": "INTER_CARD_LEFTOVER_TAB_SELF_HEAL",
            "status": "CAP_EXCEEDED",
            "atUtc": now_iso,
            "jobId": job_id,
            "healsInLastHour": heals_1h,
            "maxPerHour": INTER_CARD_SELF_HEAL_MAX_PER_HOUR,
            "trigger": trigger,
        }
        state.setdefault("selfHealReceipts", [])
        if isinstance(state["selfHealReceipts"], list):
            state["selfHealReceipts"].append(capped)
        state["interCardSelfHealStatus"] = "CAP_EXCEEDED"
        state["interCardSelfHealCapExceededAtUtc"] = now_iso
        _append_self_heal_receipt(capped)
        return capped
    cleanup = _close_pricer_ebay_tabs_for_cold_start()
    _persist_cold_start_nav_context(ctx)
    receipt = {
        "receiptId": f"inter-card-self-heal-{uuid.uuid4().hex[:12]}",
        "kind": "INTER_CARD_LEFTOVER_TAB_SELF_HEAL",
        "status": "EXECUTED",
        "atUtc": now_iso,
        "jobId": job_id,
        "trigger": trigger,
        "resetMethod": cleanup.get("resetMethod"),
        "closedTargetIds": list(cleanup.get("closedTargetIds") or []),
        "closeErrors": list(cleanup.get("closeErrors") or []),
        "coldStartPolicyOk": bool((cleanup.get("coldStartPolicy") or {}).get("ok")),
        "healsInLastHour": heals_1h + 1,
        "maxPerHour": INTER_CARD_SELF_HEAL_MAX_PER_HOUR,
    }
    state.setdefault("selfHealReceipts", [])
    if isinstance(state["selfHealReceipts"], list):
        state["selfHealReceipts"].append(receipt)
    state["lastSelfHealIdempotencyKey"] = new_key
    state["interCardSelfHealStatus"] = "EXECUTED"
    state.pop("interCardSelfHealCapExceededAtUtc", None)
    _append_self_heal_receipt(receipt)
    return receipt


def inter_card_self_heal_status() -> dict[str, Any]:
    state = _load_state()
    return {
        "status": str(state.get("interCardSelfHealStatus") or "IDLE"),
        "capExceededAtUtc": state.get("interCardSelfHealCapExceededAtUtc"),
        "healsInLastHour": _self_heals_in_last_hour(state, now=_utc_now()),
        "maxPerHour": INTER_CARD_SELF_HEAL_MAX_PER_HOUR,
        "consecutiveNoRequestRejections": int(state.get("consecutiveInterCardNoRequestRejections") or 0),
    }


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
) -> tuple[NavigationRuntimeContext, str | None, dict[str, Any] | None]:
    """Track no-request rejections; self-heal after consecutive stuck-tab rejections."""
    policy_codes = extract_policy_reason_codes(result_code=result_code, diagnostics=diagnostics)
    state = _load_state()
    if search_submission_started:
        state["consecutiveInterCardNoRequestRejections"] = 0
        state.pop("stuckNoRequestSinceUtc", None)
        _save_state(state)
        return ctx, None, None
    if _marketplace_gate_blocks_progress(
        runtime_error=runtime_error,
        raw_error=raw_error,
        policy_reason_codes=policy_codes,
        page_url=page_url,
        page_title=page_title,
    ):
        _save_state(state)
        return ctx, "CHALLENGE_REQUIRED", None
    if not _is_stuck_tab_rejection(
        runtime_error=runtime_error,
        raw_error=raw_error,
        policy_reason_codes=policy_codes,
        page_url=page_url,
        page_title=page_title,
    ):
        return ctx, None, None
    count = int(state.get("consecutiveInterCardNoRequestRejections") or 0) + 1
    state["consecutiveInterCardNoRequestRejections"] = count
    latch: str | None = None
    self_heal_receipt: dict[str, Any] | None = None
    if count >= INTER_CARD_NO_REQUEST_REJECTIONS_BEFORE_SELF_HEAL:
        state["consecutiveInterCardNoRequestRejections"] = 0
        self_heal_receipt = execute_inter_card_leftover_tab_self_heal(
            ctx,
            state=state,
            trigger=INTER_CARD_EXPECTED_TARGET_REJECTED,
        )
        if self_heal_receipt and self_heal_receipt.get("status") == "CAP_EXCEEDED":
            _persist_cold_start_nav_context(ctx)
    _save_state(state)
    return ctx, latch, self_heal_receipt


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