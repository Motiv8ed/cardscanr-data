"""Owned-daily refresh outcome taxonomy (operational health, not pricing math).

Daily refresh health must distinguish successful market checks that retain
last-known-good from browser/system failures.
"""
from __future__ import annotations

from typing import Any

LOCAL_GUI_FAILURE = "LOCAL_GUI_FAILURE"  # not exported as outcome name historically
# Local surface-state leak (e.g. typed into eBay Live) — not SORRY/403.
LOCAL_SEARCH_SURFACE_STATE_LEAK = "LOCAL_SEARCH_SURFACE_STATE_LEAK"
LOCAL_SEARCH_SURFACE_RECOVERY_FAILED = "LOCAL_SEARCH_SURFACE_RECOVERY_FAILED"
UNACCOUNTED_SEARCH_URL_NAVIGATION = "UNACCOUNTED_SEARCH_URL_NAVIGATION"
UPDATED_FROM_EBAY = "UPDATED_FROM_EBAY"
UNCHANGED_FROM_EBAY = "UNCHANGED_FROM_EBAY"
CHECKED_NO_NEW_EXACT_EVIDENCE = "CHECKED_NO_NEW_EXACT_EVIDENCE"
TEMPORARY_BROWSER_FAILURE = "TEMPORARY_BROWSER_FAILURE"
TEMPORARY_EBAY_SERVER_FAILURE = "TEMPORARY_EBAY_SERVER_FAILURE"
POST_SOLD_CAPTURE_FAILURE = "POST_SOLD_CAPTURE_FAILURE"
FINALIZE_TIMEOUT_SAFE = "FINALIZE_TIMEOUT_SAFE"
ALTERNATE_EBAY_SURFACE = "ALTERNATE_EBAY_SURFACE"
EBAY_LIVE_RESULTS = "EBAY_LIVE_RESULTS"
EBAY_ACCESS_DENIED_403 = "EBAY_ACCESS_DENIED_403"
CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"
EBAY_CHALLENGE_REQUIRED = "EBAY_CHALLENGE_REQUIRED"
PRE_FLIGHT_CONTROL_PLANE_BLOCKED = "PRE_FLIGHT_CONTROL_PLANE_BLOCKED"
NO_PRICE_EVER_FOUND = "NO_PRICE_EVER_FOUND"

# Transient eBay infrastructure (SORRY / Error Page) — not local GUI defects.
TRANSIENT_EBAY_FAILURE_OUTCOMES = frozenset(
    {
        TEMPORARY_EBAY_SERVER_FAILURE,
        TEMPORARY_BROWSER_FAILURE,
        FINALIZE_TIMEOUT_SAFE,
        EBAY_ACCESS_DENIED_403,
        POST_SOLD_CAPTURE_FAILURE,
    }
)

# Alternate eBay surfaces (e.g. Live) — retain last-good, do not trip SORRY breaker.
ALTERNATE_SURFACE_OUTCOMES = frozenset(
    {
        ALTERNATE_EBAY_SURFACE,
        EBAY_LIVE_RESULTS,
    }
)

# Local search-origin defects (Live scope leak, recovery fail) — not eBay 403/SORRY.
LOCAL_SURFACE_OUTCOMES = frozenset(
    {
        LOCAL_SEARCH_SURFACE_STATE_LEAK,
        LOCAL_SEARCH_SURFACE_RECOVERY_FAILED,
    }
)

# Successful / healthy operational outcomes (not infrastructure failures).
HEALTHY_CHECK_OUTCOMES = frozenset(
    {
        UPDATED_FROM_EBAY,
        UNCHANGED_FROM_EBAY,
        CHECKED_NO_NEW_EXACT_EVIDENCE,
    }
)

SPARSE_EVIDENCE_REASONS = frozenset(
    {
        "no_clean_exact_comps",
        "all_comps_rejected",
        "no_comps_parsed",
        "stale_evidence_only",
        "stale_single_comp_only",
    }
)


def is_sparse_no_new_evidence_reason(reason: str | None) -> bool:
    text = str(reason or "").strip().lower()
    if not text:
        return False
    if text in SPARSE_EVIDENCE_REASONS:
        return True
    return any(marker in text for marker in SPARSE_EVIDENCE_REASONS)


def classify_completed_ebay_write(
    *,
    prior_price: float | None,
    new_price: float,
) -> str:
    if prior_price is not None and prior_price > 0 and abs(float(new_price) - float(prior_price)) < 1e-9:
        return UNCHANGED_FROM_EBAY
    return UPDATED_FROM_EBAY


def classify_exception_outcome(
    exc: BaseException | str | None,
    *,
    diagnostics: dict[str, Any] | None = None,
) -> str:
    """Map provider/job exceptions to owned-daily operational outcomes."""
    from .marketplace_ops_state import classify_provider_failure
    from .providers.errors import (
        ProviderAuthenticationRequiredError,
        ProviderBlockedError,
        ProviderTemporaryError,
    )

    text = str(exc or "").lower()
    diag = diagnostics or {}
    reason = str(diag.get("reason") or "").lower()
    provider_outcome = str(diag.get("providerOutcome") or "").strip().lower()
    fail_cls_early = str(diag.get("failureClass") or diag.get("terminal") or "").upper()
    if (
        fail_cls_early == UNACCOUNTED_SEARCH_URL_NAVIGATION
        or UNACCOUNTED_SEARCH_URL_NAVIGATION.lower() in text
        or "provider_invariant" in text
    ):
        return UNACCOUNTED_SEARCH_URL_NAVIGATION
    # Historical/stale control-plane blockers are NOT a live challenge.
    if provider_outcome in {
        "marketplace_ops_cooldown",
        "marketplace_challenge_deferred",
        "ebay_availability_cooldown",
        "ebay_availability_halt",
    } or str(diag.get("ownedDailyOutcome") or "") == PRE_FLIGHT_CONTROL_PLANE_BLOCKED:
        return PRE_FLIGHT_CONTROL_PLANE_BLOCKED
    if isinstance(exc, (ProviderAuthenticationRequiredError,)):
        return CHALLENGE_REQUIRED
    category = classify_provider_failure(text, diagnostics=diag)
    if category == "CHALLENGE_REQUIRED":
        return CHALLENGE_REQUIRED
    if category == "AUTH_REQUIRED":
        return CHALLENGE_REQUIRED
    if category == "DEFERRED":
        return PRE_FLIGHT_CONTROL_PLANE_BLOCKED
    # Marketplace Error Page after Sold filter — not a local CDP target-absent bug.
    fail_cls = str(diag.get("failureClass") or "").upper()
    page_cls = str(diag.get("marketplacePageClass") or diag.get("terminal") or "").upper()
    if (
        reason in {"ebay_error_page", "ebay_sorry_error_page"}
        or fail_cls in {"MARKETPLACE_ERROR_PAGE", "TARGET_REJECTED_UNHEALTHY_PAGE", "EBAY_ERROR_PAGE"}
        or page_cls in {"EBAY_ERROR_PAGE", "MARKETPLACE_ERROR_PAGE"}
        or "marketplace ebay_error_page" in text
        or ("marketplace" in text and "error_page" in text and "cdp_target_not_found" not in text)
    ):
        return TEMPORARY_EBAY_SERVER_FAILURE
    # Post-Sold local CDP capture failure — X11 Sold may still be verified.
    if (
        str(diag.get("ownedDailyOutcome") or "") == POST_SOLD_CAPTURE_FAILURE
        or str(diag.get("terminal") or "") == POST_SOLD_CAPTURE_FAILURE
        or reason in {"post_sold_capture_failed", "cdp_post_sold_capture_failed"}
        or "post_sold_capture_failure" in text
        or "post_sold_capture_failed" in text
    ):
        return POST_SOLD_CAPTURE_FAILURE
    # Post-Sold CDP finalize hang — local tooling, not eBay SORRY / access deny.
    if (
        str(diag.get("ownedDailyOutcome") or "") == FINALIZE_TIMEOUT_SAFE
        or str(diag.get("terminal") or "") == FINALIZE_TIMEOUT_SAFE
        or reason == "post_sold_finalize_timeout"
        or "finalize_timeout_safe" in text
    ):
        return FINALIZE_TIMEOUT_SAFE
    # eBay Live / alternate search surface — not LOCAL_GUI_FAILURE, not SORRY breaker.
    if (
        str(diag.get("ownedDailyOutcome") or "") in {ALTERNATE_EBAY_SURFACE, EBAY_LIVE_RESULTS}
        or reason in {"ebay_live_results", "alternate_ebay_surface", "sold_unavailable_on_alternate_surface"}
        or "ebay_live_results" in text
        or "alternate_ebay_surface" in text
        or "ebaylive/search" in text
    ):
        return ALTERNATE_EBAY_SURFACE
    # Local search surface leak / recovery failure — separate from 403/SORRY and from post-Sold Live results.
    if (
        str(diag.get("ownedDailyOutcome") or "") in LOCAL_SURFACE_OUTCOMES
        or reason
        in {
            "search_origin_ebay_live",
            "ebay_live_surface_at_submit_gate",
            "live_leak_after_type_unrecoverable",
            "ordinary_marketplace_surface_unrecoverable",
            "surface_still_invalid_after_recover",
            "ebay_live_after_refocus_type",
            "surface_not_validated_before_submit",
            "search_surface_not_validated_before_type",
        }
        or LOCAL_SEARCH_SURFACE_STATE_LEAK.lower() in text
        or LOCAL_SEARCH_SURFACE_RECOVERY_FAILED.lower() in text
    ):
        if LOCAL_SEARCH_SURFACE_RECOVERY_FAILED.lower() in text or "unrecoverable" in reason:
            return LOCAL_SEARCH_SURFACE_RECOVERY_FAILED
        return LOCAL_SEARCH_SURFACE_STATE_LEAK
    if (
        str(diag.get("ownedDailyOutcome") or "") == EBAY_ACCESS_DENIED_403
        or reason == "ebay_access_denied_403"
        or "ebay_access_denied_403" in text
    ):
        return EBAY_ACCESS_DENIED_403
    # Classic eBay SORRY / Error Page after successful local GUI navigation.
    if (
        reason == "ebay_sorry_error_page"
        or "temporary_ebay_server_failure" in text
        or "ebay sorry" in text
        or "pre_sold_sorry" in text
        or ("sorry" in text and "error page" in text)
        or category == "TRANSIENT_EBAY"
    ):
        return TEMPORARY_EBAY_SERVER_FAILURE
    if isinstance(exc, (ProviderTemporaryError, ProviderBlockedError)):
        # ProviderTemporaryError with ebay_sorry already returned above.
        return TEMPORARY_BROWSER_FAILURE
    if "sorry" in text:
        return TEMPORARY_EBAY_SERVER_FAILURE
    if "chrome-error" in text or "timeout" in text:
        return TEMPORARY_BROWSER_FAILURE
    if "refusing_zero_price_overwrite" in text or is_sparse_no_new_evidence_reason(text):
        return CHECKED_NO_NEW_EXACT_EVIDENCE
    if "no_reliable_price" in text:
        return NO_PRICE_EVER_FOUND
    return TEMPORARY_BROWSER_FAILURE


def summarize_outcome_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "OWNED_PRICE_CHECKS_COMPLETED": 0,
        "OWNED_PRICE_ESTIMATES_UPDATED": 0,
        "OWNED_PRICE_ESTIMATES_UNCHANGED": 0,
        "OWNED_PRICE_NO_NEW_EVIDENCE": 0,
        "OWNED_PRICE_BROWSER_FAILURES": 0,
        "OWNED_PRICE_CHALLENGES": 0,
        "OWNED_PRICE_LAST_GOOD_RETAINED": 0,
        "NO_PRICE_EVER_FOUND": 0,
    }
    for row in results:
        outcome = str(row.get("ownedDailyOutcome") or row.get("outcomeClass") or "").strip()
        if outcome in HEALTHY_CHECK_OUTCOMES or outcome == "already_fresh_noop" or outcome == "owned_daily_fresh_noop":
            counts["OWNED_PRICE_CHECKS_COMPLETED"] += 1
        if outcome == UPDATED_FROM_EBAY:
            counts["OWNED_PRICE_ESTIMATES_UPDATED"] += 1
        elif outcome == UNCHANGED_FROM_EBAY:
            counts["OWNED_PRICE_ESTIMATES_UNCHANGED"] += 1
        elif outcome == CHECKED_NO_NEW_EXACT_EVIDENCE:
            counts["OWNED_PRICE_NO_NEW_EVIDENCE"] += 1
            counts["OWNED_PRICE_LAST_GOOD_RETAINED"] += 1
        elif outcome in TRANSIENT_EBAY_FAILURE_OUTCOMES:
            counts["OWNED_PRICE_BROWSER_FAILURES"] += 1
            counts["OWNED_PRICE_LAST_GOOD_RETAINED"] += 1
        elif outcome in ALTERNATE_SURFACE_OUTCOMES:
            counts["OWNED_PRICE_BROWSER_FAILURES"] += 1
            counts["OWNED_PRICE_LAST_GOOD_RETAINED"] += 1
        elif outcome in LOCAL_SURFACE_OUTCOMES:
            counts["OWNED_PRICE_BROWSER_FAILURES"] += 1
            counts["OWNED_PRICE_LAST_GOOD_RETAINED"] += 1
        elif outcome in {CHALLENGE_REQUIRED, EBAY_CHALLENGE_REQUIRED}:
            counts["OWNED_PRICE_CHALLENGES"] += 1
            counts["OWNED_PRICE_LAST_GOOD_RETAINED"] += 1
        elif outcome == PRE_FLIGHT_CONTROL_PLANE_BLOCKED:
            # Preflight control-plane block — no live challenge, retain last-good.
            counts["OWNED_PRICE_LAST_GOOD_RETAINED"] += 1
        elif outcome == NO_PRICE_EVER_FOUND:
            counts["NO_PRICE_EVER_FOUND"] += 1
        elif str(row.get("status") or "").lower() == "failed":
            counts["OWNED_PRICE_BROWSER_FAILURES"] += 1
    return counts
