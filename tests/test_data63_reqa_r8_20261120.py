"""PR #63 QA r7 follow-up (DATA-63) — locked latch + gate detection guards."""
from __future__ import annotations

import inspect
import sys
import tempfile
import unittest
from pathlib import Path
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
from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget, HARD_STOP_SOURCE_SET_SWEEP
from cardscanr_market_engine.ebay_scale.set_sweep_safety import handle_set_sweep_marketplace_outcome
from cardscanr_market_engine.inter_card_cdp_recovery import observe_inter_card_no_request_outcome
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.marketplace_gate_url import is_ebay_marketplace_gate_url
from cardscanr_market_engine.models import MarketPriceRefreshJob
from cardscanr_market_engine.navigation_runtime_context import NavigationRuntimeContext, RUNTIME_INTER_CARD as NAV_IC
from cardscanr_market_engine.owned_daily_outcomes import CHALLENGE_REQUIRED
from cardscanr_market_engine.providers.ebay_browser_provider import classify_browser_page_state
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


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

    def test_handle_set_sweep_marketplace_outcome_implementation_uses_locked_latch(self) -> None:
        source = inspect.getsource(handle_set_sweep_marketplace_outcome)
        self.assertIn("latch_hard_stop_locked", source)
        self.assertNotIn("latch_hard_stop(", source)

    def test_job_runner_challenge_path_uses_locked_latch(self) -> None:
        from cardscanr_market_engine import job_runner

        source = inspect.getsource(job_runner.MarketPriceJobRunner.run_job)
        self.assertIn("latch_hard_stop_locked", source)
        self.assertNotIn("latch_hard_stop(", source)

    def test_release_refresh_job_resets_attempt_count_guard(self) -> None:
        client = SupabaseMarketEngineClient.__new__(SupabaseMarketEngineClient)
        client._table_get = mock.Mock(  # type: ignore[method-assign]
            return_value=[{"id": "job-1", "price_key_id": "pk-1", "attempt_count": 3, "status": "running"}]
        )
        client._table_patch = mock.Mock(return_value=[{"id": "job-1", "status": "queued", "attempt_count": 2}])  # type: ignore[method-assign]
        client._rpc = mock.Mock(side_effect=AssertionError("RPC must not be called"))
        client.release_refresh_job(job_id="job-1", reason="guard:test")
        patch_body = client._table_patch.call_args_list[0].args[1]
        self.assertEqual(patch_body.get("attempt_count"), 2)

    def test_gaming_pause_leftovers_are_released_not_cancelled(self) -> None:
        runner = MarketPriceJobRunner(
            client=mock.Mock(),
            provider=mock.Mock(provider_name="ebay_browser"),
            config=mock.Mock(provider_name="ebay_browser"),
            logger=lambda *_a, **_k: None,
        )
        runner.client.release_refresh_job = mock.Mock()
        runner.client.cancel_job = mock.Mock()
        source = inspect.getsource(runner._release_claimed_job)
        self.assertIn("release_refresh_job", source)
        runner._release_claimed_job("job-gaming", "gaming_pause_batch_halt")
        runner.client.release_refresh_job.assert_called_once()
        runner.client.cancel_job.assert_not_called()

    def test_www_ebay_au_signin_is_gate_url(self) -> None:
        self.assertTrue(is_ebay_marketplace_gate_url("https://www.ebay.com.au/signin/"))
        self.assertTrue(is_challenge_url("https://www.ebay.com.au/signin/", "Sign in"))

    def test_pardon_interstitial_at_sold_search_url(self) -> None:
        sold = "https://www.ebay.com.au/sch/i.html?_nkw=Pikachu&LH_Sold=1"
        policy = evaluate_runtime_targets(
            [{"id": "T1", "type": "page", "url": sold, "title": "Pardon Our Interruption"}],
            mode=RUNTIME_INTER_CARD,
            prior=None,
        )
        self.assertIn(REASON_CHALLENGE, policy.reason_codes)
        state = classify_browser_page_state(
            title="Pardon Our Interruption",
            body_text="Pardon Our Interruption",
            url=sold,
        )
        self.assertEqual(state.get("reason"), "pardon_our_interruption")

    def test_exact_gate_titles_latch_not_card_name_substrings(self) -> None:
        sold = "https://www.ebay.com.au/sch/i.html?_nkw=test&LH_Sold=1"
        for exact_title, expect_challenge in (
            ("Access Denied", True),
            ("Security Measure", True),
            ("Security Measure | eBay", True),
            ("Pikachu League Challenge promo 403 sold listings", False),
            ("League Challenge Deck NM", False),
            ("Charizard 403/185", False),
        ):
            policy = evaluate_runtime_targets(
                [{"id": "T1", "type": "page", "url": sold, "title": exact_title}],
                mode=RUNTIME_INTER_CARD,
                prior=PriorCardContext(
                    job_id="j1",
                    query="Voltorb",
                    x11_sold_state_verified=True,
                    card_verdict="PASS_PRICE_UPDATED",
                    capture_correlated=True,
                    final_url=sold,
                ),
                current_query="Pikachu",
            )
            if expect_challenge:
                self.assertIn(REASON_CHALLENGE, policy.reason_codes, msg=exact_title)
            else:
                self.assertNotIn(REASON_CHALLENGE, policy.reason_codes, msg=exact_title)

    def test_inter_card_observe_exact_security_measure_title_latches(self) -> None:
        ctx = NavigationRuntimeContext(runtime_mode=NAV_IC, expected_prior=None)
        sold = "https://www.ebay.com.au/sch/i.html?_nkw=test&LH_Sold=1"
        _, latch = observe_inter_card_no_request_outcome(
            ctx,
            runtime_error="INTER_CARD_EXPECTED_TARGET_REJECTED",
            search_submission_started=False,
            page_url=sold,
            page_title="Security Measure",
        )
        self.assertEqual(latch, CHALLENGE_REQUIRED)

    def test_classify_security_measure_in_listing_title_not_challenge(self) -> None:
        sold = "https://www.ebay.com.au/sch/i.html?_nkw=league+challenge&LH_Sold=1"
        state = classify_browser_page_state(
            title="Pokemon League Challenge promo 403 sold listings",
            body_text="Sold listings for League Challenge",
            url=sold,
            selector_counts={"li.s-item": 5},
        )
        self.assertNotEqual(state.get("outcome"), "challenge_detected")


if __name__ == "__main__":
    unittest.main()