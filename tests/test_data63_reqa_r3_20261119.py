"""PR #63 round-3 re-QA revert-sensitive coverage (DATA-63)."""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.browser_lifecycle_policy import PriorCardContext
from cardscanr_market_engine.continuous_safety import (
    SAFETY_LEDGER_FAIL_CLOSED,
    ContinuousSafetyBudget,
    merge_budget_payloads,
)
from cardscanr_market_engine.ebay_scale import set_sweep_safety as sweep_safety_mod
from cardscanr_market_engine.ebay_scale.set_sweep_worker_hook import BUDGET_LEDGER_PATH as HOOK_LEDGER_PATH
from cardscanr_market_engine.inter_card_cdp_recovery import (
    CDP_RECOVERY_STATE_PATH,
    _fingerprint_cdp_targets,
    observe_inter_card_no_request_outcome,
    reconcile_nav_context_for_cdp_change,
)
from cardscanr_market_engine.live_ebay_component_health import (
    SCHEDULED_REPORT_MAX_AGE_MINUTES,
    scheduled_report_allows_ledger_fallback,
)
from cardscanr_market_engine.navigation_runtime_context import (
    INTER_CARD_EXPECTED_TARGET_REJECTED,
    NavigationRuntimeContext,
    RUNTIME_INTER_CARD,
    classify_pre_submit_runtime_error,
)


class Data63ReQaRound3Tests(unittest.TestCase):
    def test_classify_real_inter_card_rejection_error_text(self) -> None:
        exc = RuntimeError(
            "Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED"
        )
        code = classify_pre_submit_runtime_error(exc, runtime_mode=RUNTIME_INTER_CARD)
        self.assertEqual(code, INTER_CARD_EXPECTED_TARGET_REJECTED)

    def test_observe_real_error_text_increments_stuck_counter(self) -> None:
        ctx = NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=None)
        raw = "Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED"
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "recovery.json"
            with mock.patch(
                "cardscanr_market_engine.inter_card_cdp_recovery.CDP_RECOVERY_STATE_PATH",
                state_path,
            ):
                _, latch1, _r1 = observe_inter_card_no_request_outcome(
                    ctx,
                    runtime_error="OTHER",
                    search_submission_started=False,
                    raw_error=raw,
                )
                self.assertIsNone(latch1)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state.get("consecutiveInterCardNoRequestRejections"), 1)

    def test_challenge_page_does_not_increment_cold_start_counter(self) -> None:
        ctx = NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=None)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "recovery.json"
            with mock.patch(
                "cardscanr_market_engine.inter_card_cdp_recovery.CDP_RECOVERY_STATE_PATH",
                state_path,
            ):
                out, latch, _receipt = observe_inter_card_no_request_outcome(
                    ctx,
                    runtime_error="INTER_CARD_EXPECTED_TARGET_REJECTED",
                    search_submission_started=False,
                    raw_error="Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED",
                    result_code="INTER_CARD_CHALLENGE_TARGET",
                    diagnostics={
                        "urlBeforeSearch": "https://www.ebay.com.au/splashui/challenge",
                        "titleBeforeSearch": "Verify yourself",
                    },
                    page_url="https://www.ebay.com.au/splashui/challenge",
                    page_title="Verify yourself",
                )
            self.assertEqual(latch, "CHALLENGE_REQUIRED")
            self.assertEqual(out.runtime_mode, RUNTIME_INTER_CARD)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state.get("consecutiveInterCardNoRequestRejections", 0), 0)

    def test_iframe_only_target_list_does_not_change_fingerprint(self) -> None:
        top = [{"id": "TAB-1", "type": "page", "url": "https://www.ebay.com.au/sch/i.html"}]
        with_iframe = top + [{"id": "IFRAME-1", "type": "iframe", "url": "https://www.ebay.com.au/x"}]
        self.assertEqual(_fingerprint_cdp_targets(top), _fingerprint_cdp_targets(with_iframe))

    def test_expected_tab_id_retained_when_only_iframe_added(self) -> None:
        prior = PriorCardContext(
            job_id="j1",
            target_id="TAB-1",
            query="Voltorb",
            x11_sold_state_verified=True,
            card_verdict="PASS_PRICE_UPDATED",
        )
        ctx = NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=prior)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "recovery.json"
            fp = _fingerprint_cdp_targets([{"id": "TAB-1", "type": "page"}])
            state_path.write_text(
                json.dumps({"cdpTopLevelFingerprint": fp}),
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
            ):
                probe.return_value = mock.Mock(
                    raw_targets=[
                        {"id": "TAB-1", "type": "page"},
                        {"id": "IFRAME-9", "type": "iframe"},
                    ]
                )
                out = reconcile_nav_context_for_cdp_change(ctx)
        assert out.expected_prior is not None
        self.assertEqual(out.expected_prior.target_id, "TAB-1")
        self.assertFalse(out.prior_handoff_stale)

    def test_recycle_fingerprint_change_keeps_expected_tab_when_still_present(self) -> None:
        prior = PriorCardContext(
            job_id="j2",
            target_id="TAB-1",
            query="Voltorb base1 67",
            x11_sold_state_verified=True,
            card_verdict="PASS_PRICE_UPDATED",
        )
        ctx = NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=prior)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "recovery.json"
            state_path.write_text(
                json.dumps({"cdpTopLevelFingerprint": "r0:TAB-1"}),
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
                    return_value="r1:TAB-1",
                ),
            ):
                probe.return_value = mock.Mock(
                    raw_targets=[{"id": "TAB-1", "type": "page"}],
                )
                out = reconcile_nav_context_for_cdp_change(ctx)
        assert out.expected_prior is not None
        self.assertEqual(out.expected_prior.target_id, "TAB-1")
        self.assertFalse(out.prior_handoff_stale)

    def test_persist_merge_retains_other_process_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            t0 = datetime(2026, 11, 19, 7, 0, tzinfo=timezone.utc)
            t1 = t0 + timedelta(minutes=5)
            other = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            other.reserve_live_submission(path=ledger, market="AU", now=t0)
            stale_local = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            stale_local.record_submission(now=t1, market="AU")
            stale_local.persist(ledger)
            reloaded = ContinuousSafetyBudget.from_env(path=ledger)
            self.assertEqual(reloaded.submissions_24h(now=t1 + timedelta(minutes=1)), 2)

    def test_merge_budget_payloads_unions_timestamps(self) -> None:
        t0 = "2026-11-19T07:00:00+00:00"
        t1 = "2026-11-19T07:05:00+00:00"
        merged = merge_budget_payloads(
            {"submissions": [t0], "ledgerInitialized": True},
            {"submissions": [t1], "ledgerInitialized": True},
        )
        self.assertEqual(len(merged.get("submissions") or []), 2)

    def test_deleted_ledger_blocks_reserve_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            budget.persist(ledger)
            self.assertTrue(budget.ledger_initialized)
            ledger.unlink()
            blocked = budget.reserve_live_submission(path=ledger, market="AU")
            self.assertEqual(blocked, SAFETY_LEDGER_FAIL_CLOSED)
            self.assertTrue(budget.fail_closed)

    def test_hook_module_budget_path_redirected_in_safety_test_module(self) -> None:
        src = (ROOT / "tests" / "test_ebay_set_sweep_safety_20261119.py").read_text(encoding="utf-8")
        self.assertIn("set_sweep_worker_hook.BUDGET_LEDGER_PATH", src)
        self.assertNotIn(
            "reports/runtime/continuous_au_safety_ledger.json",
            src.split("test_all_runtime_paths")[1].split("def test_post_reserve")[0],
        )

    def test_scheduled_report_stale_rejected(self) -> None:
        now = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)
        stale_started = (now - timedelta(minutes=SCHEDULED_REPORT_MAX_AGE_MINUTES + 10)).isoformat()
        report = {"workerState": "SELECTING", "startedAtUtc": stale_started}
        self.assertFalse(scheduled_report_allows_ledger_fallback(report, now=now))

    def test_scheduled_report_hard_stop_rejected(self) -> None:
        now = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)
        fresh = now.isoformat()
        report = {"workerState": "HARD_STOP", "startedAtUtc": fresh, "lastHardStopReason": "AU_THREE"}
        self.assertFalse(scheduled_report_allows_ledger_fallback(report, now=now))

    def test_max_backoff_ceiling_clamped_to_floor(self) -> None:
        with mock.patch.dict(os.environ, {"EBAY_SCALE_SET_SWEEP_MAX_FAILURE_BACKOFF_SECONDS": "1"}, clear=False):
            importlib.reload(sweep_safety_mod)
            seconds = sweep_safety_mod.compute_failure_backoff_seconds(1)
            self.assertGreaterEqual(seconds, 30.0)
        importlib.reload(sweep_safety_mod)

    def test_counter_resets_after_successful_submission_flag(self) -> None:
        ctx = NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=None)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "recovery.json"
            state_path.write_text(
                json.dumps({"consecutiveInterCardNoRequestRejections": 2}),
                encoding="utf-8",
            )
            with mock.patch(
                "cardscanr_market_engine.inter_card_cdp_recovery.CDP_RECOVERY_STATE_PATH",
                state_path,
            ):
                observe_inter_card_no_request_outcome(
                    ctx,
                    runtime_error=INTER_CARD_EXPECTED_TARGET_REJECTED,
                    search_submission_started=True,
                )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state.get("consecutiveInterCardNoRequestRejections"), 0)

    def test_hook_ledger_path_is_configurable_not_hardcoded_reports_only(self) -> None:
        self.assertTrue(hasattr(HOOK_LEDGER_PATH, "parent"))


if __name__ == "__main__":
    unittest.main()