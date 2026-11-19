"""PR #63 QA r6 follow-up (DATA-63)."""
from __future__ import annotations

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
)
from cardscanr_market_engine.continuous_safety import (
    HARD_STOP_SOURCE_WORKER,
    ContinuousSafetyBudget,
)
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.models import MarketPriceRefreshJob
from cardscanr_market_engine.owned_daily_outcomes import CHALLENGE_REQUIRED
class Data63ReQaRound7Tests(unittest.TestCase):
    def test_latch_survives_recovery_overlap_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            with mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger):
                recovered = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
                recovered.latch_hard_stop(CHALLENGE_REQUIRED, hard_stop_source=HARD_STOP_SOURCE_WORKER)
                recovered.latch_epoch = 0
                recovered.record_operator_recovery()
                recovered.persist(ledger)

                stale_worker = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
                stale_worker.latch_epoch = 0
                stale_worker.latch_hard_stop_locked(
                    CHALLENGE_REQUIRED,
                    path=ledger,
                    hard_stop_source=HARD_STOP_SOURCE_WORKER,
                )
                reloaded = ContinuousSafetyBudget.from_env(path=ledger)
            self.assertEqual(reloaded.last_hard_stop, CHALLENGE_REQUIRED)
            self.assertGreater(reloaded.latch_epoch, recovered.latch_epoch)

    def test_promo_sold_tab_not_blocking_through_target_policy(self) -> None:
        prior = PriorCardContext(
            job_id="j1",
            query="Voltorb",
            x11_sold_state_verified=True,
            card_verdict="PASS_PRICE_UPDATED",
            capture_correlated=True,
            final_url="https://www.ebay.com.au/sch/i.html?_nkw=voltorb&LH_Sold=1",
        )
        promo_url = "https://www.ebay.com.au/sch/i.html?_nkw=pokemon+league+challenge+promo&LH_Sold=1"
        policy = evaluate_runtime_targets(
            [
                {
                    "id": "TAB-1",
                    "type": "page",
                    "url": promo_url,
                    "title": "Voltorb 403/SV-P promo | eBay",
                }
            ],
            mode=RUNTIME_INTER_CARD,
            prior=prior,
            current_query="Pikachu",
        )
        self.assertNotIn(REASON_CHALLENGE, policy.reason_codes)
        self.assertFalse(policy.ok is False and REASON_CHALLENGE in policy.reason_codes)

    def test_release_refresh_job_no_rpc_and_resets_cache(self) -> None:
        from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient

        client = SupabaseMarketEngineClient.__new__(SupabaseMarketEngineClient)
        client._table_get = mock.Mock(  # type: ignore[method-assign]
            return_value=[
                {
                    "id": "job-1",
                    "price_key_id": "pk-1",
                    "attempt_count": 2,
                    "status": "running",
                }
            ]
        )
        client._table_patch = mock.Mock(  # type: ignore[method-assign]
            return_value=[{"id": "job-1", "status": "queued", "attempt_count": 1}]
        )
        client._rpc = mock.Mock(side_effect=AssertionError("RPC must not be called"))
        row = client.release_refresh_job(job_id="job-1", reason="budget_blocked:test")
        self.assertEqual(row["status"], "queued")
        self.assertEqual(client._table_patch.call_count, 2)
        cache_patch = client._table_patch.call_args_list[1].args[1]
        self.assertEqual(cache_patch.get("refresh_status"), "queued")

    def test_run_once_challenge_releases_remaining_jobs(self) -> None:
        runner = MarketPriceJobRunner(
            client=mock.Mock(),
            provider=mock.Mock(provider_name="ebay_browser"),
            config=mock.Mock(provider_name="ebay_browser"),
            logger=lambda *_a, **_k: None,
        )
        runner.client.release_refresh_job = mock.Mock()
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
        runner.run_job = mock.Mock(  # type: ignore[method-assign]
            side_effect=[
                {
                    "jobId": "j0",
                    "status": "failed",
                    "ownedDailyOutcome": CHALLENGE_REQUIRED,
                    "interCardMarketplaceLatch": CHALLENGE_REQUIRED,
                },
            ]
        )
        pacing = mock.Mock(
            state=mock.Mock(browser_halted=False),
            record_check_duration=mock.Mock(),
            observe_outcome=mock.Mock(),
            next_delay_seconds=mock.Mock(return_value=0),
        )
        with (
            mock.patch(
                "cardscanr_market_engine.job_runner.GamingResourcePauseController",
                return_value=mock.Mock(should_block_new_jobs=mock.Mock(return_value=False)),
            ),
            mock.patch(
                "cardscanr_market_engine.job_runner.OwnedDailyPacingController",
                return_value=pacing,
            ),
            mock.patch("cardscanr_market_engine.job_runner.time.sleep"),
        ):
            results = runner.run_once(max_jobs=3)
        self.assertEqual(len(results), 1)
        released = [c.kwargs.get("job_id") for c in runner.client.release_refresh_job.call_args_list]
        self.assertIn("j1", released)
        self.assertIn("j2", released)
        runner.client.cancel_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()