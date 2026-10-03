from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.owned_daily_outcomes import (
    CHALLENGE_REQUIRED,
    TEMPORARY_BROWSER_FAILURE,
    UPDATED_FROM_EBAY,
)
from cardscanr_market_engine.owned_daily_pacing import (
    OwnedDailyPacingConfig,
    OwnedDailyPacingController,
)


class OwnedDailyPacingTests(unittest.TestCase):
    def test_success_uses_min_delay(self) -> None:
        cfg = OwnedDailyPacingConfig(
            min_inter_job_delay_seconds=90,
            max_inter_job_delay_seconds=360,
            failure_base_delay_seconds=180,
            session_rest_every_n_checks=99,
            session_rest_seconds=180,
        )
        ctl = OwnedDailyPacingController(cfg)
        ctl.observe_outcome(UPDATED_FROM_EBAY)
        self.assertEqual(ctl.next_delay_seconds(), 90)

    def test_failure_backoff_increases_bounded(self) -> None:
        cfg = OwnedDailyPacingConfig(
            min_inter_job_delay_seconds=90,
            max_inter_job_delay_seconds=360,
            failure_base_delay_seconds=180,
            session_rest_every_n_checks=99,
            session_rest_seconds=0,
        )
        ctl = OwnedDailyPacingController(cfg)
        ctl.observe_outcome(TEMPORARY_BROWSER_FAILURE)
        self.assertEqual(ctl.next_delay_seconds(), 180)
        ctl.observe_outcome(TEMPORARY_BROWSER_FAILURE)
        self.assertEqual(ctl.next_delay_seconds(), 270)
        ctl.observe_outcome(TEMPORARY_BROWSER_FAILURE)
        self.assertEqual(ctl.next_delay_seconds(), 360)
        ctl.observe_outcome(TEMPORARY_BROWSER_FAILURE)
        self.assertEqual(ctl.next_delay_seconds(), 360)

    def test_challenge_halts_browser(self) -> None:
        ctl = OwnedDailyPacingController(
            OwnedDailyPacingConfig(session_rest_every_n_checks=99, session_rest_seconds=0)
        )
        ctl.observe_outcome(CHALLENGE_REQUIRED)
        self.assertTrue(ctl.state.browser_halted)
        self.assertEqual(ctl.next_delay_seconds(more_jobs_pending=True), 0)

    def test_session_rest_extends_delay(self) -> None:
        cfg = OwnedDailyPacingConfig(
            min_inter_job_delay_seconds=90,
            max_inter_job_delay_seconds=360,
            failure_base_delay_seconds=180,
            session_rest_every_n_checks=2,
            session_rest_seconds=180,
        )
        ctl = OwnedDailyPacingController(cfg)
        ctl.observe_outcome(UPDATED_FROM_EBAY)
        self.assertEqual(ctl.next_delay_seconds(), 90)
        ctl.observe_outcome(UPDATED_FROM_EBAY)
        # After 2 checks → rest: max(90, 180+45) = 225
        self.assertEqual(ctl.next_delay_seconds(), 225)

    def test_from_env_respects_min_max(self) -> None:
        os.environ["OWNED_DAILY_MIN_INTER_JOB_DELAY_SECONDS"] = "45"
        os.environ["OWNED_DAILY_MAX_INTER_JOB_DELAY_SECONDS"] = "180"
        try:
            cfg = OwnedDailyPacingConfig.from_env()
            self.assertEqual(cfg.min_inter_job_delay_seconds, 45)
            self.assertEqual(cfg.max_inter_job_delay_seconds, 180)
        finally:
            os.environ.pop("OWNED_DAILY_MIN_INTER_JOB_DELAY_SECONDS", None)
            os.environ.pop("OWNED_DAILY_MAX_INTER_JOB_DELAY_SECONDS", None)

    def test_capacity_projection_sufficient_for_current_workload(self) -> None:
        ctl = OwnedDailyPacingController(
            OwnedDailyPacingConfig(
                min_inter_job_delay_seconds=90,
                operating_window_hours=16,
                session_rest_every_n_checks=99,
            )
        )
        # Simulate paced averages: ~150s check + 90s cool
        for _ in range(5):
            ctl.record_check_duration(150.0)
            ctl.observe_outcome(UPDATED_FROM_EBAY)
            ctl.next_delay_seconds()
        cap = ctl.capacity_projection(expected_daily_workload=142)
        self.assertGreater(cap["safe_daily_capacity"], 142)
        self.assertEqual(cap["capacity_status"], "SUFFICIENT")

    def test_noop_does_not_extend_failure_streak(self) -> None:
        cfg = OwnedDailyPacingConfig(
            min_inter_job_delay_seconds=90,
            max_inter_job_delay_seconds=360,
            failure_base_delay_seconds=180,
            session_rest_every_n_checks=99,
            session_rest_seconds=0,
        )
        ctl = OwnedDailyPacingController(cfg)
        ctl.observe_outcome(TEMPORARY_BROWSER_FAILURE)
        ctl.observe_outcome("already_fresh_noop")
        self.assertEqual(ctl.state.consecutive_browser_failures, 1)
        # Prior failure still open → keep failure backoff (do not shorten for noop).
        self.assertEqual(ctl.next_delay_seconds(), 180)
        ctl2 = OwnedDailyPacingController(cfg)
        ctl2.observe_outcome(UPDATED_FROM_EBAY)
        ctl2.observe_outcome("already_fresh_noop")
        self.assertEqual(ctl2.next_delay_seconds(), 15)


if __name__ == "__main__":
    unittest.main()
