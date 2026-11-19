"""PR #63 QA r7/r8 follow-up (DATA-63) — locked latch + gate detection guards."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.browser_lifecycle_policy import (
    REASON_CHALLENGE,
    PriorCardContext,
    RUNTIME_INTER_CARD,
    evaluate_runtime_targets,
    is_challenge_url,
)
from cardscanr_market_engine.continuous_safety import (
    ContinuousSafetyBudget,
    HARD_STOP_SOURCE_SET_SWEEP,
    HARD_STOP_SOURCE_WORKER,
)
from cardscanr_market_engine.ebay_scale.set_sweep_safety import handle_set_sweep_marketplace_outcome
from cardscanr_market_engine.inter_card_cdp_recovery import observe_inter_card_no_request_outcome
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.marketplace_gate_url import (
    is_ebay_marketplace_gate_url,
    is_exact_marketplace_gate_page_title,
    is_pardon_interstitial_at_marketplace_url,
    normalized_gate_page_title,
)
from cardscanr_market_engine.models import MarketPriceRefreshJob
from cardscanr_market_engine.navigation_runtime_context import NavigationRuntimeContext, RUNTIME_INTER_CARD as NAV_IC
from cardscanr_market_engine.owned_daily_outcomes import CHALLENGE_REQUIRED
from cardscanr_market_engine.providers.ebay_browser_provider import (
    EbayBrowserProviderConfig,
    EbayBrowserSoldCompsProvider,
    classify_browser_page_state,
    raise_x11_capture_if_page_blocked,
)
from cardscanr_market_engine.providers.errors import ProviderBlockedError
from cardscanr_market_engine.providers.linux_x11_ebay_nav import LinuxNavResult
from cardscanr_market_engine.providers.post_sold_capture_process import CaptureProcessResult
from cardscanr_market_engine.providers.query_builder import ProviderSearchQuery
from tests.test_data63_reqa_r5_20261119 import _challenge_nav_result, _job_runner_patches
from cardscanr_market_engine.providers.sold_page_health import (
    EBAY_CHALLENGE_PAGE,
    classify_marketplace_page_class,
)
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient

_SOLD_URL = "https://www.ebay.com.au/sch/i.html?_nkw=test&LH_Sold=1"


class Data63ReQaRound8Tests(unittest.TestCase):
    def test_set_sweep_captcha_latch_survives_recovery_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            with mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger):
                sweep = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
                sweep.persist(ledger)

                recovered = ContinuousSafetyBudget.from_env(path=ledger)
                recovered.record_operator_recovery()
                recovered.persist(ledger)

                handle_set_sweep_marketplace_outcome(
                    CHALLENGE_REQUIRED,
                    budget=sweep,
                    path=ledger,
                )
                reloaded = ContinuousSafetyBudget.from_env(path=ledger)
            self.assertEqual(reloaded.last_hard_stop, CHALLENGE_REQUIRED)
            self.assertEqual(reloaded.hard_stop_source, HARD_STOP_SOURCE_SET_SWEEP)
            self.assertIsNotNone(reloaded.reserve_live_submission(path=ledger, market="AU"))

    def test_release_refresh_job_decrements_attempt_count(self) -> None:
        client = SupabaseMarketEngineClient.__new__(SupabaseMarketEngineClient)
        client._table_get = mock.Mock(  # type: ignore[method-assign]
            return_value=[{"id": "job-1", "price_key_id": "pk-1", "attempt_count": 3, "status": "running"}]
        )
        client._table_patch = mock.Mock(return_value=[{"id": "job-1", "status": "queued", "attempt_count": 2}])  # type: ignore[method-assign]
        client._rpc = mock.Mock(side_effect=AssertionError("RPC must not be called"))
        client.release_refresh_job(job_id="job-1", reason="guard:test")
        patch_body = client._table_patch.call_args_list[0].args[1]
        self.assertEqual(patch_body.get("attempt_count"), 2)

    def test_run_once_gaming_pause_releases_claimed_leftovers_not_cancel(self) -> None:
        runner = MarketPriceJobRunner(
            client=mock.Mock(),
            provider=mock.Mock(provider_name="other_provider"),
            config=SimpleNamespace(provider_name="other_provider"),
            logger=lambda *_a, **_k: None,
        )
        runner.client.release_refresh_job = mock.Mock()
        runner.client.cancel_job = mock.Mock()
        jobs = [
            MarketPriceRefreshJob(
                id=f"j{i}",
                price_key_id=f"pk{i}",
                reason="stale",
                priority=1,
                status="running",
                attempt_count=1,
            )
            for i in range(3)
        ]
        runner.claim_jobs = mock.Mock(return_value=jobs)  # type: ignore[method-assign]
        runner.run_job = mock.Mock(return_value={"jobId": "j0", "status": "completed", "ownedDailyOutcome": "UNCHANGED_FROM_EBAY"})  # type: ignore[method-assign]
        gaming = mock.Mock()
        gaming.should_block_new_jobs = mock.Mock(side_effect=[False, True])
        gaming.mark_job_started = mock.Mock()
        gaming.mark_job_finished = mock.Mock()
        with (
            mock.patch(
                "cardscanr_market_engine.job_runner.GamingResourcePauseController",
                return_value=gaming,
            ),
            mock.patch("cardscanr_market_engine.job_runner.time.sleep"),
        ):
            results = runner.run_once(max_jobs=3)
        self.assertEqual(len(results), 1)
        released = [c.args[0] if c.args else c.kwargs.get("job_id") for c in runner.client.release_refresh_job.call_args_list]
        self.assertIn("j1", released)
        self.assertIn("j2", released)
        runner.client.cancel_job.assert_not_called()

    def test_auth_gate_paths_require_whole_segments(self) -> None:
        self.assertTrue(is_ebay_marketplace_gate_url("https://www.ebay.com.au/signin/"))
        self.assertTrue(is_ebay_marketplace_gate_url("https://www.ebay.com.au/signin"))
        self.assertFalse(is_ebay_marketplace_gate_url("https://www.ebay.com.au/itm/signing-event-promo/123"))
        self.assertFalse(is_ebay_marketplace_gate_url("https://www.ebay.com.au/b/Login-Card-Sleeves/123"))

    def test_normalized_title_prefix_and_nbsp(self) -> None:
        self.assertEqual(normalized_gate_page_title("eBay | Security Measure"), "security measure")
        self.assertEqual(normalized_gate_page_title("eBay\u00a0|\u00a0Security\u00a0Measure"), "security measure")

    def test_sold_page_health_exact_gate_title_pardon(self) -> None:
        page_class = classify_marketplace_page_class(url=_SOLD_URL, title="eBay | Security Measure", body="")
        self.assertEqual(page_class, EBAY_CHALLENGE_PAGE)

    def test_x11_capture_site_raises_provider_blocked_for_gate_titles(self) -> None:
        for title in (
            "Access Denied",
            "Security Measure",
            "Pardon Our Interruption",
            "eBay | Security Measure",
        ):
            with self.subTest(title=title):
                state = classify_browser_page_state(
                    title=title,
                    body_text="",
                    url=_SOLD_URL,
                    x11_sold_state_verified=True,
                )
                self.assertNotEqual(state.get("outcome"), "provider_unavailable", title)
                with self.assertRaises(ProviderBlockedError, msg=title):
                    raise_x11_capture_if_page_blocked(state, nav_mode_label="x11_test")

    def test_pardon_not_detected_from_results_body_fragment(self) -> None:
        body = "Sold listings for Pikachu\nPardon our interruption in shipping times\nResults"
        self.assertFalse(
            is_pardon_interstitial_at_marketplace_url(_SOLD_URL, title="Pikachu sold", body=body)
        )
        policy = evaluate_runtime_targets(
            [{"id": "T1", "type": "page", "url": _SOLD_URL, "title": "Pikachu sold"}],
            mode=RUNTIME_INTER_CARD,
            prior=None,
        )
        self.assertNotIn(REASON_CHALLENGE, policy.reason_codes)

    def test_pardon_interstitial_title_blocks_at_sch_url(self) -> None:
        policy = evaluate_runtime_targets(
            [{"id": "T1", "type": "page", "url": _SOLD_URL, "title": "Pardon Our Interruption"}],
            mode=RUNTIME_INTER_CARD,
            prior=None,
        )
        self.assertIn(REASON_CHALLENGE, policy.reason_codes)
        state = classify_browser_page_state(
            title="Pardon Our Interruption",
            body_text="Pardon Our Interruption",
            url=_SOLD_URL,
        )
        self.assertEqual(state.get("outcome"), "challenge_detected")

    def test_exact_gate_titles_latch_not_card_name_substrings(self) -> None:
        prior = PriorCardContext(
            job_id="j1",
            query="Voltorb",
            x11_sold_state_verified=True,
            card_verdict="PASS_PRICE_UPDATED",
            capture_correlated=True,
            final_url=_SOLD_URL,
        )
        for exact_title, expect_challenge in (
            ("Access Denied", True),
            ("Security Measure", True),
            ("Security Measure | eBay", True),
            ("Access Denied Promo | eBay", False),
            ("Security Measures", False),
            ("Pikachu League Challenge promo 403 sold listings", False),
            ("League Challenge Deck NM", False),
            ("Charizard 403/185", False),
        ):
            policy = evaluate_runtime_targets(
                [{"id": "T1", "type": "page", "url": _SOLD_URL, "title": exact_title}],
                mode=RUNTIME_INTER_CARD,
                prior=prior,
                current_query="Pikachu",
            )
            if expect_challenge:
                self.assertIn(REASON_CHALLENGE, policy.reason_codes, msg=exact_title)
            else:
                self.assertNotIn(REASON_CHALLENGE, policy.reason_codes, msg=exact_title)

    def test_inter_card_observe_exact_security_measure_title_latches(self) -> None:
        ctx = NavigationRuntimeContext(runtime_mode=NAV_IC, expected_prior=None)
        _, latch = observe_inter_card_no_request_outcome(
            ctx,
            runtime_error="INTER_CARD_EXPECTED_TARGET_REJECTED",
            search_submission_started=False,
            page_url=_SOLD_URL,
            page_title="Security Measure",
        )
        self.assertEqual(latch, CHALLENGE_REQUIRED)

    def test_classify_security_measure_in_listing_title_not_challenge(self) -> None:
        state = classify_browser_page_state(
            title="Pokemon League Challenge promo 403 sold listings",
            body_text="Sold listings for League Challenge",
            url=_SOLD_URL,
            selector_counts={"li.s-item": 5},
        )
        self.assertNotEqual(state.get("outcome"), "challenge_detected")

    def test_www_ebay_au_signin_is_gate_url(self) -> None:
        self.assertTrue(is_challenge_url("https://www.ebay.com.au/signin/", "Sign in"))

    def test_pardon_title_trailing_ellipsis_is_gate(self) -> None:
        self.assertTrue(is_exact_marketplace_gate_page_title("Pardon Our Interruption…"))
        self.assertTrue(is_exact_marketplace_gate_page_title("Pardon Our Interruption..."))

    def test_blank_title_pardon_body_on_sch_url(self) -> None:
        self.assertTrue(
            is_pardon_interstitial_at_marketplace_url(
                _SOLD_URL,
                title="",
                body="Pardon Our Interruption",
            )
        )
        state = classify_browser_page_state(
            title="",
            body_text="Pardon Our Interruption",
            url=_SOLD_URL,
            x11_sold_state_verified=True,
        )
        self.assertEqual(state.get("outcome"), "challenge_detected")

    def test_linux_x11_fetch_post_capture_calls_raise_x11_on_gate_title(self) -> None:
        def _sold_nav_ok() -> LinuxNavResult:
            return LinuxNavResult(
                ok=True,
                query="Charizard",
                search_success=True,
                sold_click_success=True,
                sold_state_verified=True,
                url=_SOLD_URL,
                title="Charizard sold",
                sorry=False,
                challenge=False,
                sold_date_lines=3,
                error="",
                diagnostics={
                    "search": {
                        "searchSubmissionStarted": True,
                        "ok": True,
                        "submitted": True,
                    }
                },
            )

        proc = CaptureProcessResult(
            status="SUCCESS",
            payload={
                "body_text": "Security measure page",
                "html": "<html><body>Security measure page</body></html>",
                "target_url": _SOLD_URL,
                "target_title": "Security Measure",
                "canonical_itm_href_count": 2,
                "candidates": [{"itemId": "123", "title": "x"}],
            },
        )
        provider = EbayBrowserSoldCompsProvider(config=EbayBrowserProviderConfig.from_env())
        request = mock.Mock(
            price_key=mock.Mock(
                id="pk",
                market_country="AU",
                currency="AUD",
                fingerprint="fp",
                card_name="Charizard",
                set_name="Base",
                set_code="BS",
                collector_number="4",
                language="en",
            ),
            provider_marketplace_id="EBAY_AU",
        )
        search_query = ProviderSearchQuery(
            query_text="Charizard",
            include_terms=(),
            exclude_terms=(),
            provider_domain="ebay.com.au",
            provider_marketplace_id="EBAY_AU",
            search_url="",
            currency="AUD",
            market_country="AU",
        )
        with (
            mock.patch.object(provider, "_linux_x11_nav_mode_enabled", return_value=True),
            mock.patch.object(provider, "_ensure_linux_chrome_cdp_ready", return_value=9444),
            mock.patch(
                "cardscanr_market_engine.providers.linux_x11_ebay_nav.navigate_query_to_sold",
                return_value=_sold_nav_ok(),
            ),
            mock.patch(
                "cardscanr_market_engine.providers.ebay_browser_provider.load_navigation_runtime_context",
                return_value=mock.Mock(is_inter_card=lambda: False),
            ),
            mock.patch(
                "cardscanr_market_engine.providers.ebay_browser_provider.pre_submit_only_requested",
                return_value=False,
            ),
            mock.patch(
                "cardscanr_market_engine.providers.post_sold_capture_process.run_capture_worker_process",
                return_value=proc,
            ),
            mock.patch(
                "cardscanr_market_engine.providers.post_sold_capture.persist_sold_capture_artifact",
                return_value={"ok": True, "htmlPath": "/tmp/fake.html"},
            ),
            mock.patch(
                "cardscanr_market_engine.providers.post_sold_capture.resolve_authoritative_capture_html_path",
                return_value=Path("/tmp/fake_capture.html"),
            ),
            mock.patch(
                "cardscanr_market_engine.finalize_deadline.run_with_finalize_deadline",
                side_effect=lambda fn, *_a, **_kw: fn(),
            ),
        ):
            with self.assertRaises(ProviderBlockedError):
                provider._fetch_with_desktop_win32(request=request, search_query=search_query)

    def test_run_job_challenge_uses_latch_hard_stop_locked_not_unlocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            budget.latch_hard_stop_locked = mock.Mock(wraps=budget.latch_hard_stop_locked)  # type: ignore[method-assign]
            budget.latch_hard_stop = mock.Mock(wraps=budget.latch_hard_stop)  # type: ignore[method-assign]
            runner = MarketPriceJobRunner(
                client=mock.Mock(),
                provider=mock.Mock(provider_name="ebay_browser"),
                config=SimpleNamespace(provider_name="ebay_browser"),
                logger=lambda *_a, **_k: None,
            )
            runner._safety_budget = budget
            runner._continuous_worker = True
            runner._enforce_safety_budget = True
            exc = ProviderBlockedError(
                "Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED",
                diagnostics={
                    "desktopNav": {"search": _challenge_nav_result().diagnostics["search"]},
                },
            )
            job = MarketPriceRefreshJob(
                id="job1",
                price_key_id="pk1",
                reason="force owned_daily stale",
                priority=1,
                status="running",
                attempt_count=1,
            )
            with _job_runner_patches(runner, ledger):
                with mock.patch.object(runner, "fetch_fallback_result", side_effect=exc):
                    runner.run_job(job)
            budget.latch_hard_stop_locked.assert_called_once_with(  # type: ignore[attr-defined]
                CHALLENGE_REQUIRED,
                hard_stop_source=HARD_STOP_SOURCE_WORKER,
            )
            budget.latch_hard_stop.assert_not_called()  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()