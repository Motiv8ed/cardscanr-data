"""Job-level signals for live eBay navigation accounting (60/h, 300/24h production AU)."""

from __future__ import annotations

from typing import Any

# Top-level job result fields (provider + job_runner).
LIVE_EBAY_NAVIGATION_CONSUMED = "liveEbayNavigationConsumed"
LIVE_SUBMISSION_RESERVED = "liveSubmissionReserved"


def _diag_blob(provider_diagnostics: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(provider_diagnostics, dict):
        return {}
    nested = provider_diagnostics.get("diagnostics")
    if isinstance(nested, dict):
        return nested
    return provider_diagnostics


def live_ebay_navigation_consumed_from_metadata(meta: dict[str, Any] | None) -> bool:
    """True when provider metadata shows search submit and/or sold navigation progressed."""
    if not isinstance(meta, dict):
        return False
    if meta.get(LIVE_EBAY_NAVIGATION_CONSUMED):
        return True
    if meta.get("searchSubmissionStarted"):
        return True
    desktop = meta.get("desktopNav")
    if isinstance(desktop, dict):
        if desktop.get("searchSubmissionStarted"):
            return True
        if desktop.get("searchSuccess") and (desktop.get("submitted") or desktop.get("soldClickSuccess")):
            return True
    nav = meta.get("navigation") if isinstance(meta.get("navigation"), dict) else {}
    search = meta.get("search") if isinstance(meta.get("search"), dict) else {}
    if not search and isinstance(nav.get("search"), dict):
        search = nav.get("search")  # type: ignore[assignment]
    if search.get("searchSubmissionStarted") or search.get("submitted"):
        return True
    stage = meta.get("searchEntryEvidence")
    if isinstance(stage, dict) and stage.get("search_submission_event_written"):
        return True
    return False


def cap_fields_from_provider_metadata(meta: dict[str, Any] | None) -> dict[str, Any]:
    consumed = live_ebay_navigation_consumed_from_metadata(meta)
    out: dict[str, Any] = {LIVE_EBAY_NAVIGATION_CONSUMED: consumed}
    if isinstance(meta, dict) and meta.get("searchSubmissionStarted"):
        out["searchSubmissionStarted"] = meta.get("searchSubmissionStarted")
    nav_count = None
    if isinstance(meta, dict):
        nav_count = meta.get("liveEbayNavigationCount")
    if nav_count is not None:
        out["liveEbayNavigationCount"] = nav_count
    return out


def supplementary_submission_needs_cap(row: dict[str, Any]) -> bool:
    """Post-job cap for eBay navigations that were not reserved before search (#63)."""
    if not isinstance(row, dict):
        return False
    if row.get(LIVE_SUBMISSION_RESERVED):
        return False
    if row.get("budgetSubmissionCounted"):
        return False
    if row.get(LIVE_EBAY_NAVIGATION_CONSUMED) or row.get("searchSubmissionStarted"):
        return True
    diag = row.get("providerDiagnostics")
    if isinstance(diag, dict):
        if diag.get(LIVE_EBAY_NAVIGATION_CONSUMED) or diag.get("searchSubmissionStarted"):
            return True
        if live_ebay_navigation_consumed_from_metadata(_diag_blob(diag)):
            return True
    return False


# Back-compat name for tests; does not count bare completed jobs (reserve-before-search is canonical).
def result_consumes_submission_cap(row: dict[str, Any]) -> bool:
    return supplementary_submission_needs_cap(row)


def extra_live_navigations_from_row(row: dict[str, Any]) -> int:
    """Navigations beyond the single reserve-at-job-start (variants + generic-error retries)."""
    if not isinstance(row, dict):
        return 0
    count = row.get("liveEbayNavigationCount")
    if count is None:
        diag = row.get("providerDiagnostics")
        if isinstance(diag, dict):
            nested = diag.get("diagnostics") if isinstance(diag.get("diagnostics"), dict) else diag
            if isinstance(nested, dict):
                count = nested.get("liveEbayNavigationCount")
            meta = diag.get("raw_metadata") if isinstance(diag.get("raw_metadata"), dict) else {}
            if count is None and isinstance(meta, dict):
                count = meta.get("liveEbayNavigationCount")
    try:
        total = max(0, int(count or 0))
    except (TypeError, ValueError):
        total = 0
    if total <= 1:
        return 0
    return total - 1


def row_needs_supplementary_cap_slots(row: dict[str, Any]) -> int:
    """Total extra reserve slots for this job result (unreserved primary and/or extra navigations)."""
    if supplementary_submission_needs_cap(row):
        return 1 + extra_live_navigations_from_row(row)
    if row.get(LIVE_SUBMISSION_RESERVED):
        return extra_live_navigations_from_row(row)
    return extra_live_navigations_from_row(row)


__all__ = [
    "LIVE_EBAY_NAVIGATION_CONSUMED",
    "LIVE_SUBMISSION_RESERVED",
    "cap_fields_from_provider_metadata",
    "live_ebay_navigation_consumed_from_metadata",
    "result_consumes_submission_cap",
    "extra_live_navigations_from_row",
    "row_needs_supplementary_cap_slots",
    "supplementary_submission_needs_cap",
]