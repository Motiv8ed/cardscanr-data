"""Deterministic GUI state-machine guards for Linux X11 eBay navigation.

Pure logic (no X11) — unit-tested. Orchestrators must honor these gates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlparse

from .sold_page_health import is_ebay_authentication_url, is_ebay_error_page

# Operational outcomes
TEMPORARY_EBAY_SERVER_FAILURE = "TEMPORARY_EBAY_SERVER_FAILURE"
EBAY_CHALLENGE_REQUIRED = "EBAY_CHALLENGE_REQUIRED"
EBAY_ACCESS_DENIED_403 = "EBAY_ACCESS_DENIED_403"
ALTERNATE_EBAY_SURFACE = "ALTERNATE_EBAY_SURFACE"
EBAY_LIVE_RESULTS = "EBAY_LIVE_RESULTS"
FINALIZE_TIMEOUT_SAFE = "FINALIZE_TIMEOUT_SAFE"
LOCAL_GUI_FAILURE = "LOCAL_GUI_FAILURE"
LOCAL_SEARCH_SURFACE_STATE_LEAK = "LOCAL_SEARCH_SURFACE_STATE_LEAK"
LOCAL_SEARCH_SURFACE_RECOVERY_FAILED = "LOCAL_SEARCH_SURFACE_RECOVERY_FAILED"
ORDINARY_RESULTS_CONFIRMED = "ORDINARY_RESULTS_CONFIRMED"
SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE = "SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE"
SEARCH_SURFACE_VALIDATED = "SEARCH_SURFACE_VALIDATED"

# Approved ordinary marketplace origins (not Live / vertical / error surfaces).
ORDINARY_MARKETPLACE_SURFACE = "ORDINARY_MARKETPLACE_SEARCH"
REJECTED_ORIGIN_EBAY_LIVE = "REJECTED_ORIGIN_EBAY_LIVE"
REJECTED_ORIGIN_SORRY = "REJECTED_ORIGIN_SORRY"
REJECTED_ORIGIN_CHALLENGE = "REJECTED_ORIGIN_CHALLENGE"
REJECTED_ORIGIN_ABOUT_BLANK = "REJECTED_ORIGIN_ABOUT_BLANK"
REJECTED_ORIGIN_UNEXPECTED = "REJECTED_ORIGIN_UNEXPECTED"
REJECTED_ORIGIN_SPECIALIZED = "REJECTED_ORIGIN_SPECIALIZED"


class SearchPhase(str, Enum):
    SEARCH_PAGE_READY = "SEARCH_PAGE_READY"
    SEARCH_SURFACE_VALIDATED = "SEARCH_SURFACE_VALIDATED"
    SEARCH_BUTTON_LOCATED = "SEARCH_BUTTON_LOCATED"
    SEARCH_FIELD_CLICKED = "SEARCH_FIELD_CLICKED"
    SEARCH_FIELD_FOCUS_PROBE = "SEARCH_FIELD_FOCUS_PROBE"
    QUERY_TYPED = "QUERY_TYPED"
    QUERY_VISIBLE_CONFIRMED = "QUERY_VISIBLE_CONFIRMED"
    SEARCH_SUBMITTED = "SEARCH_SUBMITTED"
    SEARCH_NAVIGATION_PENDING = "SEARCH_NAVIGATION_PENDING"
    ORDINARY_RESULTS_CONFIRMED = "ORDINARY_RESULTS_CONFIRMED"
    ALTERNATE_EBAY_SURFACE = "ALTERNATE_EBAY_SURFACE"
    EBAY_LIVE_RESULTS = "EBAY_LIVE_RESULTS"
    SEARCH_RESULTS_CONFIRMED = "SEARCH_RESULTS_CONFIRMED"  # alias retained for older artifacts
    SEARCH_INPUT_NOT_CONFIRMED = "SEARCH_INPUT_NOT_CONFIRMED"
    TEMPORARY_EBAY_SERVER_FAILURE = "TEMPORARY_EBAY_SERVER_FAILURE"
    EBAY_ACCESS_DENIED_403 = "EBAY_ACCESS_DENIED_403"
    EBAY_CHALLENGE = "EBAY_CHALLENGE"
    EBAY_AUTH_REQUIRED = "EBAY_AUTH_REQUIRED"
    LOCAL_GUI_FAILURE = "LOCAL_GUI_FAILURE"
    LOCAL_SEARCH_SURFACE_STATE_LEAK = "LOCAL_SEARCH_SURFACE_STATE_LEAK"
    LOCAL_SEARCH_SURFACE_RECOVERY_FAILED = "LOCAL_SEARCH_SURFACE_RECOVERY_FAILED"
    FINALIZE_TIMEOUT_SAFE = "FINALIZE_TIMEOUT_SAFE"


class SoldPhase(str, Enum):
    RESULTS_READY = "RESULTS_READY"
    SOLD_CONTROL_AVAILABLE = "SOLD_CONTROL_AVAILABLE"
    SOLD_LOCATED = "SOLD_LOCATED"
    SOLD_CLICKED = "SOLD_CLICKED"
    SOLD_NAVIGATION_PENDING = "SOLD_NAVIGATION_PENDING"
    SOLD_STATE_VERIFIED = "SOLD_STATE_VERIFIED"
    SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE = "SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE"
    EBAY_CHALLENGE = "EBAY_CHALLENGE"
    EBAY_AUTH_REQUIRED = "EBAY_AUTH_REQUIRED"
    EBAY_SORRY = "EBAY_SORRY"
    TEMPORARY_EBAY_SERVER_FAILURE = "TEMPORARY_EBAY_SERVER_FAILURE"
    EBAY_ACCESS_DENIED_403 = "EBAY_ACCESS_DENIED_403"
    SOLD_NAVIGATION_TIMEOUT = "SOLD_NAVIGATION_TIMEOUT"
    ABOUT_BLANK_ABORT = "ABOUT_BLANK_ABORT"
    LOCAL_GUI_FAILURE = "LOCAL_GUI_FAILURE"


FORBIDDEN_WHILE_SOLD_PENDING = frozenset(
    {
        "omnibox_focus",
        "ctrl_l",
        "clear_address_bar",
        "navigate_homepage",
        "begin_next_card",
        "restart_chrome",
        "send_search_query",
        "generic_reset",
    }
)

LEFT_RAIL_X_MAX = 340
LEFT_RAIL_X_MIN = 55


@dataclass
class SearchGateState:
    phase: SearchPhase = SearchPhase.SEARCH_PAGE_READY
    refocus_attempts: int = 0
    max_refocus_attempts: int = 1
    query_visible: bool = False
    surface_validated: bool = False
    search_surface_class: str | None = None
    search_origin_url: str | None = None
    submitted: bool = False
    events: list[str] = field(default_factory=list)
    route_class: str | None = None

    def note(self, msg: str) -> None:
        self.events.append(msg)


@dataclass
class SoldGateState:
    phase: SoldPhase = SoldPhase.RESULTS_READY
    pending: bool = False
    events: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.events.append(msg)


def page_is_about_blank(url: str | None, title: str | None = None) -> bool:
    u = (url or "").strip().lower()
    t = (title or "").strip().lower()
    if u.startswith("about:blank") or u == "about:blank":
        return True
    if t in {"untitled", "untitled - google chrome", "loading...", "loading…"} and (
        not u or u.startswith("about:")
    ):
        return True
    return False


def search_page_ready(*, url: str, title: str, search_button_found: bool) -> tuple[bool, str]:
    if page_is_about_blank(url, title):
        return False, "about_blank"
    if is_ebay_error_page(title=title, url=url):
        return False, "ebay_error_page"
    if is_ebay_sorry_page(title=title, url=url):
        return False, "ebay_sorry_error_page"
    if "ebay." not in (url or "").lower() and "ebay" not in (title or "").lower():
        if not search_button_found:
            return False, "not_ebay_no_search_button"
    if not search_button_found:
        return False, "search_button_missing"
    return True, "ok"


def classify_search_origin_surface(
    *,
    url: str | None,
    title: str | None = None,
    body: str | None = None,
    scope_label: str | None = None,
) -> dict[str, Any]:
    """Classify whether the current page is an approved ordinary search origin."""
    u = (url or "").strip()
    t = (title or "").strip()
    scope = (scope_label or "").strip().lower()
    base = {
        "url": u or None,
        "title": t or None,
        "scopeLabel": scope_label,
        "approved": False,
        "surfaceClass": REJECTED_ORIGIN_UNEXPECTED,
        "reason": "unclassified",
    }
    if page_is_about_blank(u, t):
        return {**base, "surfaceClass": REJECTED_ORIGIN_ABOUT_BLANK, "reason": "about_blank"}
    if is_ebay_challenge_page(title=t, url=u, body=body):
        return {**base, "surfaceClass": REJECTED_ORIGIN_CHALLENGE, "reason": "challenge"}
    if is_ebay_sorry_page(title=t, url=u, body=body):
        return {**base, "surfaceClass": REJECTED_ORIGIN_SORRY, "reason": "sorry_error_page"}
    if is_ebay_live_search_url(u) or "ebaylive" in u.lower():
        return {**base, "surfaceClass": REJECTED_ORIGIN_EBAY_LIVE, "reason": "ebay_live_url"}
    if scope in {"ebay live", "ebaylive", "live"}:
        return {**base, "surfaceClass": REJECTED_ORIGIN_EBAY_LIVE, "reason": "ebay_live_scope_label"}
    ul = u.lower()
    if not ul or "ebay." not in ul:
        return {**base, "surfaceClass": REJECTED_ORIGIN_UNEXPECTED, "reason": "not_ebay_url"}
    # Reject other specialized verticals when URL path is clearly non-marketplace search.
    path = urlparse(u).path.lower() if u else ""
    specialized_markers = ("/ebaylive/", "/str/", "/mys/", "/itm/", "/b/", "/v/")
    if any(m in path for m in specialized_markers) and "/sch/" not in path:
        # Homepage and category hubs are OK; item/store/live are not search origins.
        if path not in {"", "/"} and not path.startswith("/sch"):
            # Allow plain domain root and common hubs without specialized markers already checked.
            if any(m in path for m in ("/ebaylive/", "/str/", "/mys/", "/itm/")):
                return {
                    **base,
                    "surfaceClass": REJECTED_ORIGIN_SPECIALIZED,
                    "reason": f"specialized_path:{path}",
                }
    # Ordinary marketplace: homepage or /sch/ results (re-search from results is OK).
    return {
        **base,
        "approved": True,
        "surfaceClass": ORDINARY_MARKETPLACE_SURFACE,
        "reason": "ordinary_marketplace",
    }


def on_search_surface_validated(
    state: SearchGateState,
    *,
    url: str | None,
    title: str | None = None,
    body: str | None = None,
    scope_label: str | None = None,
) -> tuple[bool, SearchGateState, dict[str, Any]]:
    """Gate: must pass before typing/submitting. Rejects Live and other bad origins."""
    origin = classify_search_origin_surface(url=url, title=title, body=body, scope_label=scope_label)
    state.search_origin_url = url
    state.search_surface_class = str(origin.get("surfaceClass") or "")
    if origin.get("approved"):
        state.surface_validated = True
        state.phase = SearchPhase.SEARCH_SURFACE_VALIDATED
        state.note(SEARCH_SURFACE_VALIDATED)
        return True, state, origin
    state.surface_validated = False
    if origin.get("surfaceClass") == REJECTED_ORIGIN_EBAY_LIVE:
        state.phase = SearchPhase.LOCAL_SEARCH_SURFACE_STATE_LEAK
        state.note(LOCAL_SEARCH_SURFACE_STATE_LEAK)
    else:
        state.phase = SearchPhase.LOCAL_GUI_FAILURE
        state.note(f"SEARCH_SURFACE_REJECTED:{origin.get('surfaceClass')}")
    return False, state, origin


def may_submit_search(state: SearchGateState) -> bool:
    """Submit requires SEARCH_SURFACE_VALIDATED then QUERY_VISIBLE_CONFIRMED."""
    return (
        bool(state.surface_validated)
        and bool(state.query_visible)
        and state.phase == SearchPhase.QUERY_VISIBLE_CONFIRMED
        and not state.submitted
    )


def on_query_visibility(state: SearchGateState, *, visible: bool) -> SearchGateState:
    if visible:
        state.query_visible = True
        state.phase = SearchPhase.QUERY_VISIBLE_CONFIRMED
        state.note("QUERY_VISIBLE_CONFIRMED")
        return state
    if state.refocus_attempts < state.max_refocus_attempts:
        state.refocus_attempts += 1
        state.query_visible = False
        state.phase = SearchPhase.SEARCH_FIELD_FOCUS_PROBE
        state.note(f"refocus_allowed attempt={state.refocus_attempts}")
        return state
    state.query_visible = False
    state.phase = SearchPhase.SEARCH_INPUT_NOT_CONFIRMED
    state.note("SEARCH_INPUT_NOT_CONFIRMED")
    return state


def on_submit_attempt(state: SearchGateState) -> tuple[bool, SearchGateState]:
    if not may_submit_search(state):
        if not state.surface_validated:
            state.note("submit_blocked_without_search_surface_validated")
        else:
            state.note("submit_blocked_without_query_visible")
        return False, state
    state.submitted = True
    state.phase = SearchPhase.SEARCH_SUBMITTED
    state.note("SEARCH_SUBMITTED")
    state.phase = SearchPhase.SEARCH_NAVIGATION_PENDING
    state.note("SEARCH_NAVIGATION_PENDING")
    return True, state


def sold_click_coords_valid(x: int, y: int, *, win_y: int = 0) -> bool:
    return LEFT_RAIL_X_MIN <= x <= LEFT_RAIL_X_MAX and (win_y + 180) <= y <= (win_y + 720)


def may_perform_browser_action(state: SoldGateState, action: str) -> bool:
    if state.pending and action in FORBIDDEN_WHILE_SOLD_PENDING:
        return False
    return True


def on_sold_clicked(state: SoldGateState) -> SoldGateState:
    state.pending = True
    state.phase = SoldPhase.SOLD_NAVIGATION_PENDING
    state.note("SOLD_NAVIGATION_PENDING")
    return state


def on_sold_terminal(
    state: SoldGateState,
    *,
    verified: bool = False,
    challenge: bool = False,
    sorry: bool = False,
    access_denied_403: bool = False,
    timeout: bool = False,
    about_blank: bool = False,
    sold_unavailable_alternate: bool = False,
    local_gui: bool = False,
    auth_required: bool = False,
) -> SoldGateState:
    state.pending = False
    if about_blank:
        state.phase = SoldPhase.ABOUT_BLANK_ABORT
        state.note("ABOUT_BLANK_ABORT")
    elif auth_required:
        state.phase = SoldPhase.EBAY_AUTH_REQUIRED
        state.note("EBAY_AUTH_REQUIRED")
    elif challenge:
        state.phase = SoldPhase.EBAY_CHALLENGE
        state.note("EBAY_CHALLENGE")
    elif access_denied_403:
        state.phase = SoldPhase.EBAY_ACCESS_DENIED_403
        state.note(EBAY_ACCESS_DENIED_403)
    elif sorry:
        state.phase = SoldPhase.EBAY_SORRY
        state.note("EBAY_SORRY")
    elif sold_unavailable_alternate:
        state.phase = SoldPhase.SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE
        state.note(SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE)
    elif local_gui:
        state.phase = SoldPhase.LOCAL_GUI_FAILURE
        state.note(LOCAL_GUI_FAILURE)
    elif verified:
        state.phase = SoldPhase.SOLD_STATE_VERIFIED
        state.note("SOLD_STATE_VERIFIED")
    elif timeout:
        state.phase = SoldPhase.SOLD_NAVIGATION_TIMEOUT
        state.note("SOLD_NAVIGATION_TIMEOUT")
    return state


def is_ebay_challenge_page(
    title: str | None = None,
    url: str | None = None,
    body: str | None = None,
) -> bool:
    """Active marketplace challenge — passive recaptcha iframe alone is non-blocking."""
    t = (title or "").lower()
    u = (url or "").lower()
    b = (body or "").lower()
    if "splashui/challenge" in u or "security measure" in t or "verify yourself" in t:
        return True
    if "verification challenge" in f"{t}\n{b}":
        return True
    if "captcha" in t or "captcha" in u:
        return True
    if "captcha" in b:
        # Passive google recaptcha iframe on an otherwise normal page is not an active challenge.
        if "recaptcha" in b and "iframe" in b and "verify yourself" not in b and "security measure" not in b:
            return False
        return True
    return False


def is_ebay_sorry_page(
    title: str | None = None,
    url: str | None = None,
    body: str | None = None,
) -> bool:
    """Classic eBay SORRY interstitial — not marketplace Error Page | eBay.

    Error Page title/URL is classified separately (sold_page_health.is_ebay_error_page)
    so capture/Sold health can report MARKETPLACE_ERROR_PAGE instead of collapsing into
    CDP_TARGET_NOT_FOUND or treating every error page as CAPTCHA.
    """
    t = (title or "").strip().lower()
    u = (url or "").strip().lower()
    b = (body or "").strip().lower()
    blob = f"{t}\n{b}"
    if is_ebay_challenge_page(title=title, url=url, body=body):
        return False
    # Distinct class: Error Page | eBay (and /error) — not classic SORRY.
    if ("error page" in t and "ebay" in t) or "/error" in u:
        return False
    if "sorry" in blob and (
        "something went wrong" in blob
        or "looks like" in blob
        or "on our end" in blob
    ):
        return True
    if "something went wrong on our end" in blob:
        return True
    # Legacy: title-only sorry markers without Error Page wording.
    if t.startswith("sorry") and "ebay" in t:
        return True
    return False


def is_ebay_live_search_url(url: str | None) -> bool:
    u = (url or "").strip().lower()
    return "ebaylive/search" in u or "/ebaylive/" in u


def is_ordinary_sch_results_url(url: str | None) -> bool:
    u = (url or "").strip().lower()
    return "/sch/" in u and "_nkw=" in u and "ebaylive" not in u


def query_represented_in_url(url: str | None, expected_query: str | None) -> bool | None:
    """True/False when _nkw present; None if URL has no _nkw to compare."""
    if not url or not expected_query:
        return None
    try:
        qs = parse_qs(urlparse(url).query)
        nkw = unquote_plus((qs.get("_nkw") or [""])[0]).strip().lower()
    except Exception:
        return None
    if not nkw:
        return None
    # Token overlap — do not require exact string equality.
    exp_tokens = {t for t in expected_query.lower().split() if len(t) > 1}
    got_tokens = {t for t in nkw.split() if len(t) > 1}
    if not exp_tokens:
        return None
    return len(exp_tokens & got_tokens) >= max(1, min(2, len(exp_tokens)))


def classify_search_surface(
    *,
    title: str | None,
    url: str | None,
    body: str | None = None,
    http_status: int | None = None,
    expected_query: str | None = None,
    query_visible_confirmed: bool = False,
    submitted: bool = False,
    sold_control_available: bool | None = None,
) -> dict[str, Any]:
    """Classify post-submit search surface. Never auto-map ebaylive → LOCAL_GUI_FAILURE."""
    base = {
        "url": url,
        "title": title,
        "httpStatus": http_status,
        "queryVisibleConfirmed": query_visible_confirmed,
        "submitted": submitted,
        "queryRepresented": query_represented_in_url(url, expected_query),
        "ordinaryResults": False,
        "ebayLive": False,
        "alternateSurface": False,
        "soldControlAvailable": sold_control_available,
        "sorry": False,
        "challenge": False,
        "accessDenied403": False,
        "tripsSorryBreaker": False,
        "markFresh": False,
        "retainLastGood": True,
    }
    if page_is_about_blank(url, title):
        return {
            **base,
            "routeClass": "ABOUT_BLANK",
            "phase": SearchPhase.LOCAL_GUI_FAILURE.value,
            "outcome": "ABOUT_BLANK_ABORT",
            "terminal": "ABOUT_BLANK_ABORT",
        }
    if is_ebay_authentication_url(url):
        return {
            **base,
            "routeClass": "EBAY_AUTH_REQUIRED",
            "phase": SearchPhase.EBAY_AUTH_REQUIRED.value,
            "outcome": "EBAY_AUTH_REQUIRED",
            "terminal": "EBAY_AUTH_REQUIRED",
            "challenge": False,
        }
    if is_ebay_challenge_page(title=title, url=url, body=body):
        return {
            **base,
            "routeClass": "EBAY_CHALLENGE",
            "phase": SearchPhase.EBAY_CHALLENGE.value,
            "outcome": EBAY_CHALLENGE_REQUIRED,
            "terminal": "EBAY_CHALLENGE",
            "challenge": True,
        }
    from .sold_page_health import EBAY_ERROR_PAGE

    sorry = is_ebay_sorry_page(title=title, url=url, body=body)
    error_page = is_ebay_error_page(title=title, url=url, body=body)
    # HTTP 403 takes precedence over Error Page title (access-denied contract).
    if http_status == 403 and (sorry or error_page):
        return {
            **base,
            "routeClass": "EBAY_ACCESS_DENIED_403",
            "phase": SearchPhase.EBAY_ACCESS_DENIED_403.value,
            "outcome": EBAY_ACCESS_DENIED_403,
            "terminal": EBAY_ACCESS_DENIED_403,
            "sorry": bool(sorry),
            "errorPage": bool(error_page),
            "accessDenied403": True,
            "tripsSorryBreaker": True,
            "localGuiThroughSubmit": bool(query_visible_confirmed and submitted),
        }
    if error_page:
        return {
            **base,
            "routeClass": "TEMPORARY_EBAY_SERVER_FAILURE",
            "phase": SearchPhase.TEMPORARY_EBAY_SERVER_FAILURE.value,
            "outcome": TEMPORARY_EBAY_SERVER_FAILURE,
            "terminal": EBAY_ERROR_PAGE,
            "sorry": False,
            "errorPage": True,
            "tripsSorryBreaker": True,
            "localGuiThroughSubmit": bool(query_visible_confirmed and submitted),
        }
    if sorry:
        return {
            **base,
            "routeClass": "TEMPORARY_EBAY_SERVER_FAILURE",
            "phase": SearchPhase.TEMPORARY_EBAY_SERVER_FAILURE.value,
            "outcome": TEMPORARY_EBAY_SERVER_FAILURE,
            "terminal": "EBAY_SORRY",
            "sorry": True,
            "tripsSorryBreaker": True,
            "localGuiThroughSubmit": bool(query_visible_confirmed and submitted),
        }
    if is_ebay_live_search_url(url):
        sold_ok = bool(sold_control_available) if sold_control_available is not None else False
        return {
            **base,
            "routeClass": EBAY_LIVE_RESULTS,
            "phase": SearchPhase.EBAY_LIVE_RESULTS.value,
            "outcome": ALTERNATE_EBAY_SURFACE if not sold_ok else EBAY_LIVE_RESULTS,
            "terminal": SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE if not sold_ok else EBAY_LIVE_RESULTS,
            "ebayLive": True,
            "alternateSurface": True,
            "ordinaryResults": False,
            "tripsSorryBreaker": False,
            "soldControlAvailable": sold_ok,
        }
    if is_ordinary_sch_results_url(url):
        return {
            **base,
            "routeClass": ORDINARY_RESULTS_CONFIRMED,
            "phase": SearchPhase.ORDINARY_RESULTS_CONFIRMED.value,
            "outcome": ORDINARY_RESULTS_CONFIRMED,
            "terminal": ORDINARY_RESULTS_CONFIRMED,
            "ordinaryResults": True,
            "ebayLive": False,
        }
    # Unexpected tab / non-results after confirmed submit — local only when not an eBay surface.
    u = (url or "").lower()
    if submitted and query_visible_confirmed and "ebay." in u:
        return {
            **base,
            "routeClass": ALTERNATE_EBAY_SURFACE,
            "phase": SearchPhase.ALTERNATE_EBAY_SURFACE.value,
            "outcome": ALTERNATE_EBAY_SURFACE,
            "terminal": ALTERNATE_EBAY_SURFACE,
            "alternateSurface": True,
            "tripsSorryBreaker": False,
        }
    if submitted and query_visible_confirmed:
        return {
            **base,
            "routeClass": "SEARCH_RESULTS_NOT_CONFIRMED",
            "phase": SearchPhase.SEARCH_SUBMITTED.value,
            "outcome": "SEARCH_RESULTS_NOT_CONFIRMED",
            "terminal": "SEARCH_RESULTS_NOT_CONFIRMED",
        }
    return {
        **base,
        "routeClass": LOCAL_GUI_FAILURE,
        "phase": SearchPhase.LOCAL_GUI_FAILURE.value,
        "outcome": LOCAL_GUI_FAILURE,
        "terminal": LOCAL_GUI_FAILURE,
    }


def classify_post_navigation_page(
    *,
    title: str | None,
    url: str | None,
    body: str | None = None,
    query_visible_confirmed: bool = False,
    submitted: bool = False,
    http_status: int | None = None,
    expected_query: str | None = None,
    sold_control_available: bool | None = None,
) -> dict[str, Any]:
    surface = classify_search_surface(
        title=title,
        url=url,
        body=body,
        http_status=http_status,
        expected_query=expected_query,
        query_visible_confirmed=query_visible_confirmed,
        submitted=submitted,
        sold_control_available=sold_control_available,
    )
    return {
        "terminal": surface.get("terminal"),
        "outcome": surface.get("outcome"),
        "sorry": bool(surface.get("sorry")),
        "errorPage": bool(surface.get("errorPage")),
        "challenge": bool(surface.get("challenge")),
        "accessDenied403": bool(surface.get("accessDenied403")),
        "verified": surface.get("routeClass") == ORDINARY_RESULTS_CONFIRMED,
        "routeClass": surface.get("routeClass"),
        "ebayLive": bool(surface.get("ebayLive")),
        "alternateSurface": bool(surface.get("alternateSurface")),
        "ordinaryResults": bool(surface.get("ordinaryResults")),
        "tripsSorryBreaker": bool(surface.get("tripsSorryBreaker")),
        "localGuiThroughSubmit": surface.get("localGuiThroughSubmit"),
        "surface": surface,
    }


def classify_post_sold_url(
    url: str,
    title: str,
    body: str | None = None,
    *,
    http_status: int | None = None,
) -> dict[str, Any]:
    classified = classify_post_navigation_page(
        title=title, url=url, body=body, http_status=http_status, submitted=True, query_visible_confirmed=True
    )
    if classified["terminal"] is not None and classified["terminal"] not in {
        ORDINARY_RESULTS_CONFIRMED,
        ALTERNATE_EBAY_SURFACE,
        EBAY_LIVE_RESULTS,
    }:
        return {
            "terminal": classified["terminal"],
            "verified": False,
            "outcome": classified.get("outcome"),
            "sorry": classified.get("sorry"),
            "errorPage": classified.get("errorPage"),
            "challenge": classified.get("challenge"),
            "accessDenied403": classified.get("accessDenied403"),
            "soldFilterStateVerified": "lh_sold=1" in str(url or "").lower(),
            "soldPageHealthVerified": False,
            "x11SoldStateVerified": False,
            "marketplacePageClass": classified.get("terminal"),
        }
    if is_ebay_live_search_url(url):
        return {
            "terminal": SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE,
            "verified": False,
            "outcome": ALTERNATE_EBAY_SURFACE,
            "sorry": False,
            "challenge": False,
            "ebayLive": True,
        }
    # Filter + page health (Error Page with LH_Sold must not verify).
    from .sold_page_health import evaluate_sold_verification

    ev = evaluate_sold_verification(url=url, title=title, body=body, http_status=http_status)
    if ev.get("verified"):
        return {
            "terminal": "SOLD_STATE_VERIFIED",
            "verified": True,
            "outcome": None,
            "soldFilterStateVerified": True,
            "soldPageHealthVerified": True,
            "x11SoldStateVerified": True,
            "marketplacePageClass": ev.get("marketplacePageClass"),
        }
    if ev.get("terminal"):
        return {
            "terminal": ev["terminal"],
            "verified": False,
            "outcome": TEMPORARY_EBAY_SERVER_FAILURE
            if ev.get("terminal") in {"EBAY_ERROR_PAGE", "EBAY_SORRY"}
            else classified.get("outcome"),
            "sorry": ev.get("terminal") == "EBAY_SORRY",
            "errorPage": ev.get("terminal") == "EBAY_ERROR_PAGE",
            "soldFilterStateVerified": ev.get("soldFilterStateVerified"),
            "soldPageHealthVerified": ev.get("soldPageHealthVerified"),
            "x11SoldStateVerified": False,
            "marketplacePageClass": ev.get("marketplacePageClass"),
        }
    return {
        "terminal": None,
        "verified": False,
        "outcome": None,
        "soldFilterStateVerified": ev.get("soldFilterStateVerified"),
        "soldPageHealthVerified": ev.get("soldPageHealthVerified"),
        "x11SoldStateVerified": False,
    }


def on_search_post_submit_page(
    state: SearchGateState,
    *,
    title: str | None,
    url: str | None,
    body: str | None = None,
    results_ok: bool = False,
    http_status: int | None = None,
    expected_query: str | None = None,
    sold_control_available: bool | None = None,
) -> SearchGateState:
    """Advance search gate after Enter/submit using observed page state."""
    classified = classify_post_navigation_page(
        title=title,
        url=url,
        body=body,
        query_visible_confirmed=state.query_visible,
        submitted=state.submitted,
        http_status=http_status,
        expected_query=expected_query,
        sold_control_available=sold_control_available,
    )
    state.route_class = str(classified.get("routeClass") or "") or None
    if classified.get("challenge"):
        state.phase = SearchPhase.EBAY_CHALLENGE
        state.note(EBAY_CHALLENGE_REQUIRED)
        return state
    if classified.get("accessDenied403"):
        state.phase = SearchPhase.EBAY_ACCESS_DENIED_403
        state.note(EBAY_ACCESS_DENIED_403)
        return state
    if classified.get("sorry") or classified.get("errorPage"):
        state.phase = SearchPhase.TEMPORARY_EBAY_SERVER_FAILURE
        state.note(TEMPORARY_EBAY_SERVER_FAILURE)
        return state
    if classified.get("ebayLive"):
        state.phase = SearchPhase.EBAY_LIVE_RESULTS
        state.note(EBAY_LIVE_RESULTS)
        if not classified.get("surface", {}).get("soldControlAvailable"):
            state.note(SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE)
        return state
    if classified.get("alternateSurface"):
        state.phase = SearchPhase.ALTERNATE_EBAY_SURFACE
        state.note(ALTERNATE_EBAY_SURFACE)
        return state
    if results_ok or classified.get("ordinaryResults"):
        state.phase = SearchPhase.ORDINARY_RESULTS_CONFIRMED
        state.note(ORDINARY_RESULTS_CONFIRMED)
        # Keep legacy event for older report readers.
        state.note("SEARCH_RESULTS_CONFIRMED")
        return state
    if state.submitted and state.query_visible:
        state.note("search_results_not_confirmed_after_submit")
        return state
    state.phase = SearchPhase.SEARCH_INPUT_NOT_CONFIRMED
    state.note("SEARCH_INPUT_NOT_CONFIRMED")
    return state
