"""Job-level signals for 20/h and 60/24h live eBay navigation accounting."""

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
    return out


def result_consumes_submission_cap(row: dict[str, Any]) -> bool:
    """Counted eBay load/submit against 20/h and 60/24h (excluding pre-reserved jobs)."""
    if not isinstance(row, dict):
        return False
    if row.get(LIVE_SUBMISSION_RESERVED):
        return False
    if row.get(LIVE_EBAY_NAVIGATION_CONSUMED):
        return True
    if row.get("searchSubmissionStarted"):
        return True
    if str(row.get("status") or "").strip().lower() == "completed":
        return True
    diag = row.get("providerDiagnostics")
    if isinstance(diag, dict):
        if diag.get(LIVE_EBAY_NAVIGATION_CONSUMED) or diag.get("searchSubmissionStarted"):
            return True
        if live_ebay_navigation_consumed_from_metadata(_diag_blob(diag)):
            return True
    return False


__all__ = [
    "LIVE_EBAY_NAVIGATION_CONSUMED",
    "LIVE_SUBMISSION_RESERVED",
    "cap_fields_from_provider_metadata",
    "live_ebay_navigation_consumed_from_metadata",
    "result_consumes_submission_cap",
]