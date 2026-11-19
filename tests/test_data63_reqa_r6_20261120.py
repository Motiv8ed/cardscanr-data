"""PR #63 QA r5 follow-up (DATA-63)."""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import workers.market_price_worker as worker
from cardscanr_market_engine.continuous_safety import (
    ContinuousSafetyBudget,
    HARD_STOP_SOURCE_WORKER,
    merge_budget_payloads,
)
from cardscanr_market_engine.owned_daily_outcomes import CHALLENGE_REQUIRED
from cardscanr_market_engine.inter_card_cdp_recovery import observe_inter_card_no_request_outcome
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.local_browser_runtime import cold_start_close_leftover_ebay_tabs
from cardscanr_market_engine.marketplace_gate_url import is_ebay_marketplace_gate_url
from cardscanr_market_engine.models import MarketPriceRefreshJob
from cardscanr_market_engine.navigation_runtime_context import NavigationRuntimeContext, RUNTIME_INTER_CARD
from tools import market_recovery_us_gb_ca as recovery


class Data63ReQaRound6Tests(unittest.TestCase):
    def test_merge_success_resets_consecutive_after_peer_had_failures(self) -> None:
        now = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
        disk = ContinuousSafetyBudget(max_consecutive_transient=3)
        for _ in range(2):
            disk.record_transient(now=now)
        disk_payload = disk.to_payload()
        local = ContinuousSafetyBudget(max_consecutive_transient=3)
        local.record_healthy()
        merged = merge_budget_payloads(disk_payload, local.to_payload())
        self.assertEqual(merged.get("consecutiveTransient"), 0)
        self.assertEqual(merged.get("transients") or [], [])

    def test_alternating_fail_success_does_not_hard_stop_until_three_since_reset(self) -> None:
        now = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        for i, healthy in enumerate([False, False, True, False, False, False]):
            if healthy:
                budget.record_healthy()
            else:
                budget.record_transient(now + timedelta(minutes=i))
        self.assertEqual(budget.consecutive_transient, 3)
        self.assertEqual(budget.last_hard_stop, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")

    def test_operator_recovery_epoch_blocks_stale_latch_merge(self) -> None:
        disk = ContinuousSafetyBudget()
        disk.latch_hard_stop(CHALLENGE_REQUIRED, hard_stop_source=HARD_STOP_SOURCE_WORKER)
        disk_payload = disk.to_payload()
        local = ContinuousSafetyBudget()
        local.record_operator_recovery()
        merged = merge_budget_payloads(disk_payload, local.to_payload())
        self.assertIsNone(merged.get("lastHardStop"))

    def test_recover_au_clears_ledger_and_unblocks_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            budget.latch_hard_stop(CHALLENGE_REQUIRED, hard_stop_source=HARD_STOP_SOURCE_WORKER)
            budget.persist(ledger)
            snap = SimpleNamespace(state="HEALTHY", to_dict=lambda: {"state": "HEALTHY"})
            with (
                mock.patch("tools.market_recovery_us_gb_ca.ART", Path(tmp) / "art"),
                mock.patch("tools.market_recovery_us_gb_ca.wait_for_gaming_clear", return_value={"blocked": False}),
                mock.patch("tools.market_recovery_us_gb_ca.peek_availability", return_value=snap),
                mock.patch("tools.market_recovery_us_gb_ca.write_owned_daily_flag"),
                mock.patch("tools.market_recovery_us_gb_ca.apply_continuous_multi_region_env"),
                mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger),
            ):
                result = recovery.recover_au()
            self.assertTrue(result.get("ok"))
            reloaded = ContinuousSafetyBudget.from_env(path=ledger)
            self.assertIsNone(reloaded.last_hard_stop)
            self.assertIsNone(reloaded.reserve_live_submission(path=ledger, market="AU"))

    def test_recover_au_then_worker_cycle_records_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            budget.latch_hard_stop(CHALLENGE_REQUIRED, hard_stop_source=HARD_STOP_SOURCE_WORKER)
            budget.persist(ledger)
            snap = SimpleNamespace(state="HEALTHY", to_dict=lambda: {"state": "HEALTHY"})
            with (
                mock.patch("tools.market_recovery_us_gb_ca.ART", root / "art"),
                mock.patch("tools.market_recovery_us_gb_ca.wait_for_gaming_clear", return_value={"blocked": False}),
                mock.patch("tools.market_recovery_us_gb_ca.peek_availability", return_value=snap),
                mock.patch("tools.market_recovery_us_gb_ca.write_owned_daily_flag"),
                mock.patch("tools.market_recovery_us_gb_ca.apply_continuous_multi_region_env"),
                mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger),
            ):
                recovery.recover_au()
            runner = mock.Mock()
            runner._ebay_probe_mode = False
            runner.run_once = mock.Mock(
                return_value=[
                    {
                        "jobId": "j1",
                        "status": "completed",
                        "ownedDailyOutcome": "UNCHANGED_FROM_EBAY",
                        "searchSubmissionStarted": True,
                        "marketCountry": "AU",
                    }
                ]
            )
            config = SimpleNamespace(
                worker_id="worker-test",
                provider_name="ebay_browser",
                latest_report_path=root / "latest.json",
                runs_report_path=root / "runs.jsonl",
            )
            with (
                mock.patch.object(worker.ContinuousSafetyBudget, "from_env", return_value=ContinuousSafetyBudget.from_env(path=ledger)),
                mock.patch.object(worker, "owned_daily_full_enable", return_value=True),
                mock.patch.object(worker, "write_json"),
                mock.patch.object(worker, "append_jsonl"),
                mock.patch.object(worker, "append_daily_metrics"),
                mock.patch.object(worker, "write_owned_daily_flag"),
                mock.patch.object(worker, "write_continuous_status"),
                mock.patch.object(
                    worker,
                    "classify_continuous_gate",
                    return_value={
                        "workerState": "SELECTING",
                        "hardStop": None,
                        "availabilityState": "HEALTHY",
                    },
                ),
                mock.patch(
                    "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.try_set_sweep_worker_cycle",
                    return_value=None,
                ),
                mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger),
            ):
                worker.run_worker_loop(
                    args=SimpleNamespace(once=True, max_cycles=0),
                    config=config,  # type: ignore[arg-type]
                    runner=runner,
                    poll_seconds=1,
                    max_jobs=1,
                    sleep_func=lambda _s: None,
                    logger=lambda *_a, **_k: None,
                )
            reloaded = ContinuousSafetyBudget.from_env(path=ledger)
            self.assertGreaterEqual(reloaded.submissions_1h(), 1)

    def test_gate_url_matches_captcha_and_sorry_html(self) -> None:
        self.assertTrue(
            is_ebay_marketplace_gate_url(
                "https://www.ebay.com.au/splashui/captcha?ap=1&ru=https%3A%2F%2Fwww.ebay.com.au"
            )
        )
        self.assertTrue(is_ebay_marketplace_gate_url("https://www.ebay.com.au/sorry.html"))

    def test_cold_start_keeps_splashui_captcha_tab(self) -> None:
        captcha_url = "https://www.ebay.com.au/splashui/captcha?ap=1"

        def _probe_side_effect(*_a, **_k):
            return mock.Mock(
                raw_targets=[
                    {"id": "BLANK", "type": "page", "url": "about:blank", "title": ""},
                    {"id": "CAP", "type": "page", "url": captcha_url, "title": "Security Measure"},
                ],
                ebay_targets=[],
                cdp_ready=True,
            )

        resp = mock.MagicMock()
        resp.read.return_value = b'{"id":"BLANK"}'
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with (
            mock.patch(
                "cardscanr_market_engine.local_browser_runtime.probe_local_browser_runtime",
                side_effect=_probe_side_effect,
            ),
            mock.patch(
                "cardscanr_market_engine.local_browser_runtime.urllib.request.urlopen",
                return_value=resp,
            ),
        ):
            result = cold_start_close_leftover_ebay_tabs(cdp_port=9444)
        self.assertNotIn("CAP", result.get("closedTargetIds") or [])

    def test_promo_title_and_query_do_not_latch_challenge(self) -> None:
        ctx = NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=None)
        sold_url = "https://www.ebay.com.au/sch/i.html?_nkw=pokemon+league+challenge+promo&LH_Sold=1"
        _, latch = observe_inter_card_no_request_outcome(
            ctx,
            runtime_error="INTER_CARD_EXPECTED_TARGET_REJECTED",
            search_submission_started=False,
            raw_error="Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED",
            page_url=sold_url,
            page_title="Pikachu League Challenge promo 403 sold listings",
        )
        self.assertIsNone(latch)

    def test_run_job_budget_block_releases_job_to_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            runner = MarketPriceJobRunner(
                client=mock.Mock(),
                provider=mock.Mock(provider_name="ebay_browser"),
                config=SimpleNamespace(provider_name="ebay_browser"),
                logger=lambda *_a, **_k: None,
            )
            runner._safety_budget = budget
            runner._continuous_worker = True
            runner._enforce_safety_budget = True
            runner.client.release_refresh_job = mock.Mock(return_value={"status": "queued"})
            price_key = mock.Mock(
                id="pk1",
                fingerprint="fp1",
                market_country="AU",
                currency="AUD",
                language="en",
                condition="NM",
                variant="",
                raw={},
            )
            runner.client.get_price_key.return_value = price_key
            runner.client.get_cache_row = mock.Mock(return_value=None)
            job = MarketPriceRefreshJob(
                id="job-cap",
                price_key_id="pk1",
                reason="stale",
                priority=1,
                status="running",
                attempt_count=1,
            )
            with (
                mock.patch.object(runner, "_assert_market_allowed_for_worker", return_value=None),
                mock.patch(
                    "cardscanr_market_engine.ebay_scale.cross_user_cache.evaluate_cross_user_cache_for_job",
                    return_value=None,
                ),
                mock.patch(
                    "cardscanr_market_engine.job_runner.maybe_apply_set_sweep_live_skip",
                    return_value=None,
                ),
                mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger),
            ):
                for _ in range(20):
                    budget.reserve_live_submission(path=ledger, market="AU")
                with mock.patch.object(runner, "fetch_fallback_result") as fetch:
                    result = runner.run_job(job)
                fetch.assert_not_called()
            runner.client.release_refresh_job.assert_called_once()
            runner.client.cancel_job.assert_not_called()
            self.assertEqual(result.get("status"), "deferred")


if __name__ == "__main__":
    unittest.main()