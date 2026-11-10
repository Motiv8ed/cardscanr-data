"""Regression tests for 2026-11-06 AU eBay worker HARD_STOP (incomplete Sold + broken INTER_CARD tab)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
for _path in (str(ROOT), str(TOOLS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from cardscanr_market_engine.browser_lifecycle_policy import (
    INTER_CARD_RECOVERY_STRATEGY,
    RUNTIME_COLD_START,
    RUNTIME_INTER_CARD,
    PriorCardContext,
)
from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.continuous_worker_policy import HARD_STOP_OUTCOMES
from cardscanr_market_engine.ebay_availability import EbayAvailabilitySnapshot, save_availability
from cardscanr_market_engine.job_runner import (
    MarketPriceJobRunner,
    collect_inter_card_invalidation_reasons,
    maybe_invalidate_inter_card_after_failure,
)
from cardscanr_market_engine.models import MarketPriceKey, MarketPriceRefreshJob, ProviderRequest
from cardscanr_market_engine.navigation_runtime_context import (
    NavigationRuntimeContext,
    apply_context_to_environ,
    clear_context_from_environ,
    load_navigation_runtime_context,
)
from cardscanr_market_engine.owned_daily_outcomes import (
    CHALLENGE_REQUIRED,
    POST_SOLD_CAPTURE_FAILURE,
    classify_exception_outcome,
)
from cardscanr_market_engine.providers.ebay_browser_provider import (
    EbayBrowserSoldCompsProvider,
    EbayBrowserProviderConfig,
)
from cardscanr_market_engine.providers.errors import ProviderBlockedError, ProviderTemporaryError
from cardscanr_market_engine.providers.linux_x11_ebay_nav import LinuxNavResult, navigate_query_to_sold
from cardscanr_market_engine.providers.post_sold_capture import (
    CDP_DOCUMENT_NOT_READY,
    POST_SOLD_CAPTURE_FAILED,
    PageTargetCandidate,
    assess_document_readiness,
    capture_verified_sold_page,
    verify_captured_sold_state,
)
from cardscanr_market_engine.providers.linux_x11_gui_fsm import SoldGateState
from cardscanr_market_engine.providers.sold_page_health import (
    EBAY_CHALLENGE_PAGE,
    EBAY_SORRY_PAGE,
    classify_marketplace_page_class,
    evaluate_sold_verification,
)

SOLD_URL = (
    "https://www.ebay.com.au/sch/i.html?"
    "_nkw=Dragonite+5+wizards+black+star+promos+Pokemon&_sacat=0&LH_Sold=1"
)
TITLE_OK = "Dragonite 5 wizards black star promos Pokemon for sale | eBay"


def _au_key(name: str, *, job_suffix: str) -> MarketPriceKey:
    return MarketPriceKey(
        id=f"key-{job_suffix}",
        game="pokemon",
        card_name=name,
        normalized_card_name=name.lower(),
        set_name="Promos",
        set_code="wbsp",
        collector_number="5",
        language="en",
        variant="holo",
        condition="raw",
        market_country="au",
        currency="aud",
        fingerprint=f"pokemon|en|wbsp|5|{name.lower()}|holo|raw|au|aud",
    )


def _job(job_id: str, key: MarketPriceKey) -> MarketPriceRefreshJob:
    return MarketPriceRefreshJob(
        id=job_id,
        price_key_id=key.id,
        reason="incident_regression",
        priority=10,
        status="running",
        attempt_count=1,
    )


class _FakeClient:
    def __init__(self, price_key: MarketPriceKey) -> None:
        self.price_key = price_key

    def get_price_key(self, price_key_id: str) -> MarketPriceKey:
        return self.price_key

    def fail_job(self, **kwargs: object) -> dict:
        return {"status": "failed", **kwargs}

    def mark_cache_failure(self, **kwargs: object) -> dict:
        return dict(kwargs)

    def count_recent_same_failures(self, *, price_key_id: str, error_message: str) -> int:
        return 0


class _FailingProvider:
    provider_name = "ebay_browser"
    marketplace_name = "ebay"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def fetch_comps(self, request: ProviderRequest) -> object:
        raise self.exc


class RunnerIntegrationBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        avail = root / "ebay_availability_state.json"
        ops = root / "marketplace_ops_state.json"
        incidents = root / "control_plane_incidents.json"
        self._ctx_path = root / "nav_runtime_context.json"
        save_availability(
            EbayAvailabilitySnapshot(
                state="HEALTHY",
                market="AU",
                confirmed_healthy=True,
                updated_at=datetime(2026, 11, 6, tzinfo=timezone.utc),
            ),
            path=avail,
            force=True,
        )
        ops.write_text('{"version":1,"markets":{}}\n', encoding="utf-8")
        incidents.write_text('{"version":1,"incidents":{}}\n', encoding="utf-8")
        self._prev_env = dict(os.environ)
        os.environ["EBAY_AVAILABILITY_STATE_PATH"] = str(avail)
        os.environ["MARKET_OPS_STATE_PATH"] = str(ops)
        os.environ["CONTROL_PLANE_INCIDENTS_PATH"] = str(incidents)
        os.environ["MARKET_WORKER_ALLOWED_MARKETS"] = "AU"
        os.environ["CARDSCANR_NAV_CONTEXT_JSON_PATH"] = str(self._ctx_path)
        prior = PriorCardContext(
            job_id="prior-healthy",
            target_id="BROKEN_TAB",
            final_url=SOLD_URL,
            query="Dragonite",
            x11_sold_state_verified=True,
            capture_correlated=True,
            card_verdict="PASS_PRICE_UPDATED",
            market="AU",
            currency="AUD",
        )
        apply_context_to_environ(
            NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=prior)
        )
        NavigationRuntimeContext(
            runtime_mode=RUNTIME_INTER_CARD, expected_prior=prior
        ).persist(self._ctx_path)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._prev_env)
        clear_context_from_environ()
        self._tmpdir.cleanup()

    def _runner(self, key: MarketPriceKey) -> MarketPriceJobRunner:
        return MarketPriceJobRunner(
            client=_FakeClient(key),
            provider=_FailingProvider(ProviderTemporaryError("unused")),
            config=MarketEngineConfig.from_env(require_supabase=False),
            now_func=lambda: datetime(2026, 11, 6, 10, 25, tzinfo=timezone.utc),
            logger=lambda *_a, **_k: None,
        )


class IncompleteSoldEvidenceTests(unittest.TestCase):
    def test_url_title_only_with_tiny_body_does_not_verify(self) -> None:
        tiny_body = TITLE_OK + "\n" + ("x" * 180)
        ev = evaluate_sold_verification(
            url=SOLD_URL, title=TITLE_OK, body=tiny_body, authoritative_document=True
        )
        self.assertFalse(ev["verified"])
        self.assertFalse(ev["x11SoldStateVerified"])
        sold = verify_captured_sold_state(url=SOLD_URL, title=TITLE_OK, body_text=tiny_body)
        self.assertFalse(sold["SOLD_STATE_VERIFIED"])

    def test_empty_authoritative_body_never_verifies(self) -> None:
        ev = evaluate_sold_verification(
            url=SOLD_URL, title=TITLE_OK, body="", authoritative_document=True
        )
        self.assertFalse(ev["verified"])
        self.assertFalse(ev["x11SoldStateVerified"])

    def test_short_sold_marker_still_verifies(self) -> None:
        for body in (
            "Sold 6 Nov 2026\nDragonite AU $20",
            "Sold items",
            "0 results",
        ):
            ev = evaluate_sold_verification(
                url=SOLD_URL, title=TITLE_OK, body=body, authoritative_document=True
            )
            self.assertTrue(ev["verified"], msg=body)

    def test_cdp_empty_document_not_verified(self) -> None:
        def attach(_c: PageTargetCandidate) -> dict:
            return {
                "ok": False,
                "url": SOLD_URL,
                "title": TITLE_OK,
                "body_text": "",
                "error": "empty_document_body_after_fallback_chain",
                "readiness": assess_document_readiness(
                    {
                        "readyState": "complete",
                        "documentElementPresent": True,
                        "bodyPresent": True,
                        "bodyInnerTextLength": 209,
                        "outerHTMLLength": 0,
                        "frameUrl": SOLD_URL,
                        "listingNodeCount": 0,
                    }
                ),
                "methods": {"locator_inner_text": {"attempted": True, "length": 0}},
            }

        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query="Dragonite 5 wizards black star promos Pokemon",
            list_targets=lambda: [
                {"id": "t-dragonite", "type": "page", "url": SOLD_URL, "title": TITLE_OK}
            ],
            attach_and_read=attach,
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, CDP_DOCUMENT_NOT_READY)


class InterCardInvalidationSignalTests(unittest.TestCase):
    def test_desktop_nav_error_only_triggers_invalidation(self) -> None:
        reasons = collect_inter_card_invalidation_reasons(
            owned_outcome="TEMPORARY_BROWSER_FAILURE",
            result={"error": "Desktop sold navigation failed: INCOMPLETE_SOLD_DOCUMENT_EVIDENCE"},
            provider_diagnostics={
                "diagnostics": {
                    "desktopNav": {"error": "INCOMPLETE_SOLD_DOCUMENT_EVIDENCE"},
                }
            },
        )
        self.assertIn("incomplete_sold_document_evidence", reasons)

    def test_search_input_not_confirmed_triggers_invalidation(self) -> None:
        reasons = collect_inter_card_invalidation_reasons(
            owned_outcome="TEMPORARY_BROWSER_FAILURE",
            result={},
            provider_diagnostics={
                "diagnostics": {"desktopNav": {"error": "SEARCH_INPUT_NOT_CONFIRMED"}},
            },
        )
        self.assertIn("search_input_not_confirmed", reasons)


class JobRunnerIntegrationTests(RunnerIntegrationBase):
    def test_incomplete_sold_failure_invalidates_inter_card(self) -> None:
        key = _au_key("Dragonite", job_suffix="dragonite")
        exc = ProviderTemporaryError(
            "INCOMPLETE_SOLD_DOCUMENT_EVIDENCE: unreadable sold results",
            diagnostics={
                "ownedDailyOutcome": POST_SOLD_CAPTURE_FAILURE,
                "failureClass": "INCOMPLETE_SOLD_DOCUMENT_EVIDENCE",
                "incompleteSoldEvidence": True,
                "desktopNav": {
                    "error": "INCOMPLETE_SOLD_DOCUMENT_EVIDENCE",
                    "SOLD_STATE_VERIFIED": False,
                },
            },
        )
        runner = MarketPriceJobRunner(
            client=_FakeClient(key),
            provider=_FailingProvider(exc),
            config=MarketEngineConfig.from_env(require_supabase=False),
            now_func=lambda: datetime(2026, 11, 6, 10, 25, tzinfo=timezone.utc),
            logger=lambda *_a, **_k: None,
        )
        with mock.patch(
            "cardscanr_market_engine.navigation_runtime_context.DEFAULT_CONTEXT_PATH",
            self._ctx_path,
        ):
            result = runner.run_job(_job("job-dragonite", key))
        self.assertEqual(result["status"], "failed")
        loaded = load_navigation_runtime_context()
        self.assertEqual(loaded.runtime_mode, RUNTIME_COLD_START)
        self.assertIsNone(loaded.expected_prior)

    def test_challenge_from_nav_body_does_not_invalidate_but_hard_stops(self) -> None:
        key = _au_key("CaptchaCard", job_suffix="captcha")
        exc = ProviderBlockedError(
            "challenge",
            diagnostics={
                "providerOutcome": "challenge_detected",
                "desktopNav": {"challenge": True, "error": "EBAY_CHALLENGE"},
            },
        )
        runner = MarketPriceJobRunner(
            client=_FakeClient(key),
            provider=_FailingProvider(exc),
            config=MarketEngineConfig.from_env(require_supabase=False),
            now_func=lambda: datetime(2026, 11, 6, tzinfo=timezone.utc),
            logger=lambda *_a, **_k: None,
        )
        result = runner.run_job(_job("job-challenge", key))
        self.assertEqual(classify_exception_outcome(exc, diagnostics=exc.diagnostics), CHALLENGE_REQUIRED)
        self.assertEqual(result["ownedDailyOutcome"], CHALLENGE_REQUIRED)
        self.assertIn(CHALLENGE_REQUIRED, HARD_STOP_OUTCOMES)

    def test_dragonite_pikachu_mew_sequence_clears_poisoned_tab(self) -> None:
        with mock.patch(
            "cardscanr_market_engine.navigation_runtime_context.DEFAULT_CONTEXT_PATH",
            self._ctx_path,
        ):
            cards = [
                (
                    "Dragonite",
                    "job-dragonite",
                    ProviderTemporaryError(
                        "INCOMPLETE_SOLD_DOCUMENT_EVIDENCE",
                        diagnostics={
                            "ownedDailyOutcome": POST_SOLD_CAPTURE_FAILURE,
                            "failureClass": "INCOMPLETE_SOLD_DOCUMENT_EVIDENCE",
                            "incompleteSoldEvidence": True,
                            "desktopNav": {"error": "INCOMPLETE_SOLD_DOCUMENT_EVIDENCE"},
                        },
                    ),
                ),
                (
                    "Pikachu",
                    "job-pikachu",
                    ProviderTemporaryError(
                        "Desktop sold navigation failed: SEARCH_INPUT_NOT_CONFIRMED",
                        diagnostics={
                            "desktopNav": {"error": "SEARCH_INPUT_NOT_CONFIRMED", "searchSuccess": False},
                        },
                    ),
                ),
                (
                    "Mew",
                    "job-mew",
                    ProviderTemporaryError(
                        "Desktop sold navigation failed: SEARCH_INPUT_NOT_CONFIRMED",
                        diagnostics={
                            "desktopNav": {"error": "SEARCH_INPUT_NOT_CONFIRMED", "searchSuccess": False},
                        },
                    ),
                ),
            ]
            for name, job_id, exc in cards:
                key = _au_key(name, job_suffix=name.lower())
                runner = MarketPriceJobRunner(
                    client=_FakeClient(key),
                    provider=_FailingProvider(exc),
                    config=MarketEngineConfig.from_env(require_supabase=False),
                    now_func=lambda: datetime(2026, 11, 6, 10, 30, tzinfo=timezone.utc),
                    logger=lambda *_a, **_k: None,
                )
                prior = PriorCardContext(
                    job_id="prior",
                    target_id="BROKEN_TAB",
                    final_url=SOLD_URL,
                    query=name,
                    x11_sold_state_verified=True,
                    capture_correlated=True,
                    card_verdict="PASS_PRICE_UPDATED",
                    market="AU",
                    currency="AUD",
                )
                apply_context_to_environ(
                    NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=prior)
                )
                runner.run_job(_job(job_id, key))
                loaded = load_navigation_runtime_context()
                self.assertEqual(loaded.runtime_mode, RUNTIME_COLD_START, msg=name)
                self.assertIsNone(loaded.expected_prior, msg=name)


class ProviderIntegrationTests(unittest.TestCase):
    def _provider(self) -> EbayBrowserSoldCompsProvider:
        config = EbayBrowserProviderConfig.from_env()
        return EbayBrowserSoldCompsProvider(config=config)

    def _request(self) -> ProviderRequest:
        key = _au_key("Dragonite", job_suffix="dragonite")
        market = resolve_marketplace_config(market_country="AU", currency="AUD", marketplace="ebay")
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

    def _search_query(self):
        from cardscanr_market_engine.providers.query_builder import ProviderSearchQuery

        return ProviderSearchQuery(
            query_text="Dragonite 5 wizards black star promos Pokemon",
            include_terms=(),
            exclude_terms=(),
            provider_domain="ebay.com.au",
            provider_marketplace_id="ebay_au",
            search_url="https://www.ebay.com.au",
            currency="AUD",
            market_country="AU",
            query_index=0,
            query_source="primary",
        )

    @mock.patch(
        "cardscanr_market_engine.providers.ebay_browser_provider.EbayBrowserSoldCompsProvider._ensure_linux_chrome_cdp_ready",
        return_value=9444,
    )
    @mock.patch(
        "cardscanr_market_engine.providers.ebay_browser_provider.EbayBrowserSoldCompsProvider._linux_x11_nav_mode_enabled",
        return_value=True,
    )
    @mock.patch("cardscanr_market_engine.providers.linux_x11_ebay_nav.navigate_query_to_sold")
    def test_provider_promotes_incomplete_sold_nav_failure(
        self,
        mock_nav: mock.MagicMock,
        *_m: object,
    ) -> None:
        mock_nav.return_value = LinuxNavResult(
            ok=False,
            query="Dragonite 5 wizards black star promos Pokemon",
            search_success=True,
            sold_click_success=True,
            sold_state_verified=False,
            url=SOLD_URL,
            title=TITLE_OK,
            sorry=False,
            challenge=False,
            sold_date_lines=0,
            error="INCOMPLETE_SOLD_DOCUMENT_EVIDENCE",
            diagnostics={
                "sold": {
                    "failureClass": "INCOMPLETE_SOLD_DOCUMENT_EVIDENCE",
                    "bodyChars": 209,
                }
            },
        )
        provider = self._provider()
        with self.assertRaises(ProviderTemporaryError) as ctx:
            provider._fetch_with_desktop_win32(
                request=self._request(),
                search_query=self._search_query(),
            )
        diag = ctx.exception.diagnostics
        self.assertEqual(diag.get("ownedDailyOutcome"), POST_SOLD_CAPTURE_FAILURE)
        self.assertTrue(diag.get("incompleteSoldEvidence"))
        desktop = diag.get("desktopNav") or {}
        self.assertEqual(desktop.get("error"), "INCOMPLETE_SOLD_DOCUMENT_EVIDENCE")

    @mock.patch(
        "cardscanr_market_engine.providers.ebay_browser_provider.EbayBrowserSoldCompsProvider._ensure_linux_chrome_cdp_ready",
        return_value=9444,
    )
    @mock.patch(
        "cardscanr_market_engine.providers.ebay_browser_provider.EbayBrowserSoldCompsProvider._linux_x11_nav_mode_enabled",
        return_value=True,
    )
    @mock.patch("cardscanr_market_engine.providers.linux_x11_ebay_nav.navigate_query_to_sold")
    def test_provider_challenge_from_sold_nav_hard_stops(
        self,
        mock_nav: mock.MagicMock,
        *_m: object,
    ) -> None:
        mock_nav.return_value = LinuxNavResult(
            ok=False,
            query="Dragonite",
            search_success=True,
            sold_click_success=True,
            sold_state_verified=False,
            url=SOLD_URL,
            title="Please verify yourself",
            sorry=False,
            challenge=True,
            sold_date_lines=0,
            error="EBAY_CHALLENGE",
            diagnostics={"sold": {"challenge": True, "failureClass": "EBAY_CHALLENGE"}},
        )
        provider = self._provider()
        with self.assertRaises(ProviderBlockedError) as ctx:
            provider._fetch_with_desktop_win32(
                request=self._request(),
                search_query=self._search_query(),
            )
        self.assertEqual(ctx.exception.diagnostics.get("ownedDailyOutcome"), CHALLENGE_REQUIRED)


class GuiSoldIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._art_dir = Path(tempfile.mkdtemp(prefix="linux_sold_art_"))

    @mock.patch("linux_x11_ebay_sold.activate")
    @mock.patch(
        "linux_x11_ebay_sold.chrome_geom",
        return_value={"x": 0, "y": 0, "w": 1200, "h": 800, "wid": 1, "wid_hex": "0x1"},
    )
    @mock.patch("linux_x11_ebay_sold.chrome_owner_count", return_value=1)
    @mock.patch("linux_x11_ebay_sold.omnibox_url", return_value=SOLD_URL)
    @mock.patch("linux_x11_ebay_sold.title", return_value=TITLE_OK)
    @mock.patch("linux_x11_ebay_sold.copy_body", return_value="")
    def test_already_sold_empty_copy_not_verified(self, *_m: object) -> None:
        import linux_x11_ebay_sold
        from linux_x11_ebay_sold import gui_sold

        with mock.patch.object(linux_x11_ebay_sold, "ART", self._art_dir):
            out = gui_sold(tag="qa_empty")
        self.assertFalse(out["ok"])
        self.assertFalse(out["SOLD_STATE_VERIFIED"])
        self.assertEqual(out.get("error"), "INCOMPLETE_SOLD_DOCUMENT_EVIDENCE")

    @mock.patch("linux_x11_ebay_sold.activate")
    @mock.patch(
        "linux_x11_ebay_sold.chrome_geom",
        return_value={"x": 0, "y": 0, "w": 1200, "h": 800, "wid": 1, "wid_hex": "0x1"},
    )
    @mock.patch("linux_x11_ebay_sold.chrome_owner_count", return_value=1)
    @mock.patch("linux_x11_ebay_sold.omnibox_url", return_value=SOLD_URL)
    @mock.patch("linux_x11_ebay_sold.title", return_value=TITLE_OK)
    @mock.patch(
        "linux_x11_ebay_sold.copy_body",
        return_value="Please verify yourself — security challenge",
    )
    def test_already_sold_challenge_body_surfaces_challenge(self, *_m: object) -> None:
        import linux_x11_ebay_sold
        from linux_x11_ebay_sold import gui_sold

        with mock.patch.object(linux_x11_ebay_sold, "ART", self._art_dir):
            out = gui_sold(tag="qa_captcha")
        self.assertFalse(out["ok"])
        self.assertTrue(out["challenge"])
        self.assertEqual(out.get("error"), "EBAY_CHALLENGE")


def _mixed_case_marker(marker: str) -> str:
    out: list[str] = []
    upper = True
    for ch in marker:
        if ch.isalpha():
            out.append(ch.upper() if upper else ch.lower())
            upper = not upper
        else:
            out.append(ch)
    return "".join(out)


class CanonicalProviderSecuritySignalMatrixTests(unittest.TestCase):
    """Every canonical provider security signal must hard-stop Sold paths (never healthy)."""

    BODY_MODES: tuple[tuple[str, str | None], ...] = (
        ("empty", ""),
        ("whitespace", "   \n\t  "),
        ("partial", None),
        ("title_only_provisional", ""),
    )

    def _apply(self, *, title: str, url: str, body: str) -> dict[str, object]:
        from linux_x11_ebay_sold import _apply_authoritative_body_verification

        _gate = SoldGateState()
        _verified, _gate, fields = _apply_authoritative_body_verification(
            url=url,
            title=title,
            body=body,
            gate=_gate,
        )
        fields["verified"] = _verified
        return fields

    def _assert_security_hard_stop(
        self,
        *,
        expected_class: str,
        title: str,
        url: str,
        body: str,
        mode: str,
        marker: str,
    ) -> None:
        page_class = evaluate_sold_verification(
            url=url,
            title=title,
            body=body,
            authoritative_document=False,
        )
        auth_class = evaluate_sold_verification(
            url=url,
            title=title,
            body=body,
            authoritative_document=True,
        )
        apply_fields = self._apply(title=title, url=url, body=body)
        with self.subTest(marker=marker, mode=mode, expected=expected_class):
            self.assertEqual(
                classify_marketplace_page_class(url=url, title=title, body=body),
                expected_class,
            )
            self.assertFalse(page_class.get("verified"))
            self.assertFalse(auth_class.get("verified"))
            self.assertFalse(apply_fields.get("verified"))
            self.assertNotEqual(apply_fields.get("failureClass"), "INCOMPLETE_SOLD_DOCUMENT_EVIDENCE")
            if expected_class == EBAY_CHALLENGE_PAGE:
                self.assertEqual(apply_fields.get("failureClass"), "EBAY_CHALLENGE")
                self.assertTrue(apply_fields.get("challenge"))
            if expected_class == EBAY_SORRY_PAGE:
                self.assertEqual(apply_fields.get("failureClass"), EBAY_SORRY_PAGE)
                self.assertTrue(apply_fields.get("sorry"))

    def test_active_visible_text_markers_matrix(self) -> None:
        from cardscanr_market_engine.providers.marketplace_browser_page_state import (
            ACTIVE_CHALLENGE_VISIBLE_TEXT_MARKERS,
        )

        for marker in ACTIVE_CHALLENGE_VISIBLE_TEXT_MARKERS:
            title = f"{_mixed_case_marker(marker)} | eBay"
            for mode, body_template in self.BODY_MODES:
                body = body_template
                if mode == "partial":
                    body = f"prefix {_mixed_case_marker(marker)} suffix"
                assert body is not None
                self._assert_security_hard_stop(
                    expected_class=EBAY_CHALLENGE_PAGE,
                    title=title,
                    url=SOLD_URL,
                    body=body,
                    mode=mode,
                    marker=marker,
                )

    def test_active_url_markers_matrix(self) -> None:
        from cardscanr_market_engine.providers.marketplace_browser_page_state import (
            ACTIVE_CHALLENGE_URL_MARKERS,
        )

        for marker in ACTIVE_CHALLENGE_URL_MARKERS:
            if marker.startswith("/"):
                url = f"https://www.ebay.com.au{marker}capture?LH_Sold=1"
            else:
                url = f"https://{marker}sch?LH_Sold=1"
            title = TITLE_OK
            for mode, body_template in self.BODY_MODES:
                body = body_template if body_template is not None else f"partial {marker}"
                if mode == "partial":
                    body = f"noise {_mixed_case_marker(marker)} tail"
                self._assert_security_hard_stop(
                    expected_class=EBAY_CHALLENGE_PAGE,
                    title=title,
                    url=url,
                    body=body,
                    mode=mode,
                    marker=marker,
                )

    def test_access_block_and_sorry_markers_matrix(self) -> None:
        from cardscanr_market_engine.providers.marketplace_browser_page_state import (
            ACCESS_BLOCK_TEXT_MARKERS,
            SORRY_ERROR_TEXT_MARKERS,
        )

        for marker in ACCESS_BLOCK_TEXT_MARKERS:
            title = f"{_mixed_case_marker(marker)} | eBay"
            for mode, body_template in self.BODY_MODES:
                body = body_template
                if mode == "partial":
                    body = f"lead {_mixed_case_marker(marker)} trail"
                assert body is not None
                self._assert_security_hard_stop(
                    expected_class=EBAY_CHALLENGE_PAGE,
                    title=title,
                    url=SOLD_URL,
                    body=body,
                    mode=mode,
                    marker=marker,
                )

        for marker in SORRY_ERROR_TEXT_MARKERS:
            if "\n" in marker:
                title = " ".join(_mixed_case_marker(part) for part in marker.splitlines())
            else:
                title = f"{_mixed_case_marker(marker)} | eBay"
            for mode, body_template in self.BODY_MODES:
                body = body_template
                if mode == "partial":
                    body = f"{_mixed_case_marker(' '.join(marker.split()))} extra"
                assert body is not None
                self._assert_security_hard_stop(
                    expected_class=EBAY_SORRY_PAGE,
                    title=title,
                    url=SOLD_URL,
                    body=body,
                    mode=mode,
                    marker=marker,
                )


class ContinuousSafetyLatchTests(unittest.TestCase):
    def test_recovery_failure_still_increments_transient_latch(self) -> None:
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        reason = None
        for _ in range(3):
            reason = budget.record_transient()
        self.assertEqual(reason, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")

    def test_challenge_still_hard_stop_outcome(self) -> None:
        self.assertIn(CHALLENGE_REQUIRED, HARD_STOP_OUTCOMES)


class InterCardRecoveryAfterCaptureFailureTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_context_from_environ()

    def test_capture_failure_invalidates_inter_card_for_next_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx_path = Path(tmp) / "nav_runtime_context.json"
            prior = PriorCardContext(
                job_id="prior-1",
                target_id="BROKEN_TAB",
                final_url=SOLD_URL,
                query="Dragonite",
                x11_sold_state_verified=True,
                capture_correlated=True,
                card_verdict="PASS_PRICE_UPDATED",
                market="AU",
                currency="AUD",
            )
            with mock.patch(
                "cardscanr_market_engine.navigation_runtime_context.DEFAULT_CONTEXT_PATH",
                ctx_path,
            ):
                apply_context_to_environ(
                    NavigationRuntimeContext(
                        runtime_mode=RUNTIME_INTER_CARD,
                        expected_prior=prior,
                    )
                )
                os.environ["CARDSCANR_NAV_CONTEXT_JSON_PATH"] = str(ctx_path)
                maybe_invalidate_inter_card_after_failure(
                    logger=lambda *_args, **_kwargs: None,
                    owned_outcome=POST_SOLD_CAPTURE_FAILURE,
                    result={
                        "postSoldCapturePhase": POST_SOLD_CAPTURE_FAILED,
                        "failureClass": CDP_DOCUMENT_NOT_READY,
                        "incompleteSoldEvidence": True,
                    },
                    provider_diagnostics={
                        "diagnostics": {
                            "failureClass": CDP_DOCUMENT_NOT_READY,
                            "incompleteSoldEvidence": True,
                        }
                    },
                )
                loaded = load_navigation_runtime_context()
                self.assertEqual(loaded.runtime_mode, RUNTIME_COLD_START)
                self.assertIsNone(loaded.expected_prior)

    def test_next_card_search_uses_recovery_not_reuse_same_tab(self) -> None:
        apply_context_to_environ(NavigationRuntimeContext(runtime_mode=RUNTIME_COLD_START))
        captured: dict = {}

        def _fake_wsl(args: list[str], **kwargs: object) -> dict:
            captured["args"] = list(args)
            return {
                "ok": False,
                "error": "SEARCH_INPUT_NOT_CONFIRMED",
                "diagnostics": {"strategy": INTER_CARD_RECOVERY_STRATEGY, "goHome": True},
            }

        with mock.patch(
            "cardscanr_market_engine.providers.linux_x11_ebay_nav._wsl_python",
            side_effect=_fake_wsl,
        ):
            navigate_query_to_sold("Pikachu 58 base Pokemon", reset_homepage=True)
        self.assertIn("--home", captured["args"])
        self.assertIn(RUNTIME_COLD_START, captured["args"])
        self.assertNotIn(RUNTIME_INTER_CARD, captured["args"])
