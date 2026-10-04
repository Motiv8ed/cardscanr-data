"""Deterministic tests for Linux X11 eBay GUI state-machine guards."""
from __future__ import annotations

from cardscanr_market_engine.owned_daily_outcomes import (
    ALTERNATE_EBAY_SURFACE as OUTCOME_ALTERNATE,
    LOCAL_SEARCH_SURFACE_STATE_LEAK,
    TEMPORARY_EBAY_SERVER_FAILURE as OUTCOME_TEMPORARY_EBAY,
    classify_exception_outcome,
)
from cardscanr_market_engine.providers.errors import ProviderTemporaryError
from cardscanr_market_engine.providers.linux_x11_gui_diagnostics import (
    GuiAttemptTimings,
    build_post_navigation_snapshot,
    build_pre_submit_snapshot,
)
from cardscanr_market_engine.providers.linux_x11_gui_fsm import (
    ALTERNATE_EBAY_SURFACE,
    EBAY_ACCESS_DENIED_403,
    EBAY_CHALLENGE_REQUIRED,
    EBAY_LIVE_RESULTS,
    FORBIDDEN_WHILE_SOLD_PENDING,
    LOCAL_GUI_FAILURE,
    ORDINARY_RESULTS_CONFIRMED,
    SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE,
    TEMPORARY_EBAY_SERVER_FAILURE,
    SearchGateState,
    SearchPhase,
    SoldGateState,
    SoldPhase,
    classify_post_navigation_page,
    classify_post_sold_url,
    classify_search_surface,
    is_ebay_sorry_page,
    may_perform_browser_action,
    may_submit_search,
    on_query_visibility,
    on_search_post_submit_page,
    on_search_surface_validated,
    on_sold_clicked,
    on_sold_terminal,
    on_submit_attempt,
    page_is_about_blank,
    sold_click_coords_valid,
)


def test_query_not_visible_blocks_submit():
    state = SearchGateState()
    state = on_query_visibility(state, visible=False)
    # first failure allows refocus, still not confirmed
    assert state.phase == SearchPhase.SEARCH_FIELD_FOCUS_PROBE
    assert not may_submit_search(state)
    allowed, state = on_submit_attempt(state)
    assert allowed is False
    assert not state.submitted


def test_first_focus_failure_allows_one_refocus():
    state = SearchGateState()
    ok, state, _ = on_search_surface_validated(state, url="https://www.ebay.com.au/")
    assert ok
    state = on_query_visibility(state, visible=False)
    assert state.refocus_attempts == 1
    assert state.phase == SearchPhase.SEARCH_FIELD_FOCUS_PROBE
    state = on_query_visibility(state, visible=True)
    assert state.phase == SearchPhase.QUERY_VISIBLE_CONFIRMED
    assert may_submit_search(state)


def test_second_focus_failure_fails_safely():
    state = SearchGateState()
    state = on_query_visibility(state, visible=False)
    state = on_query_visibility(state, visible=False)
    assert state.phase == SearchPhase.SEARCH_INPUT_NOT_CONFIRMED
    assert not may_submit_search(state)
    allowed, state = on_submit_attempt(state)
    assert allowed is False


def test_sold_pending_blocks_next_navigation():
    state = SoldGateState()
    state = on_sold_clicked(state)
    assert state.pending is True
    assert state.phase == SoldPhase.SOLD_NAVIGATION_PENDING
    for action in FORBIDDEN_WHILE_SOLD_PENDING:
        assert may_perform_browser_action(state, action) is False


def test_sold_verified_allows_next_action():
    state = SoldGateState()
    state = on_sold_clicked(state)
    state = on_sold_terminal(state, verified=True)
    assert state.pending is False
    assert state.phase == SoldPhase.SOLD_STATE_VERIFIED
    assert may_perform_browser_action(state, "begin_next_card") is True
    assert may_perform_browser_action(state, "navigate_homepage") is True


def test_about_blank_aborts_card():
    assert page_is_about_blank("about:blank", "Untitled - Google Chrome")
    state = SoldGateState()
    state = on_sold_clicked(state)
    state = on_sold_terminal(state, about_blank=True)
    assert state.phase == SoldPhase.ABOUT_BLANK_ABORT
    assert state.pending is False
    classified = classify_post_sold_url("about:blank", "Untitled")
    assert classified["terminal"] == "ABOUT_BLANK_ABORT"


def test_sold_click_must_be_left_rail():
    assert sold_click_coords_valid(192, 473, win_y=50)
    assert not sold_click_coords_valid(410, 562, win_y=50)  # Magnezone bad click


def test_confirmed_query_may_submit():
    state = SearchGateState()
    ok, state, origin = on_search_surface_validated(
        state, url="https://www.ebay.com.au/", title="eBay Australia", scope_label="All Categories"
    )
    assert ok is True
    assert state.phase == SearchPhase.SEARCH_SURFACE_VALIDATED
    assert origin["approved"] is True
    state = on_query_visibility(state, visible=True)
    assert may_submit_search(state)
    allowed, state = on_submit_attempt(state)
    assert allowed is True
    assert state.phase == SearchPhase.SEARCH_NAVIGATION_PENDING
    assert "SEARCH_SUBMITTED" in state.events
    assert "SEARCH_NAVIGATION_PENDING" in state.events
    # cannot double-submit via may_submit
    assert not may_submit_search(state)


def test_query_visible_without_surface_validation_blocks_submit():
    state = SearchGateState()
    state = on_query_visibility(state, visible=True)
    assert state.query_visible is True
    assert state.surface_validated is False
    assert not may_submit_search(state)
    allowed, state = on_submit_attempt(state)
    assert allowed is False
    assert "submit_blocked_without_search_surface_validated" in state.events


def test_ebay_live_origin_is_surface_state_leak():
    state = SearchGateState()
    ok, state, origin = on_search_surface_validated(
        state,
        url="https://www.ebay.com.au/ebaylive/channels/NqET4XA8BzIgOIAR",
        title="Buy, Sell, and Save on eBay's Global Marketplace",
        scope_label="eBay Live",
    )
    assert ok is False
    assert origin["surfaceClass"] == "REJECTED_ORIGIN_EBAY_LIVE"
    assert state.phase == SearchPhase.LOCAL_SEARCH_SURFACE_STATE_LEAK
    assert LOCAL_SEARCH_SURFACE_STATE_LEAK in state.events
    assert not may_submit_search(state)


def test_homepage_scope_ebay_live_label_rejected():
    state = SearchGateState()
    ok, state, origin = on_search_surface_validated(
        state,
        url="https://www.ebay.com.au/",
        title="eBay Australia",
        scope_label="eBay Live",
    )
    assert ok is False
    assert origin["reason"] == "ebay_live_scope_label"
    assert state.phase == SearchPhase.LOCAL_SEARCH_SURFACE_STATE_LEAK


def test_about_blank_origin_rejected():
    state = SearchGateState()
    ok, state, origin = on_search_surface_validated(
        state, url="about:blank", title="Untitled - Google Chrome"
    )
    assert ok is False
    assert origin["surfaceClass"] == "REJECTED_ORIGIN_ABOUT_BLANK"


def test_required_sequence_surface_then_query_then_submit():
    state = SearchGateState()
    ok, state, _ = on_search_surface_validated(state, url="https://www.ebay.com.au/")
    assert ok and state.phase == SearchPhase.SEARCH_SURFACE_VALIDATED
    state = on_query_visibility(state, visible=True)
    assert state.phase == SearchPhase.QUERY_VISIBLE_CONFIRMED
    assert state.surface_validated is True
    allowed, state = on_submit_attempt(state)
    assert allowed and state.phase == SearchPhase.SEARCH_NAVIGATION_PENDING


def test_local_surface_leak_outcome_not_sorry_breaker():
    outcome = classify_exception_outcome(
        ProviderTemporaryError(
            "LOCAL_SEARCH_SURFACE_STATE_LEAK: search submitted or attempted from invalid eBay surface",
            diagnostics={
                "reason": "search_origin_ebay_live",
                "ownedDailyOutcome": LOCAL_SEARCH_SURFACE_STATE_LEAK,
            },
        ),
        diagnostics={
            "reason": "search_origin_ebay_live",
            "ownedDailyOutcome": LOCAL_SEARCH_SURFACE_STATE_LEAK,
        },
    )
    assert outcome == LOCAL_SEARCH_SURFACE_STATE_LEAK
    assert outcome != OUTCOME_TEMPORARY_EBAY
    assert outcome != "TEMPORARY_EBAY_SERVER_FAILURE"


def test_ordinary_sch_results_confirmed():
    url = (
        "https://www.ebay.com.au/sch/i.html?"
        "_nkw=Ceruledge+20+phantasmal+flames+Pokemon&_sacat=0&LH_TitleDesc=0"
    )
    surface = classify_search_surface(
        title="Ceruledge for sale | eBay",
        url=url,
        expected_query="Ceruledge 20 phantasmal flames Pokemon",
        query_visible_confirmed=True,
        submitted=True,
    )
    assert surface["routeClass"] == ORDINARY_RESULTS_CONFIRMED
    assert surface["ordinaryResults"] is True
    assert surface["tripsSorryBreaker"] is False

    state = SearchGateState()
    ok, state, _ = on_search_surface_validated(state, url="https://www.ebay.com.au/")
    assert ok
    state = on_query_visibility(state, visible=True)
    _, state = on_submit_attempt(state)
    state = on_search_post_submit_page(state, title="Ceruledge for sale | eBay", url=url, results_ok=True)
    assert state.phase == SearchPhase.ORDINARY_RESULTS_CONFIRMED


def test_ebaylive_search_is_alternate_not_local_gui_failure():
    url = (
        "https://www.ebay.com.au/ebaylive/search?"
        "_nkw=Ceruledge+20+phantasmal+flames+Pokemon"
        "&_sacat=-1&previousChannelId=NqET4XA8BzIgOIAR"
    )
    surface = classify_search_surface(
        title="Buy, Sell, and Save on eBay's Global Marketplace",
        url=url,
        expected_query="Ceruledge 20 phantasmal flames Pokemon",
        query_visible_confirmed=True,
        submitted=True,
        sold_control_available=False,
    )
    assert surface["routeClass"] == EBAY_LIVE_RESULTS
    assert surface["ebayLive"] is True
    assert surface["alternateSurface"] is True
    assert surface["tripsSorryBreaker"] is False
    assert surface["markFresh"] is False
    assert surface["retainLastGood"] is True
    assert surface["terminal"] == SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE
    assert surface["outcome"] == ALTERNATE_EBAY_SURFACE
    assert surface["routeClass"] != LOCAL_GUI_FAILURE

    state = SearchGateState()
    ok, state, _ = on_search_surface_validated(state, url="https://www.ebay.com.au/")
    assert ok
    state = on_query_visibility(state, visible=True)
    _, state = on_submit_attempt(state)
    state = on_search_post_submit_page(
        state,
        title="Buy, Sell, and Save on eBay's Global Marketplace",
        url=url,
        results_ok=False,
        sold_control_available=False,
    )
    assert state.phase == SearchPhase.EBAY_LIVE_RESULTS
    assert state.phase != SearchPhase.LOCAL_GUI_FAILURE

    outcome = classify_exception_outcome(
        ProviderTemporaryError(
            "ALTERNATE_EBAY_SURFACE: eBay Live/alternate search surface cannot provide Sold comps",
            diagnostics={"reason": "ebay_live_results", "ownedDailyOutcome": "ALTERNATE_EBAY_SURFACE"},
        ),
        diagnostics={"reason": "ebay_live_results", "ownedDailyOutcome": "ALTERNATE_EBAY_SURFACE"},
    )
    assert outcome == OUTCOME_ALTERNATE


def test_403_sorry_is_access_denied():
    title = "Error Page | eBay"
    url = "https://www.ebay.com.au/sch/i.html?_nkw=Iron+Bundle+62"
    body = "SORRY\nSomething went wrong on our end"
    surface = classify_search_surface(
        title=title,
        url=url,
        body=body,
        http_status=403,
        query_visible_confirmed=True,
        submitted=True,
    )
    assert surface["routeClass"] == EBAY_ACCESS_DENIED_403
    assert surface["accessDenied403"] is True
    assert surface["tripsSorryBreaker"] is True

    state = SearchGateState()
    ok, state, _ = on_search_surface_validated(state, url="https://www.ebay.com.au/")
    assert ok
    state = on_query_visibility(state, visible=True)
    _, state = on_submit_attempt(state)
    state = on_search_post_submit_page(state, title=title, url=url, body=body, http_status=403)
    assert state.phase == SearchPhase.EBAY_ACCESS_DENIED_403


def test_challenge_page_stops():
    surface = classify_search_surface(
        title="Please verify yourself",
        url="https://www.ebay.com.au/splashui/challenge",
        body="security measure captcha",
        query_visible_confirmed=True,
        submitted=True,
    )
    assert surface["routeClass"] == "EBAY_CHALLENGE"
    assert surface["challenge"] is True
    assert surface["outcome"] == EBAY_CHALLENGE_REQUIRED


def test_about_blank_after_submit():
    surface = classify_search_surface(
        title="Untitled - Google Chrome",
        url="about:blank",
        query_visible_confirmed=True,
        submitted=True,
    )
    assert surface["routeClass"] == "ABOUT_BLANK"
    assert surface["terminal"] == "ABOUT_BLANK_ABORT"


def test_unexpected_non_ebay_tab_after_submit():
    surface = classify_search_surface(
        title="New Tab",
        url="chrome://newtab/",
        query_visible_confirmed=True,
        submitted=True,
    )
    assert surface["routeClass"] == "SEARCH_RESULTS_NOT_CONFIRMED"


def test_navigation_timeout_sold_terminal():
    state = SoldGateState()
    state = on_sold_clicked(state)
    state = on_sold_terminal(state, timeout=True)
    assert state.phase == SoldPhase.SOLD_NAVIGATION_TIMEOUT
    assert state.pending is False


def test_sold_unavailable_on_alternate_surface():
    state = SoldGateState()
    state = on_sold_terminal(state, sold_unavailable_alternate=True)
    assert state.phase == SoldPhase.SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE
    sold = classify_post_sold_url(
        "https://www.ebay.com.au/ebaylive/search?_nkw=x",
        "Buy, Sell, and Save on eBay's Global Marketplace",
    )
    assert sold["terminal"] == SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE
    assert sold["verified"] is False


def test_finalize_timeout_safe_outcome():
    outcome = classify_exception_outcome(
        ProviderTemporaryError(
            "FINALIZE_TIMEOUT_SAFE",
            diagnostics={"ownedDailyOutcome": "FINALIZE_TIMEOUT_SAFE", "reason": "post_sold_finalize_timeout"},
        ),
        diagnostics={"ownedDailyOutcome": "FINALIZE_TIMEOUT_SAFE", "reason": "post_sold_finalize_timeout"},
    )
    assert outcome == "FINALIZE_TIMEOUT_SAFE"


def test_pre_post_submit_diagnostics_omit_secrets():
    pre = build_pre_submit_snapshot(
        current_url="https://www.ebay.com.au/",
        page_title="eBay Australia",
        query_expected="Ceruledge 20",
        query_visibly_confirmed=True,
        search_field_geometry={"click": [431, 201]},
        search_button_geometry={"xy": [953, 201]},
        selected_category_label="All Categories",
        fsm_state="QUERY_VISIBLE_CONFIRMED",
        search_origin_url="https://www.ebay.com.au/",
        search_surface_class="ORDINARY_MARKETPLACE_SEARCH",
        search_scope_label="All Categories",
        search_surface_validated=True,
    )
    post = build_post_navigation_snapshot(
        resulting_url="https://www.ebay.com.au/ebaylive/search?_nkw=Ceruledge",
        title="Buy, Sell, and Save on eBay's Global Marketplace",
        route_classification=EBAY_LIVE_RESULTS,
        ebay_live=True,
        ordinary_results=False,
        sold_control_available=False,
    )
    blob = str(pre) + str(post)
    assert "cookie" not in blob.lower()
    assert "authorization" not in blob.lower()
    assert "password" not in blob.lower()
    assert pre["queryVisiblyConfirmed"] is True
    assert pre.get("searchSurfaceValidated") is True or pre.get("searchSurfaceClass") is not None
    assert post["ebayLive"] is True


def test_gui_attempt_timings_elapsed():
    t = GuiAttemptTimings()
    t.mark("T0_job_claimed", 1000.0)
    t.mark("T5_search_submitted", 1005.0)
    t.mark("T14_job_finalized", 1010.0)
    elapsed = t.elapsed_ms()
    assert elapsed["T0_job_claimed"] == 0
    assert elapsed["T5_search_submitted"] == 5000
    assert elapsed["totalMs"] == 10000


def test_iron_bundle_error_page_is_temporary_ebay_server_failure():
    """Regression: Iron Bundle sv6/62 — Error Page | eBay after confirmed GUI submit.

    Error Page is distinct from classic SORRY but still TEMPORARY_EBAY_SERVER_FAILURE
    (not CAPTCHA, not CDP_TARGET_NOT_FOUND).
    """
    from cardscanr_market_engine.providers.sold_page_health import is_ebay_error_page

    title = "Error Page | eBay"
    url = (
        "https://www.ebay.com.au/sch/i.html?"
        "_nkw=Iron+Bundle+62+twilight+masquerade+Pokemon"
        "&_sacat=0&_from=R40&_trksid=m570.l1313"
    )
    body = "SORRY\nSomething went wrong on our end\n0.27672817.1790657862.5d4d0662"
    assert is_ebay_error_page(title=title, url=url, body=body)
    assert is_ebay_error_page(title=title, url=url)
    assert not is_ebay_sorry_page(title=title, url=url)

    state = SearchGateState()
    ok, state, _ = on_search_surface_validated(state, url="https://www.ebay.com.au/")
    assert ok
    state = on_query_visibility(state, visible=True)
    allowed, state = on_submit_attempt(state)
    assert allowed is True
    state = on_search_post_submit_page(state, title=title, url=url, body=body, results_ok=False)
    assert state.phase == SearchPhase.TEMPORARY_EBAY_SERVER_FAILURE
    assert TEMPORARY_EBAY_SERVER_FAILURE in state.events
    assert state.phase != SearchPhase.SEARCH_INPUT_NOT_CONFIRMED

    classified = classify_post_navigation_page(
        title=title,
        url=url,
        body=body,
        query_visible_confirmed=True,
        submitted=True,
    )
    assert classified.get("errorPage") is True
    assert classified["sorry"] is False
    assert classified["outcome"] == TEMPORARY_EBAY_SERVER_FAILURE
    assert classified["localGuiThroughSubmit"] is True

    sold = classify_post_sold_url(url, title, body)
    assert sold["terminal"] == "EBAY_ERROR_PAGE"
    assert sold["outcome"] == TEMPORARY_EBAY_SERVER_FAILURE
    assert sold.get("soldPageHealthVerified") is False
    assert sold.get("x11SoldStateVerified") is False


def test_iron_bundle_outcome_retains_last_good_no_freshness_success():
    exc = ProviderTemporaryError(
        "TEMPORARY_EBAY_SERVER_FAILURE: eBay SORRY/error page during desktop navigation",
        diagnostics={"reason": "ebay_sorry_error_page"},
    )
    outcome = classify_exception_outcome(exc, diagnostics=exc.diagnostics)
    assert outcome == OUTCOME_TEMPORARY_EBAY
    # Operational contract: failure outcome is not a healthy freshness success.
    assert outcome not in {
        "UPDATED_FROM_EBAY",
        "UNCHANGED_FROM_EBAY",
        "CHECKED_NO_NEW_EXACT_EVIDENCE",
    }
