#!/usr/bin/env python3
"""Revert-proof AU eBay Error Page + variant pacing (real wiring)."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest, ProviderResult, SoldComp
from cardscanr_market_engine.providers.au_ebay_variant_resilience import (
    LIVE_EBAY_NAVIGATION_COUNT,
    is_generic_error_page_failure,
    is_generic_error_page_nav,
    should_stop_query_variants_after_healthy_capture,
)
from cardscanr_market_engine.providers.ebay_browser_provider import (
    EbayBrowserProviderConfig,
    EbayBrowserSoldCompsProvider,
    verify_sold_result_state,
)
from cardscanr_market_engine.providers.errors import ProviderTemporaryError
from cardscanr_market_engine.providers.linux_x11_ebay_nav import LinuxNavResult
from cardscanr_market_engine.providers.query_builder import ProviderSearchQuery
from cardscanr_market_engine.providers.sold_page_health import (
    classify_sold_page_health,
    evaluate_sold_verification,
    is_tiny_ebay_generic_error_body,
)

NOW = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)
SOLD_URL = "https://www.ebay.com.au/sch/i.html?_nkw=Nidoking&LH_Sold=1"
HEALTHY_TITLE = "Nidoking 11 base Pokemon for sale | eBay"
ERROR_TITLE = "Error Page | eBay"
HEALTHY_BODY = "Sold items\n" + ("Sold 12 Nov 2026\nAU $4.20\nNidoking\n" * 25)
REAL_GENERIC_ERROR_BODY = (
    "Sorry, something went wrong on our end.\n"
    "Please try to check back later.\n"
    "reference id: 0.4540db17.1795129116.2353a0b\n"
)
CLASSIC_SORRY_BODY = (
    "SORRY\n"
    "Something went wrong on our end. We are working on it. Please try again later.\n"
)
CLASSIC_SORRY_TITLE = "Sorry! Something went wrong on our end | eBay"
CHALLENGE_TITLE = "Please verify yourself"


def _au_request() -> ProviderRequest:
    market = resolve_marketplace_config(market_country="AU", currency="AUD", marketplace="ebay")
    key = MarketPriceKey(
        id="pk-nidoking",
        game="pokemon",
        card_name="Nidoking",
        normalized_card_name="nidoking",
        set_name="Base",
        set_code="BASE1",
        collector_number="11",
        language="en",
        variant="normal",
        condition="near_mint",
        market_country="au",
        currency="aud",
        fingerprint="nidoking-test",
        raw={},
    )
    return ProviderRequest(
        price_key=key,
        market_country=market.market_country,
        currency=market.currency,
        marketplace=market.marketplace,
        provider_marketplace_id=market.provider_marketplace_id,
        provider_domain=market.provider_domain,
        search_locale=market.search_locale,
        display_name=market.display_name,
        market_config=market,
    )


def _healthy_provider_result(*, query_text: str, clean_included: int = 5) -> ProviderResult:
    comps = [
        SoldComp(
            source_listing_id="id0",
            title="Nidoking 11",
            sold_price=4.0,
            shipping_price=0.0,
            total_price=4.0,
            currency="AUD",
            sold_date=None,
            listing_url="https://www.ebay.com.au/itm/0",
            condition_text="Used",
            raw_metadata={},
        )
    ]
    return ProviderResult(
        provider_name="ebay_browser",
        marketplace="EBAY_AU",
        provider_fingerprint="fp",
        query_used=query_text,
        comps=comps,
        raw_metadata={
            "x11SoldStateVerified": True,
            "postSoldCapturePhase": "POST_SOLD_CAPTURE_READY",
            "cleanIncludedCount": clean_included,
            "desktopNav": {
                "SOLD_STATE_VERIFIED": True,
                "title": HEALTHY_TITLE,
                "url": SOLD_URL,
            },
        },
    )


def _nidoking_error_exc(query_text: str) -> ProviderTemporaryError:
    sold = {
        "title": ERROR_TITLE,
        "url": SOLD_URL,
        "sorry": True,
        "error": "ebay_error_page",
        "body": REAL_GENERIC_ERROR_BODY,
        "bodySample": REAL_GENERIC_ERROR_BODY,
        "SOLD_STATE_VERIFIED": False,
    }
    desktop_nav = {
        "title": ERROR_TITLE,
        "url": SOLD_URL,
        "sorry": True,
        "error": "ebay_error_page",
        "sold": sold,
        "search": {"title": HEALTHY_TITLE, "submitted": True},
    }
    return ProviderTemporaryError(
        "TEMPORARY_EBAY_SERVER_FAILURE: eBay SORRY/error page during desktop navigation",
        diagnostics={
            "reason": "ebay_error_page",
            "title": ERROR_TITLE,
            "url": SOLD_URL,
            "body": REAL_GENERIC_ERROR_BODY,
            "desktopNav": desktop_nav,
            "query_text": query_text,
        },
    )


class WiringSoldVerificationTests(unittest.TestCase):
    """Reverts: verify_sold_result_state block, sold_page_health tiny-body."""

    def test_verify_sold_result_state_rejects_lh_sold_error_page(self) -> None:
        state = verify_sold_result_state(url=SOLD_URL, title=ERROR_TITLE, body_text=REAL_GENERIC_ERROR_BODY)
        self.assertFalse(state["SOLD_STATE_VERIFIED"])

    def test_sold_page_health_tiny_body_blocks_health(self) -> None:
        health = classify_sold_page_health(url=SOLD_URL, title=HEALTHY_TITLE, body=REAL_GENERIC_ERROR_BODY)
        self.assertFalse(health["soldPageHealthVerified"])
        ev = evaluate_sold_verification(url=SOLD_URL, title=ERROR_TITLE, body=REAL_GENERIC_ERROR_BODY)
        self.assertFalse(ev["verified"])

    def test_real_ebay_generic_error_body_fixture(self) -> None:
        self.assertTrue(is_tiny_ebay_generic_error_body(REAL_GENERIC_ERROR_BODY))
        self.assertLess(len(REAL_GENERIC_ERROR_BODY.strip()), 220)
        self.assertIn("sorry", REAL_GENERIC_ERROR_BODY.lower())


class WiringGenericErrorClassificationTests(unittest.TestCase):
    """Reverts: is_generic_error_page_nav positive markers / hard stops."""

    def test_real_nidoking_error_body_with_sorry_headline_is_generic(self) -> None:
        self.assertTrue(
            is_generic_error_page_nav(
                title=ERROR_TITLE,
                url=SOLD_URL,
                body=REAL_GENERIC_ERROR_BODY,
                sorry=True,
                challenge=False,
                http_status=403,
            )
        )

    def test_classic_sorry_splash_is_hard_stop_not_generic(self) -> None:
        self.assertFalse(
            is_generic_error_page_nav(
                title=CLASSIC_SORRY_TITLE,
                url=SOLD_URL,
                body=CLASSIC_SORRY_BODY,
                sorry=True,
                challenge=False,
            )
        )

    def test_challenge_page_is_hard_stop_not_generic(self) -> None:
        self.assertFalse(
            is_generic_error_page_nav(
                title=CHALLENGE_TITLE,
                url=SOLD_URL,
                body="Please complete the captcha challenge to continue.",
                challenge=True,
            )
        )

    def test_nidoking_exc_enables_fallback_classifier(self) -> None:
        exc = _nidoking_error_exc("Nidoking BASE1 11 Pokemon")
        self.assertTrue(is_generic_error_page_failure(exc))


class WiringGuiSoldLhSoldShortcutTests(unittest.TestCase):
    """Reverts: linux_x11_ebay_sold LH_Sold=1 error-page check."""

    def test_lh_sold_shortcut_runs_error_page_branch(self) -> None:
        xlib = mock.MagicMock()
        fake_search = mock.MagicMock()
        fake_search.ART = ROOT / "reports" / "artifacts"
        fake_search.PREFIX = ROOT
        fake_search.omnibox_url.return_value = SOLD_URL
        fake_search.title.return_value = ERROR_TITLE
        fake_search.chrome_geom.return_value = {"x": 0, "y": 0, "w": 100, "h": 100}
        fake_search.chrome_owner_count.return_value = 1
        fake_image = mock.MagicMock()
        mods = {
            "Xlib": xlib,
            "Xlib.X": xlib,
            "Xlib.display": xlib,
            "PIL.Image": fake_image,
            "linux_x11_ebay_search": fake_search,
        }
        tools_path = str(ROOT / "tools")
        with mock.patch.dict(sys.modules, mods):
            for key in list(sys.modules):
                if key == "linux_x11_ebay_sold" or key.endswith(".linux_x11_ebay_sold"):
                    del sys.modules[key]
            sys.path.insert(0, tools_path)
            try:
                import linux_x11_ebay_sold as sold_mod  # type: ignore[import-not-found]

                gate_state = SimpleNamespace(phase=sold_mod.SoldPhase.EBAY_SORRY, events=[])
                with (
                    mock.patch.object(sold_mod, "on_sold_terminal", side_effect=lambda gate, **kw: gate),
                    mock.patch.object(sold_mod, "SoldGateState", return_value=gate_state),
                    mock.patch.object(sold_mod, "_env"),
                    mock.patch.object(sold_mod, "activate"),
                    mock.patch.object(sold_mod, "chrome_geom", return_value={"x": 0, "y": 0, "w": 100, "h": 100}),
                    mock.patch.object(sold_mod, "copy_body", return_value=REAL_GENERIC_ERROR_BODY),
                    mock.patch.object(sold_mod, "omnibox_url", return_value=SOLD_URL),
                    mock.patch.object(sold_mod, "title", return_value=ERROR_TITLE),
                    mock.patch.object(sold_mod, "chrome_owner_count", return_value=1),
                ):
                    out = sold_mod.gui_sold(tag="nidoking_shortcut_test")
            finally:
                if tools_path in sys.path:
                    sys.path.remove(tools_path)
        self.assertFalse(out["SOLD_STATE_VERIFIED"])
        self.assertEqual(out["title"], ERROR_TITLE)
        self.assertTrue(out["sorry"])
        self.assertEqual(out["error"], "ebay_error_page")
        self.assertIn("0.4540db17", out["body"])


class WiringFetchSerialTests(unittest.TestCase):
    """Reverts: variant stop/sleep, fallback, _live_ebay_navigation_count on failure."""

    def test_serial_stops_after_healthy_capture_without_third_fetch(self) -> None:
        provider = EbayBrowserSoldCompsProvider(config=EbayBrowserProviderConfig.from_env())
        request = _au_request()
        calls: list[str] = []

        def _playwright(*, request: ProviderRequest, search_query: ProviderSearchQuery) -> ProviderResult:
            calls.append(search_query.query_text)
            return _healthy_provider_result(query_text=search_query.query_text, clean_included=5)

        with (
            mock.patch.dict("os.environ", {"EBAY_BROWSER_MAX_QUERY_ATTEMPTS": "5"}, clear=False),
            mock.patch(
                "cardscanr_market_engine.providers.ebay_browser_provider.build_provider_search_queries",
                return_value=[
                    ProviderSearchQuery(
                        query_text="Nidoking 11 base Pokemon",
                        include_terms=(),
                        exclude_terms=(),
                        provider_domain="ebay.com.au",
                        provider_marketplace_id="EBAY_AU",
                        search_url="",
                        currency="AUD",
                        market_country="AU",
                        query_index=0,
                        query_source="broad",
                    ),
                    ProviderSearchQuery(
                        query_text="Nidoking BASE1 11 Pokemon",
                        include_terms=(),
                        exclude_terms=(),
                        provider_domain="ebay.com.au",
                        provider_marketplace_id="EBAY_AU",
                        search_url="",
                        currency="AUD",
                        market_country="AU",
                        query_index=1,
                        query_source="set_code",
                    ),
                ],
            ),
            mock.patch.object(provider, "_fetch_with_playwright", side_effect=_playwright),
            mock.patch(
                "cardscanr_market_engine.providers.au_ebay_variant_resilience.variant_inter_attempt_delay_seconds",
                return_value=0.0,
            ),
        ):
            result = provider._fetch_comps_serial(request)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.raw_metadata.get("queryStopReason"), "au_stop_after_healthy_sold_capture")

    def test_nidoking_variant1_error_falls_back_to_variant0(self) -> None:
        provider = EbayBrowserSoldCompsProvider(config=EbayBrowserProviderConfig.from_env())
        request = _au_request()

        def _playwright(*, request: ProviderRequest, search_query: ProviderSearchQuery) -> ProviderResult:
            if search_query.query_index == 0:
                return _healthy_provider_result(query_text=search_query.query_text, clean_included=1)
            raise _nidoking_error_exc(search_query.query_text)

        with (
            mock.patch.dict(
                "os.environ",
                {"EBAY_BROWSER_MAX_QUERY_ATTEMPTS": "5", "AU_EBAY_MIN_COMPARABLE_COUNT": "2"},
                clear=False,
            ),
            mock.patch(
                "cardscanr_market_engine.providers.ebay_browser_provider.build_provider_search_queries",
                return_value=[
                    ProviderSearchQuery(
                        query_text="Nidoking 11 base Pokemon",
                        include_terms=(),
                        exclude_terms=(),
                        provider_domain="ebay.com.au",
                        provider_marketplace_id="EBAY_AU",
                        search_url="",
                        currency="AUD",
                        market_country="AU",
                        query_index=0,
                        query_source="broad",
                    ),
                    ProviderSearchQuery(
                        query_text="Nidoking BASE1 11 Pokemon",
                        include_terms=(),
                        exclude_terms=(),
                        provider_domain="ebay.com.au",
                        provider_marketplace_id="EBAY_AU",
                        search_url="",
                        currency="AUD",
                        market_country="AU",
                        query_index=1,
                        query_source="set_code",
                    ),
                ],
            ),
            mock.patch.object(provider, "_fetch_with_playwright", side_effect=_playwright),
            mock.patch(
                "cardscanr_market_engine.providers.au_ebay_variant_resilience.variant_inter_attempt_delay_seconds",
                return_value=0.0,
            ),
        ):
            result = provider._fetch_comps_serial(request)
        self.assertEqual(result.raw_metadata.get("queryStopReason"), "au_variant_generic_error_fallback")
        self.assertTrue(result.raw_metadata.get("auVariantGenericErrorFallback"))
        self.assertGreater(len(result.comps), 0)

    def test_serial_failure_carries_live_navigation_count(self) -> None:
        provider = EbayBrowserSoldCompsProvider(config=EbayBrowserProviderConfig.from_env())
        request = _au_request()

        def _fail_with_nav_count(*_a, **_k):
            provider._live_ebay_navigation_count = 2
            raise _nidoking_error_exc("Nidoking BASE1 11 Pokemon")

        with (
            mock.patch.dict("os.environ", {"EBAY_BROWSER_MAX_QUERY_ATTEMPTS": "1"}, clear=False),
            mock.patch.object(provider, "_fetch_with_playwright", side_effect=_fail_with_nav_count),
        ):
            with self.assertRaises(ProviderTemporaryError) as ctx:
                provider._fetch_comps_serial(request)
        self.assertEqual(ctx.exception.diagnostics.get(LIVE_EBAY_NAVIGATION_COUNT), 2)


class WiringDesktopNavTests(unittest.TestCase):
    """Reverts: single-layer desktop retry + reserve before extra nav."""

    def test_desktop_generic_error_retries_once_with_one_sleep(self) -> None:
        provider = EbayBrowserSoldCompsProvider(config=EbayBrowserProviderConfig.from_env())
        request = _au_request()
        search_query = ProviderSearchQuery(
            query_text="Nidoking BASE1 11",
            include_terms=(),
            exclude_terms=(),
            provider_domain="ebay.com.au",
            provider_marketplace_id="EBAY_AU",
            search_url="",
            currency="AUD",
            market_country="AU",
            query_index=1,
            query_source="set_code",
        )
        error_nav = LinuxNavResult(
            ok=False,
            query=search_query.query_text,
            search_success=True,
            sold_click_success=False,
            sold_state_verified=False,
            url=SOLD_URL,
            title=ERROR_TITLE,
            sorry=True,
            challenge=False,
            sold_date_lines=0,
            error="ebay_error_page",
            diagnostics={
                "sold": {
                    "title": ERROR_TITLE,
                    "sorry": True,
                    "error": "ebay_error_page",
                    "body": REAL_GENERIC_ERROR_BODY,
                },
                "search": {"submitted": True},
            },
        )
        ok_nav = LinuxNavResult(
            ok=False,
            query=search_query.query_text,
            search_success=True,
            sold_click_success=False,
            sold_state_verified=False,
            url=SOLD_URL,
            title=ERROR_TITLE,
            sorry=True,
            challenge=False,
            sold_date_lines=0,
            error="ebay_error_page",
            diagnostics={"sold": {"title": ERROR_TITLE, "error": "ebay_error_page", "body": REAL_GENERIC_ERROR_BODY}},
        )
        nav_calls: list[int] = []

        def _nav(*_a, **_k):
            nav_calls.append(1)
            return error_nav if len(nav_calls) == 1 else ok_nav

        sleeps: list[float] = []

        with (
            mock.patch.object(provider, "_linux_x11_nav_mode_enabled", return_value=True),
            mock.patch.object(provider, "_ensure_linux_chrome_cdp_ready", return_value=9444),
            mock.patch(
                "cardscanr_market_engine.providers.linux_x11_ebay_nav.navigate_query_to_sold",
                side_effect=_nav,
            ),
            mock.patch(
                "cardscanr_market_engine.providers.au_ebay_variant_resilience.generic_error_page_retry_delay_seconds",
                return_value=0.01,
            ),
            mock.patch("cardscanr_market_engine.providers.ebay_browser_provider.time.sleep", side_effect=lambda s: sleeps.append(s)),
            mock.patch.object(provider, "_reserve_live_navigation_slot", autospec=True) as reserve_mock,
        ):
            reserve_mock.return_value = None
            with self.assertRaises(ProviderTemporaryError) as ctx:
                provider._fetch_with_desktop_win32(request=request, search_query=search_query)
        self.assertEqual(len(nav_calls), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(is_generic_error_page_failure(ctx.exception))
        self.assertGreaterEqual(reserve_mock.call_count, 2)
        self.assertEqual(provider._live_ebay_navigation_count, 2)
        self.assertEqual(ctx.exception.diagnostics.get(LIVE_EBAY_NAVIGATION_COUNT), 2)

    def test_reserve_called_before_second_navigation_not_first_with_pre_reserved(self) -> None:
        provider = EbayBrowserSoldCompsProvider(config=EbayBrowserProviderConfig.from_env())
        budget = mock.Mock()
        budget.budget_exhausted.return_value = None
        budget.reserve_live_submission.return_value = None
        provider.bind_live_submission_budget(budget, first_slot_pre_reserved=True)
        provider._live_ebay_navigation_count = 0
        provider._reserve_live_navigation_slot(market_country="AU")
        provider._live_ebay_navigation_count = 1
        provider._reserve_live_navigation_slot(market_country="AU")
        self.assertEqual(budget.reserve_live_submission.call_count, 1)

    def test_reserve_wired_before_navigate_in_desktop_fetch(self) -> None:
        """Removing _reserve_live_navigation_slot call in _run_desktop_nav_with_budget fails this."""
        provider = EbayBrowserSoldCompsProvider(config=EbayBrowserProviderConfig.from_env())
        provider.bind_live_submission_budget(mock.Mock(budget_exhausted=mock.Mock(return_value=None)), first_slot_pre_reserved=False)
        request = _au_request()
        search_query = ProviderSearchQuery(
            query_text="Nidoking BASE1 11",
            include_terms=(),
            exclude_terms=(),
            provider_domain="ebay.com.au",
            provider_marketplace_id="EBAY_AU",
            search_url="",
            currency="AUD",
            market_country="AU",
            query_index=0,
            query_source="set_code",
        )
        error_nav = LinuxNavResult(
            ok=False,
            query=search_query.query_text,
            search_success=True,
            sold_click_success=False,
            sold_state_verified=False,
            url=SOLD_URL,
            title=ERROR_TITLE,
            sorry=True,
            challenge=False,
            sold_date_lines=0,
            error="ebay_error_page",
            diagnostics={"sold": {"body": REAL_GENERIC_ERROR_BODY, "title": ERROR_TITLE}},
        )
        call_order: list[str] = []

        def _reserve(**_kwargs: object) -> None:
            call_order.append("reserve")
            return None

        def _nav(*_a, **_k):
            call_order.append("navigate")
            return error_nav

        with (
            mock.patch.object(provider, "_linux_x11_nav_mode_enabled", return_value=True),
            mock.patch.object(provider, "_ensure_linux_chrome_cdp_ready", return_value=9444),
            mock.patch(
                "cardscanr_market_engine.providers.linux_x11_ebay_nav.navigate_query_to_sold",
                side_effect=_nav,
            ),
            mock.patch.object(provider, "_reserve_live_navigation_slot", side_effect=_reserve),
            mock.patch(
                "cardscanr_market_engine.providers.au_ebay_variant_resilience.generic_error_page_retry_delay_seconds",
                return_value=0.0,
            ),
        ):
            with self.assertRaises(ProviderTemporaryError):
                provider._fetch_with_desktop_win32(request=request, search_query=search_query)
        self.assertEqual(call_order[:2], ["reserve", "navigate"])
        self.assertGreaterEqual(provider._live_ebay_navigation_count, 1)


class WiringCapLedgerTests(unittest.TestCase):
    """Reverts: bind_live_submission_budget + in-provider reserve before extra nav."""

    def test_cap_two_with_one_prior_blocks_second_variant_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=2, max_submissions_per_day=2)
            budget.record_submission(now=NOW, market="AU")
            budget.record_submission(now=NOW, market="AU")
            self.assertEqual(len(budget.submission_events), 2)

            provider = EbayBrowserSoldCompsProvider(config=EbayBrowserProviderConfig.from_env())
            provider.bind_live_submission_budget(budget, first_slot_pre_reserved=True)

            blocked = provider._reserve_live_navigation_slot(market_country="AU")
            self.assertIsNone(blocked)
            provider._live_ebay_navigation_count = 1
            blocked_second = provider._reserve_live_navigation_slot(market_country="AU")
            self.assertEqual(blocked_second, "HOURLY_SUBMISSION_CEILING")
            budget.reload(ledger)
            self.assertEqual(len(budget.submission_events), 2)

    def test_serial_cap_fallback_uses_last_healthy_at_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=2, max_submissions_per_day=2)
            budget.record_submission(now=NOW, market="AU")
            budget.record_submission(now=NOW, market="AU")

            provider = EbayBrowserSoldCompsProvider(config=EbayBrowserProviderConfig.from_env())
            provider.bind_live_submission_budget(budget, first_slot_pre_reserved=True)
            request = _au_request()

            def _playwright(*, request: ProviderRequest, search_query: ProviderSearchQuery) -> ProviderResult:
                if search_query.query_index == 0:
                    return _healthy_provider_result(query_text=search_query.query_text, clean_included=1)
                provider._live_ebay_navigation_count = 1
                raise ProviderTemporaryError(
                    "blocked",
                    diagnostics={
                        "budgetBlocked": "HOURLY_SUBMISSION_CEILING",
                        "desktopNav": {},
                    },
                )

            with (
                mock.patch.dict(
                    "os.environ",
                    {"EBAY_BROWSER_MAX_QUERY_ATTEMPTS": "5", "AU_EBAY_MIN_COMPARABLE_COUNT": "10"},
                    clear=False,
                ),
                mock.patch(
                    "cardscanr_market_engine.providers.ebay_browser_provider.build_provider_search_queries",
                    return_value=[
                        ProviderSearchQuery(
                            query_text="Nidoking 11 base Pokemon",
                            include_terms=(),
                            exclude_terms=(),
                            provider_domain="ebay.com.au",
                            provider_marketplace_id="EBAY_AU",
                            search_url="",
                            currency="AUD",
                            market_country="AU",
                            query_index=0,
                            query_source="broad",
                        ),
                        ProviderSearchQuery(
                            query_text="Nidoking BASE1 11 Pokemon",
                            include_terms=(),
                            exclude_terms=(),
                            provider_domain="ebay.com.au",
                            provider_marketplace_id="EBAY_AU",
                            search_url="",
                            currency="AUD",
                            market_country="AU",
                            query_index=1,
                            query_source="set_code",
                        ),
                    ],
                ),
                mock.patch.object(provider, "_fetch_with_playwright", side_effect=_playwright),
                mock.patch(
                    "cardscanr_market_engine.providers.au_ebay_variant_resilience.variant_inter_attempt_delay_seconds",
                    return_value=0.0,
                ),
            ):
                result = provider._fetch_comps_serial(request)
            self.assertEqual(len(budget.submission_events), 2)
            self.assertEqual(result.raw_metadata.get("queryStopReason"), "au_variant_cap_fallback")


class WiringVariantStopUsesCleanIncludedTests(unittest.TestCase):
    def test_stop_uses_clean_included_not_raw_comp_count(self) -> None:
        request = _au_request()
        many_comps = _healthy_provider_result(query_text="q", clean_included=0)
        many_comps = ProviderResult(
            provider_name=many_comps.provider_name,
            marketplace=many_comps.marketplace,
            provider_fingerprint=many_comps.provider_fingerprint,
            query_used=many_comps.query_used,
            comps=[many_comps.comps[0]] * 20,
            raw_metadata=dict(many_comps.raw_metadata, cleanIncludedCount=0),
        )
        pair = (SimpleNamespace(), many_comps)
        self.assertFalse(should_stop_query_variants_after_healthy_capture(last_healthy=pair, request=request))


if __name__ == "__main__":
    unittest.main()