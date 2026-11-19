"""PR #63 round-5 re-QA: provider→runner behaviours through real entrypoints (DATA-63)."""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.continuous_safety import (
    HARD_STOP_SOURCE_WORKER,
    SAFETY_LEDGER_FAIL_CLOSED,
    ContinuousSafetyBudget,
    merge_budget_payloads,
)
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.live_ebay_component_health import classify_live_ebay_worker_component_health
from cardscanr_market_engine.marketplace_gate_url import is_ebay_marketplace_gate_url
from cardscanr_market_engine.models import MarketPriceRefreshJob
from cardscanr_market_engine.owned_daily_outcomes import CHALLENGE_REQUIRED
from cardscanr_market_engine.providers.ebay_browser_provider import (
    EbayBrowserProviderConfig,
    EbayBrowserSoldCompsProvider,
)
from cardscanr_market_engine.providers.errors import ProviderBlockedError
from cardscanr_market_engine.providers.linux_x11_ebay_nav import LinuxNavResult
from cardscanr_market_engine.providers.query_builder import ProviderSearchQuery
from cardscanr_market_engine.runtime_write_guard import assert_safe_runtime_json_write
import workers.market_price_worker as worker


def _challenge_nav_result() -> LinuxNavResult:
    url = "https://www.ebay.com.au/splashui/challenge?ap=1&referrer=https%3A%2F%2Fwww.ebay.com.au"
    return LinuxNavResult(
        ok=False,
        query="Charizard",
        search_success=False,
        sold_click_success=False,
        sold_state_verified=False,
        url=url,
        title="Verify yourself to continue",
        sorry=False,
        challenge=False,
        sold_date_lines=0,
        error="INTER_CARD_EXPECTED_TARGET_REJECTED",
        diagnostics={
            "search": {
                "resultCode": "INTER_CARD_CHALLENGE_TARGET",
                "urlBeforeSearch": url,
                "titleBeforeSearch": "Verify yourself to continue",
                "targetPolicy": {"reason_codes": ["INTER_CARD_CHALLENGE_TARGET"]},
            }
        },
    )


@contextlib.contextmanager
def _job_runner_patches(runner: MarketPriceJobRunner, ledger: Path):
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
    runner.client.fail_job = mock.Mock()
    runner.client.count_recent_same_failures = mock.Mock(return_value=0)
    runner.client.cancel_job = mock.Mock()
    with (
        mock.patch.object(runner, "_assert_market_allowed_for_worker", return_value=None),
        mock.patch(
            "cardscanr_market_engine.ebay_scale.cross_user_cache.evaluate_cross_user_cache_for_job",
            return_value=None,
        ),
        mock.patch(
            "cardscanr_market_engine.job_runner.load_navigation_runtime_context",
            return_value=mock.Mock(runtime_mode="INTER_CARD", expected_prior=None, to_dict=lambda: {}),
        ),
        mock.patch(
            "cardscanr_market_engine.job_runner.reconcile_navigation_context_for_browser_recycle",
            side_effect=lambda ctx, **_k: ctx,
        ),
        mock.patch(
            "cardscanr_market_engine.inter_card_cdp_recovery.reconcile_nav_context_for_cdp_change",
            side_effect=lambda ctx: ctx,
        ),
        mock.patch(
            "cardscanr_market_engine.job_runner.prepare_context_for_market",
            return_value=None,
        ),
        mock.patch(
            "cardscanr_market_engine.job_runner.apply_context_to_environ",
            return_value=None,
        ),
        mock.patch(
            "cardscanr_market_engine.job_runner.maybe_apply_set_sweep_live_skip",
            return_value=None,
        ),
        mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger),
    ):
        yield price_key


class Data63ReQaRound5Tests(unittest.TestCase):
    def test_provider_nav_failure_carries_challenge_result_code(self) -> None:
        provider = EbayBrowserSoldCompsProvider(config=EbayBrowserProviderConfig.from_env())
        request = mock.Mock(
            price_key=mock.Mock(id="pk", market_country="AU", currency="AUD"),
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
        nav = _challenge_nav_result()
        with (
            mock.patch.object(provider, "_linux_x11_nav_mode_enabled", return_value=True),
            mock.patch.object(provider, "_ensure_linux_chrome_cdp_ready", return_value=9444),
            mock.patch(
                "cardscanr_market_engine.providers.linux_x11_ebay_nav.navigate_query_to_sold",
                return_value=nav,
            ),
            mock.patch(
                "cardscanr_market_engine.providers.ebay_browser_provider.load_navigation_runtime_context",
                return_value=mock.Mock(is_inter_card=lambda: True),
            ),
            mock.patch(
                "cardscanr_market_engine.providers.ebay_browser_provider.pre_submit_only_requested",
                return_value=False,
            ),
        ):
            with self.assertRaises(ProviderBlockedError) as caught:
                provider._fetch_with_desktop_win32(request=request, search_query=search_query)
        diag = caught.exception.diagnostics or {}
        self.assertEqual(diag.get("resultCode"), "INTER_CARD_CHALLENGE_TARGET")
        self.assertIn("splashui/challenge", str(diag.get("urlBeforeSearch") or ""))

    def test_run_job_provider_challenge_latches_budget_without_hand_fed_top_level_code(self) -> None:
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
                    result = runner.run_job(job)
            self.assertEqual(result.get("interCardMarketplaceLatch"), CHALLENGE_REQUIRED)
            self.assertEqual(budget.last_hard_stop, CHALLENGE_REQUIRED)
            self.assertEqual(budget.hard_stop_source, HARD_STOP_SOURCE_WORKER)

    def test_run_job_hourly_cap_blocks_fetch_and_cancels_job(self) -> None:
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
            job = MarketPriceRefreshJob(
                id="job-cap",
                price_key_id="pk1",
                reason="stale",
                priority=1,
                status="running",
                attempt_count=1,
            )
            with _job_runner_patches(runner, ledger):
                with mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger):
                    for _ in range(20):
                        self.assertIsNone(budget.reserve_live_submission(path=ledger, market="AU"))
                fetch = mock.patch.object(runner, "fetch_fallback_result")
                with fetch as mocked_fetch:
                    result = runner.run_job(job)
                mocked_fetch.assert_not_called()
            release = getattr(runner.client, "release_refresh_job", None)
            if release is not None:
                release.assert_called()
            else:
                runner.client.cancel_job.assert_called()
            self.assertTrue(result.get("budgetBlocked"))

    def test_run_job_respects_latched_hard_stop_without_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            budget.latch_hard_stop(CHALLENGE_REQUIRED, hard_stop_source=HARD_STOP_SOURCE_WORKER)
            budget.persist(ledger)
            runner = MarketPriceJobRunner(
                client=mock.Mock(),
                provider=mock.Mock(provider_name="ebay_browser"),
                config=SimpleNamespace(provider_name="ebay_browser"),
                logger=lambda *_a, **_k: None,
            )
            runner._safety_budget = budget
            runner._continuous_worker = True
            runner._enforce_safety_budget = True
            job = MarketPriceRefreshJob(
                id="job-latch",
                price_key_id="pk1",
                reason="stale",
                priority=1,
                status="running",
                attempt_count=1,
            )
            with _job_runner_patches(runner, ledger):
                with mock.patch.object(runner, "fetch_fallback_result") as mocked_fetch:
                    result = runner.run_job(job)
                mocked_fetch.assert_not_called()
            self.assertTrue(result.get("budgetBlocked"))
            if getattr(runner.client, "release_refresh_job", None) is not None:
                runner.client.release_refresh_job.assert_called()
            else:
                runner.client.cancel_job.assert_called()

    def test_run_job_missing_ledger_fail_closed_via_run_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            budget.persist(ledger)
            ledger.unlink()
            runner = MarketPriceJobRunner(
                client=mock.Mock(),
                provider=mock.Mock(provider_name="ebay_browser"),
                config=SimpleNamespace(provider_name="ebay_browser"),
                logger=lambda *_a, **_k: None,
            )
            runner._safety_budget = budget
            runner._continuous_worker = True
            runner._enforce_safety_budget = True
            job = MarketPriceRefreshJob(
                id="job-missing-ledger",
                price_key_id="pk1",
                reason="stale",
                priority=1,
                status="running",
                attempt_count=1,
            )
            with _job_runner_patches(runner, ledger):
                with mock.patch.object(runner, "fetch_fallback_result") as mocked_fetch:
                    result = runner.run_job(job)
                mocked_fetch.assert_not_called()
            self.assertEqual(result.get("ownedDailyOutcome"), SAFETY_LEDGER_FAIL_CLOSED)

    def test_run_once_challenge_halts_batch_and_releases_claimed_jobs(self) -> None:
        runner = MarketPriceJobRunner(
            client=mock.Mock(),
            provider=mock.Mock(provider_name="ebay_browser"),
            config=SimpleNamespace(provider_name="ebay_browser"),
            logger=lambda *_a, **_k: None,
        )
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
                {"jobId": "j1", "status": "completed"},
                {"jobId": "j2", "status": "completed"},
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
        cancel_calls = [c.kwargs.get("job_id") or c.args[0] for c in runner.client.cancel_job.call_args_list]
        self.assertIn("j1", cancel_calls)
        self.assertIn("j2", cancel_calls)

    def test_record_healthy_does_not_clear_worker_challenge_latch(self) -> None:
        budget = ContinuousSafetyBudget()
        budget.latch_hard_stop(CHALLENGE_REQUIRED, hard_stop_source=HARD_STOP_SOURCE_WORKER)
        budget.record_healthy()
        self.assertEqual(budget.last_hard_stop, CHALLENGE_REQUIRED)

    def test_worker_loop_records_submission_when_reserve_not_on_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
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
                mock.patch.object(worker.ContinuousSafetyBudget, "from_env", return_value=budget),
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
                with mock.patch.dict(
                    os.environ,
                    {"MARKET_WORKER_ENFORCE_SAFETY_BUDGET": "true"},
                    clear=False,
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
            budget.reload(ledger)
            self.assertEqual(budget.submissions_1h(), 1)

    def test_worker_loop_idle_cycle_persists_ledger_for_health_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            persist_spy = mock.Mock(wraps=budget.persist)
            budget.persist = persist_spy  # type: ignore[method-assign]
            runner = mock.Mock()
            runner._ebay_probe_mode = False
            runner.run_once = mock.Mock(return_value=[])
            config = SimpleNamespace(
                worker_id="worker-test",
                provider_name="ebay_browser",
                latest_report_path=root / "latest.json",
                runs_report_path=root / "runs.jsonl",
            )
            with (
                mock.patch.object(worker.ContinuousSafetyBudget, "from_env", return_value=budget),
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
            self.assertGreaterEqual(persist_spy.call_count, 1)
            row = classify_live_ebay_worker_component_health(
                process_present=True,
                process_id=1234,
                status_path=root / "status.json",
                ledger_path=ledger,
            )
            self.assertEqual(row.ledger_evidence, "fresh")

    def test_gate_url_ignores_sold_query_substrings(self) -> None:
        soldish = "https://www.ebay.com.au/sch/i.html?_nkw=charizard+authentic&LH_Sold=1"
        promo = "https://www.ebay.com.au/sch/i.html?_nkw=pokemon+league+challenge+promo"
        self.assertFalse(is_ebay_marketplace_gate_url(soldish))
        self.assertFalse(is_ebay_marketplace_gate_url(promo))
        self.assertTrue(
            is_ebay_marketplace_gate_url("https://www.ebay.com.au/splashui/challenge?ap=1")
        )

    def test_runtime_write_guard_through_pytest_env(self) -> None:
        target = ROOT / "reports" / "runtime" / "guard_probe_r5.json"
        with mock.patch.dict(os.environ, {"CARDSCANR_PYTEST": "1"}, clear=False):
            os.environ.pop("CARDSCANR_TEST_RUNTIME_ROOT", None)
            with self.assertRaises(RuntimeError):
                assert_safe_runtime_json_write(target)

    def test_merge_sticky_latch_not_cleared_by_stale_process(self) -> None:
        disk = ContinuousSafetyBudget()
        disk.latch_hard_stop("MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES", hard_stop_source="worker")
        local = ContinuousSafetyBudget()
        local.record_healthy()
        merged = merge_budget_payloads(disk.to_payload(), local.to_payload())
        self.assertEqual(
            merged.get("lastHardStop"),
            "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
        )


if __name__ == "__main__":
    unittest.main()