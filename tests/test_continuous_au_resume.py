"""Offline continuous AU resume, safety budget, and failed-card backoff."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.failure_policy import build_failure_policy
from cardscanr_market_engine.owned_daily_enablement import (
    apply_continuous_au_env,
    owned_daily_full_enable,
    write_owned_daily_flag,
)
from cardscanr_market_engine.owned_daily_outcomes import TEMPORARY_EBAY_SERVER_FAILURE
from cardscanr_market_engine.owned_daily_pacing import OwnedDailyPacingController
from cardscanr_market_engine.providers.errors import ProviderTemporaryError
from cardscanr_market_engine.continuous_worker_policy import maybe_resume_transient_halt


class ContinuousSafetyTests(unittest.TestCase):
    def test_hourly_ceiling_is_not_a_target(self) -> None:
        now = datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=200)
        for i in range(20):
            budget.record_submission(now + timedelta(minutes=i))
        self.assertEqual(budget.budget_exhausted(now + timedelta(minutes=19)), "HOURLY_SUBMISSION_CEILING")

    def test_consecutive_transient_hard_stop(self) -> None:
        now = datetime(2026, 10, 4, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        self.assertIsNone(budget.record_transient(now))
        self.assertIsNone(budget.record_transient(now + timedelta(minutes=1)))
        reason = budget.record_transient(now + timedelta(minutes=2))
        self.assertEqual(reason, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")


class FailedCardBackoffTests(unittest.TestCase):
    def test_first_sorry_backoff_outlasts_typical_marketplace_hour(self) -> None:
        exc = ProviderTemporaryError(
            "TEMPORARY_EBAY_SERVER_FAILURE: eBay SORRY/error page during desktop navigation",
            diagnostics={"reason": "ebay_sorry_error_page"},
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EBAY_TRANSIENT_FAILURE_COOLDOWN_MINUTES", None)
            os.environ.pop("EBAY_TRANSIENT_FAILURE_SECOND_COOLDOWN_MINUTES", None)
            policy = build_failure_policy(exc, consecutive_same_failures=1)
        self.assertGreaterEqual(policy.backoff, timedelta(minutes=90))
        self.assertTrue(policy.retryable)


class TransientHaltResumeTests(unittest.TestCase):
    def test_transient_halt_clears_when_healthy(self) -> None:
        ctl = OwnedDailyPacingController()
        ctl.observe_outcome(TEMPORARY_EBAY_SERVER_FAILURE)
        self.assertTrue(ctl.state.browser_halted)
        cleared = maybe_resume_transient_halt(
            ctl,
            {"workerState": "SELECTING", "hardStop": None},
        )
        self.assertTrue(cleared)
        self.assertFalse(ctl.state.browser_halted)

    def test_challenge_does_not_auto_resume(self) -> None:
        ctl = OwnedDailyPacingController()
        ctl.observe_outcome("CHALLENGE_REQUIRED")
        self.assertTrue(ctl.state.browser_halted)
        cleared = maybe_resume_transient_halt(
            ctl,
            {"workerState": "SELECTING", "hardStop": None},
        )
        self.assertFalse(cleared)
        self.assertTrue(ctl.state.browser_halted)


class EnablementFlagTests(unittest.TestCase):
    def test_flag_file_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "owned_daily_full_enable.flag"
            write_owned_daily_flag(True, path=flag)
            with mock.patch.dict(os.environ, {"OWNED_DAILY_FULL_ENABLE": "false"}):
                self.assertTrue(owned_daily_full_enable(flag_path=flag))
            write_owned_daily_flag(False, path=flag)
            with mock.patch.dict(os.environ, {"OWNED_DAILY_FULL_ENABLE": "true"}):
                self.assertFalse(owned_daily_full_enable(flag_path=flag))

    def test_apply_continuous_au_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            apply_continuous_au_env()
            self.assertEqual(os.environ["OWNED_DAILY_ALLOWED_MARKETS"], "AU")
            self.assertEqual(os.environ["OWNED_DAILY_FULL_MAX_ENQUEUE"], "1")
            self.assertEqual(os.environ["MARKET_WORKER_CONCURRENCY"], "1")
            self.assertEqual(os.environ["EBAY_BROWSER_NAV_MODE"], "linux_x11")


if __name__ == "__main__":
    unittest.main()
