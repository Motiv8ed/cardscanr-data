"""Structured reliability-harness classification (no loose error-string matching)."""
from __future__ import annotations

from typing import Any

from .owned_daily_outcomes import (
    CHALLENGE_REQUIRED,
    CHECKED_NO_NEW_EXACT_EVIDENCE,
    TEMPORARY_BROWSER_FAILURE,
    UNCHANGED_FROM_EBAY,
    UPDATED_FROM_EBAY,
)
from .providers.post_sold_capture import POST_SOLD_CAPTURE_READY

# Production outcome / structured flag → reliability card verdict
PRODUCTION_TO_RELIABILITY_VERDICT: dict[str, str] = {
    UPDATED_FROM_EBAY: "PASS_PRICE_UPDATED",
    UNCHANGED_FROM_EBAY: "PASS_PRICE_UNCHANGED",
    CHECKED_NO_NEW_EXACT_EVIDENCE: "SAFE_NO_NEW_EXACT_EVIDENCE",
    CHALLENGE_REQUIRED: "STOP_CHALLENGE",
    TEMPORARY_BROWSER_FAILURE: "FAIL_NAVIGATION",
}


def _desktop_nav(diag: dict[str, Any]) -> dict[str, Any]:
    desktop = diag.get("desktopNav") or diag.get("linuxNav") or {}
    return desktop if isinstance(desktop, dict) else {}


def is_structured_challenge(
    result: dict[str, Any] | None,
    diag: dict[str, Any] | None = None,
) -> bool:
    """Challenge only from structured production evidence — never loose 'sold' text."""
    result = result or {}
    diag = diag or {}
    outcome = str(result.get("ownedDailyOutcome") or result.get("outcomeClass") or "")
    if outcome == CHALLENGE_REQUIRED:
        return True

    desktop = _desktop_nav(diag)
    if bool(desktop.get("challenge")) or bool(desktop.get("sorry")):
        return True

    if bool(result.get("challenge")) or bool(result.get("sorry")):
        return True

    provider_outcome = str(diag.get("providerOutcome") or "").lower()
    if provider_outcome in {
        "challenge_detected",
        "challenge_required",
        "sorry",
        "ebay_access_denied",
        "marketplace_blocked",
    }:
        return True

    operational = str(diag.get("operationalStatus") or "").upper()
    if operational in {"CHALLENGE_REQUIRED", "EBAY_CHALLENGE", "SORRY", "EBAY_ACCESS_DENIED_403"}:
        return True

    url = str(
        desktop.get("url")
        or diag.get("finalUrl")
        or result.get("sourceUrl")
        or ""
    ).lower()
    if any(token in url for token in ("splashui", "/sorry", "captcha", "challenge")):
        return True

    return False


def classify_reliability_card_verdict(
    result: dict[str, Any] | None,
    *,
    diag: dict[str, Any] | None = None,
    search_submission_started: bool,
    preflight_failed: bool = False,
) -> str:
    result = result or {}
    diag = diag or {}
    if preflight_failed:
        return "STOP_PREFLIGHT"
    if is_structured_challenge(result, diag):
        return "STOP_CHALLENGE"
    if not search_submission_started:
        # Local infra / dependency / pre-submit failure — not a marketplace attempt.
        outcome = str(result.get("ownedDailyOutcome") or result.get("outcomeClass") or "")
        if outcome == TEMPORARY_BROWSER_FAILURE or str(result.get("status") or "") in {"failed", "error"}:
            return "FAIL_NAVIGATION"
        return "STOP_PREFLIGHT"

    outcome = str(result.get("ownedDailyOutcome") or result.get("outcomeClass") or "")
    mapped = PRODUCTION_TO_RELIABILITY_VERDICT.get(outcome)
    if mapped and mapped != "FAIL_NAVIGATION":
        return mapped
    if outcome == TEMPORARY_BROWSER_FAILURE:
        # After a real submission, treat as navigation failure (not challenge).
        return "FAIL_NAVIGATION"

    status = str(result.get("status") or "")
    phase = result.get("postSoldCapturePhase") or diag.get("postSoldCapturePhase")
    sold = bool(result.get("x11SoldStateVerified") or diag.get("x11SoldStateVerified"))
    if outcome == UPDATED_FROM_EBAY or (status == "completed" and result.get("recommendedPrice") is not None):
        return "PASS_PRICE_UPDATED"
    if outcome == UNCHANGED_FROM_EBAY:
        return "PASS_PRICE_UNCHANGED"
    if outcome == CHECKED_NO_NEW_EXACT_EVIDENCE or status == "checked_no_new_exact_evidence":
        return "SAFE_NO_NEW_EXACT_EVIDENCE"
    if not sold:
        return "FAIL_SOLD_VERIFY"
    if phase and phase != POST_SOLD_CAPTURE_READY:
        return "FAIL_CAPTURE"
    if status in {"failed", "error"} or result.get("error"):
        err = str(result.get("error") or "").lower()
        if "parse" in err:
            return "FAIL_PARSE"
        if "capture" in err or "cdp" in err:
            return "FAIL_CAPTURE"
        if "nav" in err or "search" in err:
            return "FAIL_NAVIGATION"
        return "FAIL_LOCAL"
    return "FAIL_LOCAL"


__all__ = [
    "PRODUCTION_TO_RELIABILITY_VERDICT",
    "classify_reliability_card_verdict",
    "is_structured_challenge",
]
