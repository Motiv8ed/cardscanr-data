"""PR #63 round-4 re-QA: behavioural regressions (DATA-63)."""
from __future__ import annotations

import multiprocessing as mp
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

from cardscanr_market_engine.atomic_json_state import atomic_write_json
from cardscanr_market_engine.browser_lifecycle_policy import RUNTIME_INTER_CARD, PriorCardContext
from cardscanr_market_engine.continuous_safety import (
    SAFETY_LEDGER_FAIL_CLOSED,
    ContinuousSafetyBudget,
    merge_budget_payloads,
)
from cardscanr_market_engine.inter_card_cdp_recovery import (
    observe_inter_card_no_request_outcome,
    reconcile_nav_context_for_cdp_change,
)
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.live_ebay_component_health import classify_live_ebay_worker_component_health
from cardscanr_market_engine.local_browser_runtime import cold_start_close_leftover_ebay_tabs
from cardscanr_market_engine.models import MarketPriceRefreshJob
from cardscanr_market_engine.owned_daily_outcomes import CHALLENGE_REQUIRED
from cardscanr_market_engine.providers.errors import ProviderError
from cardscanr_market_engine.runtime_write_guard import assert_safe_runtime_json_write


def _reserve_batch(ledger: str, count: int) -> None:
    budget = ContinuousSafetyBudget.from_env(path=Path(ledger))
    for _ in range(count):
        budget.reserve_live_submission(path=Path(ledger), market="AU")


class Data63ReQaRound4Tests(unittest.TestCase):
    def test_runtime_write_guard_blocks_repo_runtime_under_pytest(self) -> None:
        target = ROOT / "reports" / "runtime" / "guard_probe.json"
        with mock.patch.dict(os.environ, {"CARDSCANR_PYTEST": "1"}, clear=False):
            os.environ.pop("CARDSCANR_TEST_RUNTIME_ROOT", None)
            with self.assertRaises(RuntimeError):
                assert_safe_runtime_json_write(target)

    def test_per_card_reserve_stops_at_hourly_cap_before_fetch(self) -> None:
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
            allowed = 0
            blocked = 0
            with mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger):
                for _ in range(25):
                    reserved, reason = runner._reserve_per_card_submission("AU")
                    self.assertTrue(reserved)
                    if reason:
                        blocked += 1
                    else:
                        allowed += 1
            self.assertEqual(allowed, 20)
            self.assertEqual(blocked, 5)
            self.assertEqual(budget.submissions_1h(), 20)

    def test_challenge_result_code_latches_not_cold_start(self) -> None:
        ctx = mock.Mock(runtime_mode=RUNTIME_INTER_CARD, expected_prior=None)
        realistic = {
            "resultCode": "INTER_CARD_CHALLENGE_TARGET",
            "targetPolicy": {
                "reason_codes": ["INTER_CARD_CHALLENGE_TARGET"],
            },
            "urlBeforeSearch": "https://www.ebay.com.au/splashui/challenge",
            "titleBeforeSearch": "Verify yourself",
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "recovery.json"
            with mock.patch(
                "cardscanr_market_engine.inter_card_cdp_recovery.CDP_RECOVERY_STATE_PATH",
                state_path,
            ):
                out, latch = observe_inter_card_no_request_outcome(
                    ctx,
                    runtime_error="INTER_CARD_EXPECTED_TARGET_REJECTED",
                    search_submission_started=False,
                    raw_error="Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED",
                    result_code="INTER_CARD_CHALLENGE_TARGET",
                    diagnostics=realistic,
                    page_url=realistic["urlBeforeSearch"],
                    page_title=realistic["titleBeforeSearch"],
                )
            self.assertEqual(latch, CHALLENGE_REQUIRED)
            state = __import__("json").loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state.get("consecutiveInterCardNoRequestRejections", 0), 0)
            self.assertEqual(out.runtime_mode, RUNTIME_INTER_CARD)

    def test_job_runner_challenge_failure_latches_hard_stop(self) -> None:
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
            price_key = mock.Mock(
                id="pk1",
                fingerprint="fp1",
                market_country="AU",
                currency="AUD",
            )
            runner.client.get_price_key.return_value = price_key
            runner.client.fail_job = mock.Mock()
            runner.client.count_recent_same_failures = mock.Mock(return_value=0)
            exc = ProviderError(
                "Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED",
                diagnostics={
                    "resultCode": "INTER_CARD_CHALLENGE_TARGET",
                    "urlBeforeSearch": "https://www.ebay.com.au/splashui/challenge",
                    "titleBeforeSearch": "Challenge",
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
            runner.client.get_cache_row = mock.Mock(return_value=None)
            with (
                mock.patch.object(
                    runner,
                    "fetch_fallback_result",
                    side_effect=exc,
                ),
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
                runner.run_job(job)
            self.assertEqual(budget.last_hard_stop, CHALLENGE_REQUIRED)

    def test_merge_payload_keeps_healthy_reset(self) -> None:
        now = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)
        disk = ContinuousSafetyBudget(max_consecutive_transient=3)
        for _ in range(3):
            disk.record_transient(now=now)
        disk_payload = disk.to_payload()
        local = ContinuousSafetyBudget(max_consecutive_transient=3)
        local.record_healthy()
        merged = merge_budget_payloads(disk_payload, local.to_payload())
        self.assertEqual(merged.get("consecutiveTransient"), 0)
        self.assertIsNone(merged.get("lastHardStop"))

    def test_two_process_reserve_never_exceeds_hourly_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            ctx = mp.get_context("spawn")
            workers = [
                ctx.Process(target=_reserve_batch, args=(str(ledger), 15))
                for _ in range(2)
            ]
            for proc in workers:
                proc.start()
            for proc in workers:
                proc.join(timeout=30)
                self.assertEqual(proc.exitcode, 0)
            reloaded = ContinuousSafetyBudget.from_env(path=ledger)
            self.assertLessEqual(reloaded.submissions_1h(), 20)

    def test_deleted_ledger_reserve_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            with mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger):
                budget.persist(ledger)
            ledger.unlink()
            blocked = budget.reserve_live_submission(path=ledger, market="AU")
            self.assertEqual(blocked, SAFETY_LEDGER_FAIL_CLOSED)

    def test_tab_gone_without_recycle_keeps_expected_target(self) -> None:
        prior = PriorCardContext(
            job_id="j1",
            target_id="TAB-1",
            query="Voltorb",
            x11_sold_state_verified=True,
            card_verdict="PASS_PRICE_UPDATED",
        )
        ctx = __import__(
            "cardscanr_market_engine.navigation_runtime_context",
            fromlist=["NavigationRuntimeContext"],
        ).NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=prior)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "recovery.json"
            state_path.write_text(
                '{"cdpTopLevelFingerprint":"r0:TAB-1"}',
                encoding="utf-8",
            )
            with (
                mock.patch(
                    "cardscanr_market_engine.inter_card_cdp_recovery.CDP_RECOVERY_STATE_PATH",
                    state_path,
                ),
                mock.patch(
                    "cardscanr_market_engine.local_browser_runtime.probe_local_browser_runtime",
                ) as probe,
                mock.patch(
                    "cardscanr_market_engine.inter_card_cdp_recovery._fingerprint_cdp_targets",
                    return_value="r0:TAB-OTHER",
                ),
            ):
                probe.return_value = mock.Mock(
                    raw_targets=[{"id": "TAB-OTHER", "type": "page"}],
                )
                out = reconcile_nav_context_for_cdp_change(ctx)
        assert out.expected_prior is not None
        self.assertEqual(out.expected_prior.target_id, "TAB-1")
        self.assertFalse(out.prior_handoff_stale)

    def test_cold_start_preserves_challenge_tab(self) -> None:
        def _probe_side_effect(*_a, **_k):
            browser = mock.Mock(
                raw_targets=[
                    {
                        "id": "BLANK",
                        "type": "page",
                        "url": "about:blank",
                        "title": "",
                    },
                    {
                        "id": "CH",
                        "type": "page",
                        "url": "https://www.ebay.com.au/splashui/challenge",
                        "title": "Verify",
                    },
                    {
                        "id": "SOLD",
                        "type": "page",
                        "url": "https://www.ebay.com.au/sch/i.html?LH_Sold=1",
                        "title": "Sold",
                    },
                ],
                ebay_targets=[],
                cdp_ready=True,
            )
            return browser

        with mock.patch(
            "cardscanr_market_engine.local_browser_runtime.probe_local_browser_runtime",
            side_effect=_probe_side_effect,
        ):
            resp = mock.MagicMock()
            resp.read.return_value = b'{"id":"BLANK"}'
            resp.__enter__.return_value = resp
            resp.__exit__.return_value = False
            with mock.patch(
                "cardscanr_market_engine.local_browser_runtime.urllib.request.urlopen",
                return_value=resp,
            ):
                result = cold_start_close_leftover_ebay_tabs(cdp_port=9444)
        self.assertNotIn("CH", result.get("closedTargetIds") or [])


if __name__ == "__main__":
    unittest.main()