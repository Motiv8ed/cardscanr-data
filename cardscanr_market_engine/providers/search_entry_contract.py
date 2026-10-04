"""Accounted eBay search-entry contract for production owned_daily pricing.

ONE live production query-entry path:

  SEARCH_ENTRY_MODE=RENDERED_UI_X11

  rendered search input → type query → SEARCH_SUBMISSION_STARTED → Enter once

Direct navigation to constructed ``/sch/i.html?_nkw=...`` (or any query-bearing
results URL) before the durable event is a programming invariant failure:

  UNACCOUNTED_SEARCH_URL_NAVIGATION

Homepage/root navigation for COLD_START remains allowed.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlparse

from ..live_navigation_attempt import has_search_submission_started
from .errors import ProviderError

SEARCH_ENTRY_MODE_RENDERED_UI_X11 = "RENDERED_UI_X11"
UNACCOUNTED_SEARCH_URL_NAVIGATION = "UNACCOUNTED_SEARCH_URL_NAVIGATION"
DEFAULT_NAV_MODE = "linux_x11"


class ProviderInvariantError(ProviderError):
    """Local programming invariant — not marketplace unavailability."""

    error_code = "provider_invariant"
    retryable = False


def resolved_ebay_browser_nav_mode() -> str:
    """Production default is linux_x11 (accounted UI search), never bare Playwright."""
    raw = os.getenv("EBAY_BROWSER_NAV_MODE", "").strip().lower()
    if raw:
        return raw
    return DEFAULT_NAV_MODE


def is_ebay_homepage_or_root(url: str | None) -> bool:
    u = str(url or "").strip()
    if not u:
        return False
    parsed = urlparse(u)
    host = (parsed.netloc or "").lower()
    if "ebay." not in host:
        return False
    path = (parsed.path or "/").rstrip("/") or "/"
    return path in {"/", ""} and not parse_qs(parsed.query)


def is_query_bearing_search_results_url(url: str | None) -> bool:
    """True when URL is an eBay search-results page carrying a query (_nkw/_nkw etc.)."""
    u = str(url or "").strip()
    if not u:
        return False
    low = u.lower()
    if "ebay." not in low:
        return False
    parsed = urlparse(u)
    path = (parsed.path or "").lower()
    qs = parse_qs(parsed.query)
    nkw = unquote_plus((qs.get("_nkw") or qs.get("nkw") or [""])[0]).strip()
    if not nkw:
        return False
    # Ordinary /sch results or live-search surfaces with a query payload.
    if "/sch/" in path or "ebaylive" in low or "/sch/i.html" in low:
        return True
    # Any ebay URL with _nkw is treated as query-bearing navigation.
    return True


def extract_nkw(url: str | None) -> str | None:
    if not url:
        return None
    qs = parse_qs(urlparse(str(url)).query)
    nkw = unquote_plus((qs.get("_nkw") or qs.get("nkw") or [""])[0]).strip()
    return nkw or None


def current_attempt_has_search_submission(
    attempt_id: str | None = None,
) -> bool:
    aid = str(attempt_id or os.environ.get("CARDSCANR_LIVE_ATTEMPT_ID") or "").strip()
    if not aid:
        return False
    return bool(has_search_submission_started(aid))


def assert_programmatic_navigation_allowed(
    url: str | None,
    *,
    attempt_id: str | None = None,
    allow_homepage: bool = True,
) -> None:
    """Fail closed before issuing a network navigation to a query-bearing results URL.

    Allowed without SEARCH_SUBMISSION_STARTED:
      - blank / about:blank
      - eBay homepage/root (COLD_START warm)

    Forbidden without SEARCH_SUBMISSION_STARTED:
      - any query-bearing /sch or results URL
    """
    u = str(url or "").strip()
    if not u or u.lower().startswith("about:blank"):
        return
    if allow_homepage and is_ebay_homepage_or_root(u):
        return
    if not is_query_bearing_search_results_url(u):
        # Non-query ebay paths (e.g. category hubs) are out of scope; still block
        # manufactured sold deep-links carrying LH_Sold without event.
        low = u.lower()
        if "ebay." in low and ("lh_sold=1" in low or "lh_complete=1" in low):
            raise ProviderInvariantError(
                f"{UNACCOUNTED_SEARCH_URL_NAVIGATION}: manufactured sold/results URL "
                "before SEARCH_SUBMISSION_STARTED",
                diagnostics={
                    "failureClass": UNACCOUNTED_SEARCH_URL_NAVIGATION,
                    "terminal": UNACCOUNTED_SEARCH_URL_NAVIGATION,
                    "url": u[:500],
                    "searchEntryMode": SEARCH_ENTRY_MODE_RENDERED_UI_X11,
                    "directSearchUrlNavigation": True,
                    "searchSubmissionEventWritten": False,
                    "marketplaceError": False,
                    "challenge": False,
                },
            )
        return

    if current_attempt_has_search_submission(attempt_id):
        return

    raise ProviderInvariantError(
        f"{UNACCOUNTED_SEARCH_URL_NAVIGATION}: programmatic navigation to "
        "query-bearing eBay results URL before SEARCH_SUBMISSION_STARTED",
        diagnostics={
            "failureClass": UNACCOUNTED_SEARCH_URL_NAVIGATION,
            "terminal": UNACCOUNTED_SEARCH_URL_NAVIGATION,
            "url": u[:500],
            "nkw": extract_nkw(u),
            "searchEntryMode": SEARCH_ENTRY_MODE_RENDERED_UI_X11,
            "directSearchUrlNavigation": True,
            "searchSubmissionEventWritten": False,
            "marketplaceError": False,
            "challenge": False,
            "note": "No network request should be issued for this navigation",
        },
    )


def build_search_entry_evidence(
    *,
    search_entry_mode: str = SEARCH_ENTRY_MODE_RENDERED_UI_X11,
    search_input_located: bool | None = None,
    query_typed: bool | None = None,
    query_before_submit: str | None = None,
    direct_search_url_navigation: bool = False,
    search_submission_event_written: bool | None = None,
    enter_pressed: bool | None = None,
    ordinary_results_confirmed: bool | None = None,
    attempt_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    aid = str(attempt_id or os.environ.get("CARDSCANR_LIVE_ATTEMPT_ID") or "").strip() or None
    event_written = (
        search_submission_event_written
        if search_submission_event_written is not None
        else (current_attempt_has_search_submission(aid) if aid else False)
    )
    out: dict[str, Any] = {
        "searchEntryMode": search_entry_mode,
        "searchInputLocated": search_input_located,
        "queryTyped": query_typed,
        "queryBeforeSubmit": query_before_submit,
        "directSearchUrlNavigation": bool(direct_search_url_navigation),
        "searchSubmissionEventWritten": bool(event_written),
        "enterPressed": enter_pressed,
        "ordinaryResultsConfirmed": ordinary_results_confirmed,
        "attemptId": aid,
    }
    if extra:
        out.update(extra)
    return out


def historical_kakuna_accounting_gap() -> dict[str, Any]:
    """Immutable ledger note for the Kakuna STOPPED_SAFE gap (do not invent events)."""
    return {
        "attemptId": "c1dbfde5-3672-4cfc-8532-faaff8d2cdb3",
        "card": "Kakuna / Chaos Rising / 2",
        "fingerprint": "pokemon|en|me4|2|kakuna|raw|raw|au|aud",
        "canonicalSearchSubmissionStarted": False,
        "officialConsumed": False,
        "querySearchUrlObserved": True,
        "querySearchUrl": "https://www.ebay.com.au/sch/i.html?_nkw=Kakuna+2+chaos+rising+Pokemon",
        "networkSearchPageReached": True,
        "auSearchEntry": "homepage_then_clean_search_url",
        "terminal": "PRE_SOLD_SORRY",
        "ACCOUNTING_CONTRACT_VIOLATION_HISTORICAL": True,
        "note": (
            "Historical SEARCH_SUBMISSION_STARTED event remains ABSENT and must not be "
            "manufactured. A query-bearing /sch URL was reached via Playwright "
            "homepage_then_clean_search_url before the durable event boundary."
        ),
        "oldEntryMode": "homepage_then_clean_search_url",
        "newEntryMode": SEARCH_ENTRY_MODE_RENDERED_UI_X11,
        "sourceFile": "cardscanr_market_engine/providers/ebay_browser_provider.py",
        "sourceFunction": "_fetch_with_playwright",
        "sourceLines": "3686-3716",
    }


__all__ = [
    "DEFAULT_NAV_MODE",
    "ProviderInvariantError",
    "SEARCH_ENTRY_MODE_RENDERED_UI_X11",
    "UNACCOUNTED_SEARCH_URL_NAVIGATION",
    "assert_programmatic_navigation_allowed",
    "build_search_entry_evidence",
    "current_attempt_has_search_submission",
    "extract_nkw",
    "historical_kakuna_accounting_gap",
    "is_ebay_homepage_or_root",
    "is_query_bearing_search_results_url",
    "resolved_ebay_browser_nav_mode",
]
