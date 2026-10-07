"""Deterministic regressions for continuous transient safety accounting.

Covers the proven AU HARD_STOP defects:
- leftover eBay tabs on COLD_START must use cold_start cleanup before budget burn
- consecutiveTransient must stay consistent with retained transient history
- HARD_STOP latches via lastHardStop (owner-clear only)
- attempts while already stopped must not manufacture equivalent history
- challenge/CAPTCHA hard-stop path remains fail-closed
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.continuous_worker_policy import (
    HARD_STOP_OUTCOMES,
    maybe_resume_transient_halt,
)
from cardscanr_market_engine.navigation_runtime_context import (
    NavigationRuntimeContext,
    apply_context_to_environ,
    clear_context_from_environ,
)
from cardscanr_market_engine.owned_daily_outcomes import CHALLENGE_REQUIRED
from cardscanr_market_engine.owned_daily_pacing import OwnedDailyPacingController
from cardscanr_market_engine.browser_lifecycle_policy import RUNTIME_COLD_START
from cardscanr_market_engine.providers.linux_x11_ebay_nav import ensure_chrome_with_cdp


class TransientLedgerConsistencyTests(unittest.TestCase):
    def test_three_genuine_consecutive_transients_latch_hard_stop(self) -> None:
        now = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        self.assertIsNone(budget.record_transient(now))
        self.assertIsNone(budget.record_transient(now + timedelta(minutes=1)))
        reason = budget.record_transient(now + timedelta(minutes=2))
        self.assertEqual(reason, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")
        self.assertEqual(budget.consecutive_transient, 3)
        self.assertEqual(len(budget.transients), 3)
        self.assertEqual(budget.last_hard_stop, reason)
        self.assertEqual(budget.transient_hard_stop(now + timedelta(minutes=2)), reason)

    def test_prune_reconciles_consecutive_without_clearing_latched_hard_stop(self) -> None:
        now = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        for i in range(3):
            budget.record_transient(now + timedelta(minutes=i))
        self.assertEqual(
            budget.transient_hard_stop(now + timedelta(minutes=2)),
            "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
        )
        # One hour later: transient timestamps prune away, but HARD_STOP stays latched.
        later = now + timedelta(hours=1, minutes=5)
        stop = budget.transient_hard_stop(later)
        self.assertEqual(stop, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")
        self.assertEqual(len(budget.transients), 0)
        self.assertEqual(budget.consecutive_transient, 0)
        self.assertEqual(budget.last_hard_stop, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")

    def test_stale_consecutive_without_history_is_reconciled_on_load(self) -> None:
        """Proven bug shape: consecutiveTransient=3 with empty transients[]."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            path.write_text(
                '{"submissions":[],"transients":[],"consecutiveTransient":3,'
                '"lastHardStop":"MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",'
                '"marketSubmissions":[]}\n',
                encoding="utf-8",
            )
            budget = ContinuousSafetyBudget(
                max_consecutive_transient=3,
                max_transient_per_hour=5,
            )
            budget.load(path)
            self.assertEqual(budget.consecutive_transient, 0)
            self.assertEqual(len(budget.transients), 0)
            self.assertEqual(
                budget.transient_hard_stop(),
                "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
            )

    def test_healthy_reset_is_owner_clear(self) -> None:
        now = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        for i in range(3):
            budget.record_transient(now + timedelta(minutes=i))
        self.assertIsNotNone(budget.transient_hard_stop(now + timedelta(minutes=2)))
        budget.record_healthy()
        self.assertEqual(budget.consecutive_transient, 0)
        self.assertIsNone(budget.last_hard_stop)
        self.assertIsNone(budget.transient_hard_stop(now + timedelta(minutes=3)))

    def test_attempts_while_already_stopped_do_not_add_history(self) -> None:
        now = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        for i in range(3):
            budget.record_transient(now + timedelta(minutes=i))
        self.assertEqual(len(budget.transients), 3)
        self.assertEqual(budget.consecutive_transient, 3)
        # Further identical pre-submit failures must not grow the ledger.
        for i in range(5):
            reason = budget.record_transient(now + timedelta(minutes=10 + i))
            self.assertEqual(reason, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")
        self.assertEqual(len(budget.transients), 3)
        self.assertEqual(budget.consecutive_transient, 3)

    def test_persist_never_writes_contradictory_consecutive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
            budget.consecutive_transient = 3
            budget.transients = []
            budget.last_hard_stop = "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES"
            budget.persist(path)
            reloaded = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
            reloaded.load(path)
            self.assertEqual(reloaded.consecutive_transient, 0)
            self.assertEqual(len(reloaded.transients), 0)
            self.assertEqual(
                reloaded.last_hard_stop,
                "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
            )


class ColdStartLeftoverTabTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_context_from_environ()

    def test_leftover_ebay_tabs_use_cold_start_cleanup_then_succeed(self) -> None:
        apply_context_to_environ(NavigationRuntimeContext(runtime_mode=RUNTIME_COLD_START))
        first = mock.Mock(returncode=4, stdout="CDP_HAS_EBAY_TARGET\n", stderr="")
        second = mock.Mock(returncode=0, stdout="CDP_OK\n", stderr="")
        cleanup = mock.Mock(
            return_value={"ok": True, "closedTargetIds": ["t1"], "ebayTargetsAfter": []}
        )
        with mock.patch(
            "cardscanr_market_engine.providers.linux_x11_ebay_nav.subprocess.run",
            side_effect=[first, second],
        ) as run_mock:
            with mock.patch.object(Path, "write_bytes", return_value=None):
                with mock.patch(
                    "cardscanr_market_engine.local_browser_runtime.cold_start_close_leftover_ebay_tabs",
                    cleanup,
                ):
                    ensure_chrome_with_cdp(cdp_port=9444)
        cleanup.assert_called_once_with(cdp_port=9444)
        self.assertEqual(run_mock.call_count, 2)

    def test_leftover_tabs_cleanup_failure_still_raises(self) -> None:
        apply_context_to_environ(NavigationRuntimeContext(runtime_mode=RUNTIME_COLD_START))
        proc = mock.Mock(returncode=4, stdout="CDP_HAS_EBAY_TARGET\n", stderr="")
        with mock.patch(
            "cardscanr_market_engine.providers.linux_x11_ebay_nav.subprocess.run",
            return_value=proc,
        ):
            with mock.patch.object(Path, "write_bytes", return_value=None):
                with mock.patch(
                    "cardscanr_market_engine.local_browser_runtime.cold_start_close_leftover_ebay_tabs",
                    return_value={"ok": False, "ebayTargetsAfter": ["https://www.ebay.com.au/"]},
                ):
                    with self.assertRaises(RuntimeError) as ctx:
                        ensure_chrome_with_cdp(cdp_port=9444)
        self.assertIn("CDP_HAS_EBAY_TARGET", str(ctx.exception))


class ChallengeHardStopSafetyTests(unittest.TestCase):
    def test_challenge_remains_hard_stop_safe_no_auto_resume(self) -> None:
        self.assertIn(CHALLENGE_REQUIRED, HARD_STOP_OUTCOMES)
        ctl = OwnedDailyPacingController()
        ctl.observe_outcome(CHALLENGE_REQUIRED)
        self.assertTrue(ctl.state.browser_halted)
        cleared = maybe_resume_transient_halt(
            ctl,
            {"workerState": "SELECTING", "hardStop": None},
        )
        self.assertFalse(cleared)
        self.assertTrue(ctl.state.browser_halted)

    def test_transient_hard_stop_does_not_auto_clear_without_owner_healthy(self) -> None:
        now = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        for i in range(3):
            budget.record_transient(now + timedelta(minutes=i))
        # Prune + time advance is not owner-clear.
        later = now + timedelta(hours=2)
        self.assertIsNotNone(budget.transient_hard_stop(later))
        self.assertIsNotNone(budget.last_hard_stop)


if __name__ == "__main__":
    unittest.main()
