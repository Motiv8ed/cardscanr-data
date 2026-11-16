"""Acceptance tests for unattended AU eBay recovery probe + navigation hardening."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.ebay_availability import (
    begin_probe,
    browser_work_allowed,
    record_sorry,
    save_availability,
)
from cardscanr_market_engine.owned_daily_outcomes import (
    CHALLENGE_REQUIRED,
    EBAY_RATE_LIMITED,
    TEMPORARY_BROWSER_FAILURE,
    TEMPORARY_EBAY_SERVER_FAILURE,
    POST_SOLD_CAPTURE_FAILURE,
    classify_exception_outcome,
)
from cardscanr_market_engine.providers.errors import ProviderRateLimitedError
from cardscanr_market_engine.providers.post_sold_capture_process import (
    DEFAULT_CAPTURE_DEADLINE_SECONDS,
    MAX_CAPTURE_DEADLINE_SECONDS,
    DEFAULT_POST_SOLD_FINALIZE_SECONDS,
    capture_deadline_seconds,
    post_sold_finalize_deadline_seconds,
)
from cardscanr_market_engine.region_pricing_registry import is_region_dispatchable, region_definition
from cardscanr_market_engine.x11_chrome_focus import _run
import workers.market_price_worker as worker


def _fake_config(root: Path, *, provider: str = "ebay_browser") -> SimpleNamespace:
    return SimpleNamespace(
        worker_id="worker-test",
        provider_name=provider,
        latest_report_path=root / "latest.json",
        runs_report_path=root / "runs.jsonl",
    )


class ProbeAwareRunner:
    def __init__(self, *, outcomes: list[list[dict]] | None = None, client: object | None = None) -> None:
        self.client = client or mock.Mock()
        self.outcomes = list(outcomes or [[{"jobId": "p1", "status": "completed", "ownedDailyOutcome": "UNCHANGED_FROM_EBAY"}]])
        self.calls = 0
        self.last_max_jobs: int | None = None
        self.probe_modes: list[bool] = []

    def run_once(self, *, max_jobs: int | None = None) -> list[dict]:
        self.calls += 1
        self.last_max_jobs = int(max_jobs or 0)
        self.probe_modes.append(bool(getattr(self, "_ebay_probe_mode", False)))
        row = self.outcomes.pop(0)
        return row


@contextlib.contextmanager
def _isolated_worker_loop(*, budget: ContinuousSafetyBudget | None = None):
    injected = budget or ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch.dict("os.environ", {"CARDSCANR_OPERATION_MODE": ""}, clear=False)
        )
        stack.enter_context(mock.patch.object(worker.ContinuousSafetyBudget, "from_env", return_value=injected))
        stack.enter_context(mock.patch.object(worker.ContinuousSafetyBudget, "persist"))
        stack.enter_context(mock.patch.object(worker, "write_owned_daily_flag"))
        stack.enter_context(mock.patch.object(worker, "write_continuous_status"))
        stack.enter_context(mock.patch.object(worker, "append_daily_metrics"))
        stack.enter_context(mock.patch.object(worker, "write_json"))
        stack.enter_context(mock.patch.object(worker, "append_jsonl"))
        yield injected


class ScheduledProbeGateTests(unittest.TestCase):
    def test_owned_daily_false_runs_single_probe_when_probe_required(self) -> None:
        gate = {
            "workerState": "PROBE_REQUIRED",
            "hardStop": None,
            "availabilityState": "PROBE_REQUIRED",
            "probeAllowed": True,
            "cooldownUntil": None,
            "nextProbeAt": "2020-01-01T00:00:00Z",
            "activeChallenges": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _fake_config(root)
            runner = ProbeAwareRunner()
            flag_calls: list[bool] = []
            with mock.patch.object(worker, "owned_daily_full_enable", return_value=False):
                with mock.patch.object(worker, "classify_continuous_gate", return_value=gate):
                    with mock.patch.object(worker, "write_owned_daily_flag", side_effect=lambda v: flag_calls.append(v)):
                        with _isolated_worker_loop():
                            code = worker.run_worker_loop(
                                args=SimpleNamespace(once=True, max_cycles=0),
                                config=config,  # type: ignore[arg-type]
                                runner=runner,  # type: ignore[arg-type]
                                poll_seconds=1,
                                max_jobs=3,
                                sleep_func=lambda _s: None,
                                logger=lambda *_a, **_k: None,
                            )
            self.assertEqual(code, 0)
            self.assertEqual(runner.calls, 1)
            self.assertEqual(runner.last_max_jobs, 1)
            self.assertTrue(runner.probe_modes[0])
            self.assertNotIn(False, flag_calls)

    def test_future_next_probe_at_defers_without_navigation(self) -> None:
        gate = {
            "workerState": "COOLDOWN",
            "hardStop": None,
            "availabilityState": "COOLDOWN",
            "probeAllowed": False,
            "cooldownUntil": "2099-06-01T00:00:00Z",
            "nextProbeAt": "2099-06-01T00:30:00Z",
            "activeChallenges": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _fake_config(root)
            runner = ProbeAwareRunner()
            with mock.patch.object(worker, "owned_daily_full_enable", return_value=False):
                with mock.patch.object(worker, "classify_continuous_gate", return_value=gate) as classify:
                    with _isolated_worker_loop():
                        worker.run_worker_loop(
                            args=SimpleNamespace(once=True, max_cycles=0),
                            config=config,  # type: ignore[arg-type]
                            runner=runner,  # type: ignore[arg-type]
                            poll_seconds=1,
                            max_jobs=1,
                            sleep_func=lambda _s: None,
                            logger=lambda *_a, **_k: None,
                        )
            self.assertEqual(runner.calls, 0)
            classify.assert_called()

    def test_challenge_hard_stops_without_probe(self) -> None:
        gate = {
            "workerState": "HARD_STOP",
            "hardStop": "ACTIVE_CHALLENGE",
            "availabilityState": "CHALLENGE_REQUIRED",
            "probeAllowed": False,
            "cooldownUntil": None,
            "nextProbeAt": None,
            "activeChallenges": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _fake_config(root)
            runner = ProbeAwareRunner()
            with mock.patch.object(worker, "owned_daily_full_enable", return_value=False):
                with mock.patch.object(worker, "classify_continuous_gate", return_value=gate):
                    with _isolated_worker_loop():
                        code = worker.run_worker_loop(
                            args=SimpleNamespace(once=True, max_cycles=0),
                            config=config,  # type: ignore[arg-type]
                            runner=runner,  # type: ignore[arg-type]
                            poll_seconds=1,
                            max_jobs=1,
                            sleep_func=lambda _s: None,
                            logger=lambda *_a, **_k: None,
                        )
            self.assertEqual(code, 2)
            self.assertEqual(runner.calls, 0)

    def test_after_probe_healthy_next_job_not_probe(self) -> None:
        probe_gate = {
            "workerState": "PROBE_REQUIRED",
            "hardStop": None,
            "availabilityState": "PROBE_REQUIRED",
            "probeAllowed": True,
            "activeChallenges": 0,
        }
        healthy_gate = {
            "workerState": "SELECTING",
            "hardStop": None,
            "availabilityState": "HEALTHY",
            "probeAllowed": False,
            "activeChallenges": 0,
        }
        gates = iter([probe_gate, healthy_gate])

        def _next_gate(**_kwargs):
            return next(gates)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _fake_config(root)
            runner = ProbeAwareRunner(
                outcomes=[
                    [{"jobId": "probe", "status": "completed", "ownedDailyOutcome": "UNCHANGED_FROM_EBAY"}],
                    [{"jobId": "normal", "status": "completed", "ownedDailyOutcome": "UNCHANGED_FROM_EBAY"}],
                ]
            )
            with mock.patch.object(worker, "owned_daily_full_enable", return_value=False):
                with mock.patch.object(worker, "classify_continuous_gate", side_effect=_next_gate):
                    with _isolated_worker_loop():
                        worker.run_worker_loop(
                            args=SimpleNamespace(once=False, max_cycles=2),
                            config=config,  # type: ignore[arg-type]
                            runner=runner,  # type: ignore[arg-type]
                            poll_seconds=1,
                            max_jobs=2,
                            sleep_func=lambda _s: None,
                            logger=lambda *_a, **_k: None,
                        )
            self.assertEqual(runner.calls, 2)
            self.assertTrue(runner.probe_modes[0])
            self.assertFalse(runner.probe_modes[1])

    def test_probe_server_failure_does_not_latch_safety_budget(self) -> None:
        gate = {
            "workerState": "PROBE_REQUIRED",
            "hardStop": None,
            "availabilityState": "PROBE_REQUIRED",
            "probeAllowed": True,
            "activeChallenges": 0,
        }
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _fake_config(root)
            runner = ProbeAwareRunner(
                outcomes=[
                    [
                        {
                            "jobId": "probe",
                            "status": "failed",
                            "ownedDailyOutcome": TEMPORARY_EBAY_SERVER_FAILURE,
                            "error": "ebay sorry page",
                        }
                    ]
                ]
            )
            with mock.patch.object(worker, "owned_daily_full_enable", return_value=True):
                with mock.patch.object(worker, "classify_continuous_gate", return_value=gate):
                    with _isolated_worker_loop(budget=budget):
                        worker.run_worker_loop(
                            args=SimpleNamespace(once=True, max_cycles=0),
                            config=config,  # type: ignore[arg-type]
                            runner=runner,  # type: ignore[arg-type]
                            poll_seconds=1,
                            max_jobs=1,
                            sleep_func=lambda _s: None,
                            logger=lambda *_a, **_k: None,
                        )
            self.assertIsNone(budget.last_hard_stop)
            self.assertEqual(budget.consecutive_transient, 0)


class WaitResultsAndTitleTests(unittest.TestCase):
    def test_stale_and_mixed_urls_never_confirm_matching_does(self) -> None:
        import tools.linux_x11_ebay_search as x11

        golbat = "Golbat 42 Pokemon"
        jolteon_url = "https://www.ebay.com.au/sch/i.html?_nkw=Jolteon+89+scarlet+violet+Pokemon&LH_Sold=1"
        mixed_url = "https://www.ebay.com.au/sch/i.html?_nkw=Golbat&_odkw=Jolteon&LH_Sold=1"
        golbat_url = "https://www.ebay.com.au/sch/i.html?_nkw=Golbat+42+Pokemon&LH_Sold=1"
        title_text = "Golbat 42 for sale | eBay Australia"
        poll = {"n": 0}

        def fake_title() -> str:
            return title_text

        def fake_url() -> str:
            poll["n"] += 1
            if poll["n"] < 3:
                return jolteon_url
            if poll["n"] == 3:
                return mixed_url
            return golbat_url

        with mock.patch.object(x11, "title", side_effect=fake_title):
            with mock.patch.object(x11, "cdp_active_url", side_effect=fake_url):
                with mock.patch.object(x11, "omnibox_url", side_effect=fake_url):
                    with mock.patch.object(x11, "time"):
                        stale = x11.wait_results(golbat, timeout=2)
        self.assertFalse(stale["ok"])
        self.assertFalse(stale.get("challenge"))
        self.assertFalse(stale.get("sorry"))

        poll["n"] = 0

        def matching_url() -> str:
            poll["n"] += 1
            return golbat_url

        with mock.patch.object(x11, "title", side_effect=fake_title):
            with mock.patch.object(x11, "cdp_active_url", side_effect=matching_url):
                with mock.patch.object(x11, "omnibox_url", side_effect=matching_url):
                    with mock.patch.object(x11, "time"):
                        ok = x11.wait_results(golbat, timeout=1)
        self.assertTrue(ok["ok"])

    def test_challenge_classified_before_stale_confirm(self) -> None:
        import tools.linux_x11_ebay_search as x11

        with mock.patch.object(x11, "title", return_value="Security measure | eBay"):
            with mock.patch.object(
                x11,
                "cdp_active_url",
                return_value="https://www.ebay.com.au/sch/i.html?_nkw=Golbat&LH_Sold=1",
            ):
                with mock.patch.object(x11, "is_ebay_challenge_page", return_value=True):
                    with mock.patch.object(x11, "time"):
                        out = x11.wait_results("Golbat", timeout=1)
        self.assertTrue(out.get("challenge"))

    def test_quoted_chrome_title_parses_fully(self) -> None:
        import tools.linux_x11_ebay_search as x11

        tree = (
            '     0x02a00003 "Drapion "52" Pokemon | eBay" "Google Chrome": '
            '("google-chrome" "Google-chrome")  1000x900+0+0  +0+0\n'
        )
        with mock.patch.object(x11.subprocess, "check_output", return_value=tree):
            parsed = x11.title()
        self.assertIn("Drapion", parsed)
        self.assertIn("52", parsed)
        self.assertIn("Pokemon", parsed)


class TimeoutReliefTests(unittest.TestCase):
    def test_run_retries_once_on_timeout_only(self) -> None:
        calls = {"n": 0}

        def flaky_run(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise subprocess.TimeoutExpired(cmd=["xwininfo"], timeout=1)
            if calls["n"] == 2:
                return subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
            raise subprocess.TimeoutExpired(cmd=["xwininfo"], timeout=1)

        with mock.patch.object(subprocess, "run", side_effect=flaky_run):
            result = _run(["xwininfo"])
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(calls["n"], 2)

    def test_run_propagates_second_timeout(self) -> None:
        with mock.patch.object(
            subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["xwininfo"], timeout=1),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                _run(["xwininfo"])

    def test_capture_defaults_and_cap(self) -> None:
        import importlib

        import cardscanr_market_engine.providers.post_sold_capture_process as psp

        importlib.reload(psp)
        with mock.patch.dict(os.environ, {}, clear=False):
            self.assertEqual(psp.DEFAULT_CAPTURE_DEADLINE_SECONDS, 25.0)
            self.assertEqual(psp.MAX_CAPTURE_DEADLINE_SECONDS, 30.0)
            self.assertEqual(psp.capture_deadline_seconds(), 25.0)
            self.assertEqual(psp.capture_deadline_seconds(99.0), 30.0)
            self.assertEqual(psp.DEFAULT_POST_SOLD_FINALIZE_SECONDS, 40.0)
            self.assertEqual(psp.post_sold_finalize_deadline_seconds(), 40.0)


SORRY_ROW = {
    "jobId": "sorry",
    "status": "failed",
    "ownedDailyOutcome": TEMPORARY_EBAY_SERVER_FAILURE,
    "error": "TEMPORARY_EBAY_SERVER_FAILURE: eBay SORRY/error page",
}
HEALTHY_ROW = {
    "jobId": "ok",
    "status": "completed",
    "ownedDailyOutcome": "UNCHANGED_FROM_EBAY",
    "searchSubmissionStarted": True,
}
RATE_LIMIT_ROW = {
    "jobId": "rl",
    "status": "failed",
    "ownedDailyOutcome": EBAY_RATE_LIMITED,
    "error": "HTTP 429 Too Many Requests: eBay marketplace throttle",
}


def _pre_classification_revert_429_outcome(exc_message: str) -> str:
    """QA-02 bug: HTTP 429 fell through to TEMPORARY_BROWSER_FAILURE."""
    if "429" in exc_message.lower():
        return TEMPORARY_BROWSER_FAILURE
    return classify_exception_outcome(exc_message, diagnostics={"statusCode": 429})


def _buggy_qa02_noop_latch(outcome: str | None, row: dict) -> bool:
    """Pre-QA-02 latch: excluded every TRANSIENT_EBAY_FAILURE_OUTCOME before the membership check."""
    text = str(outcome or "").strip()
    if not text:
        return False
    from cardscanr_market_engine.continuous_worker_policy import HARD_STOP_OUTCOMES
    from cardscanr_market_engine.owned_daily_outcomes import (
        CHALLENGE_REQUIRED,
        TEMPORARY_EBAY_SERVER_FAILURE,
        TRANSIENT_EBAY_FAILURE_OUTCOMES,
    )

    if text in HARD_STOP_OUTCOMES or text in {CHALLENGE_REQUIRED, "EBAY_AUTH_REQUIRED"}:
        return False
    if text in {
        TEMPORARY_EBAY_SERVER_FAILURE,
        "POST_SOLD_CAPTURE_FAILURE",
        "FINALIZE_TIMEOUT_SAFE",
        "TEMPORARY_BROWSER_FAILURE",
        "EBAY_ACCESS_DENIED_403",
    }:
        return False
    if worker.is_local_runtime_failure_message(str(row.get("error") or "")):
        return False
    return text in TRANSIENT_EBAY_FAILURE_OUTCOMES


class SorryTransientLatchTests(unittest.TestCase):
    def test_three_consecutive_sorry_latch_hard_stop(self) -> None:
        now = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        sorry_rows = [dict(SORRY_ROW, jobId=f"s{i}") for i in range(3)]
        for i, row in enumerate(sorry_rows):
            self.assertTrue(worker._counts_toward_marketplace_transient_latch(row["ownedDailyOutcome"], row))
            stop = budget.record_transient(now + timedelta(minutes=i))
            if i < 2:
                self.assertIsNone(stop)
            else:
                self.assertEqual(stop, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")
        self.assertEqual(budget.consecutive_transient, 3)
        self.assertEqual(budget.last_hard_stop, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")

    def test_success_between_sorry_resets_consecutive_count(self) -> None:
        now = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        sequence = [SORRY_ROW, SORRY_ROW, HEALTHY_ROW, SORRY_ROW, SORRY_ROW, SORRY_ROW]
        for i, row in enumerate(sequence):
            outcome = row.get("ownedDailyOutcome")
            if worker._counts_toward_marketplace_transient_latch(outcome, row):
                budget.record_transient(now + timedelta(minutes=i))
            elif outcome in {"UPDATED_FROM_EBAY", "UNCHANGED_FROM_EBAY", "CHECKED_NO_NEW_EXACT_EVIDENCE"}:
                budget.record_healthy()
        self.assertEqual(budget.consecutive_transient, 3)
        self.assertEqual(budget.last_hard_stop, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")

    def test_revert_proof_sorry_latch_is_not_noop(self) -> None:
        self.assertTrue(
            worker._counts_toward_marketplace_transient_latch(
                TEMPORARY_EBAY_SERVER_FAILURE,
                SORRY_ROW,
            )
        )
        self.assertFalse(_buggy_qa02_noop_latch(TEMPORARY_EBAY_SERVER_FAILURE, SORRY_ROW))


class RateLimitTransientLatchTests(unittest.TestCase):
    def test_http_429_classifies_as_marketplace_rate_limit(self) -> None:
        exc = ProviderRateLimitedError("HTTP 429 Too Many Requests", diagnostics={"statusCode": 429})
        self.assertEqual(classify_exception_outcome(exc, diagnostics=exc.diagnostics), EBAY_RATE_LIMITED)
        self.assertEqual(
            classify_exception_outcome(
                "HTTP 429 Too Many Requests",
                diagnostics={"statusCode": 429, "reason": "ebay_rate_limited"},
            ),
            EBAY_RATE_LIMITED,
        )

    def test_local_navigation_timeout_stays_browser_failure(self) -> None:
        self.assertEqual(
            classify_exception_outcome(
                "desktop sold navigation failed: timeout waiting for results",
                diagnostics={},
            ),
            TEMPORARY_BROWSER_FAILURE,
        )

    def test_local_timeout_with_429_in_message_stays_browser_failure(self) -> None:
        cases = [
            (
                "CDP_CAPTURE_PROCESS_TIMEOUT: http timeout after 429ms waiting for sold results",
                {"reason": "cdp_post_sold_capture_failed"},
                POST_SOLD_CAPTURE_FAILURE,
            ),
            (
                "HTTP 429 Too Many Requests",
                {},
                TEMPORARY_BROWSER_FAILURE,
            ),
        ]
        for message, diagnostics, expected in cases:
            with self.subTest(message=message):
                outcome = classify_exception_outcome(message, diagnostics=diagnostics)
                self.assertEqual(outcome, expected)
                self.assertNotEqual(outcome, EBAY_RATE_LIMITED)
                self.assertFalse(
                    worker._counts_toward_marketplace_transient_latch(
                        outcome,
                        {"ownedDailyOutcome": outcome, "error": message},
                    )
                )

    def test_three_consecutive_429_latch_hard_stop(self) -> None:
        now = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        rows = [dict(RATE_LIMIT_ROW, jobId=f"r{i}") for i in range(3)]
        for i, row in enumerate(rows):
            self.assertTrue(worker._counts_toward_marketplace_transient_latch(row["ownedDailyOutcome"], row))
            stop = budget.record_transient(now + timedelta(minutes=i))
            if i < 2:
                self.assertIsNone(stop)
            else:
                self.assertEqual(stop, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")
        self.assertEqual(budget.consecutive_transient, 3)

    def test_sorry_and_429_mix_accumulates_to_hard_stop(self) -> None:
        now = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        sequence = [SORRY_ROW, SORRY_ROW, RATE_LIMIT_ROW]
        for i, row in enumerate(sequence):
            self.assertTrue(worker._counts_toward_marketplace_transient_latch(row["ownedDailyOutcome"], row))
            stop = budget.record_transient(now + timedelta(minutes=i))
            if i < 2:
                self.assertIsNone(stop)
            else:
                self.assertEqual(stop, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")

    def test_healthy_row_resets_sorry_and_429_streak(self) -> None:
        now = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        sequence = [SORRY_ROW, RATE_LIMIT_ROW, HEALTHY_ROW, SORRY_ROW, RATE_LIMIT_ROW, SORRY_ROW]
        for i, row in enumerate(sequence):
            outcome = row.get("ownedDailyOutcome")
            if worker._counts_toward_marketplace_transient_latch(outcome, row):
                budget.record_transient(now + timedelta(minutes=i))
            elif outcome in {"UPDATED_FROM_EBAY", "UNCHANGED_FROM_EBAY", "CHECKED_NO_NEW_EXACT_EVIDENCE"}:
                budget.record_healthy()
        self.assertEqual(budget.last_hard_stop, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")

    def test_revert_proof_429_browser_outcome_does_not_latch(self) -> None:
        now = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        msg = "HTTP 429 Too Many Requests"
        reverted_outcome = _pre_classification_revert_429_outcome(msg)
        self.assertEqual(reverted_outcome, TEMPORARY_BROWSER_FAILURE)
        row = {"ownedDailyOutcome": reverted_outcome, "error": msg}
        for _ in range(5):
            if worker._counts_toward_marketplace_transient_latch(row["ownedDailyOutcome"], row):
                budget.record_transient(now)
        self.assertIsNone(budget.last_hard_stop)
        self.assertEqual(budget.consecutive_transient, 0)


class LocalTransientSafetyTests(unittest.TestCase):
    def test_local_failures_do_not_latch_hard_stop(self) -> None:
        now = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        local_rows = [
            {
                "ownedDailyOutcome": TEMPORARY_BROWSER_FAILURE,
                "error": "Command 'xdotool windowactivate --sync 12582915' returned non-zero exit status 1.",
            },
            {
                "ownedDailyOutcome": POST_SOLD_CAPTURE_FAILURE,
                "error": "CDP_CAPTURE_PROCESS_TIMEOUT: capture worker exceeded deadline",
            },
            {
                "ownedDailyOutcome": TEMPORARY_BROWSER_FAILURE,
                "error": "X11_FOCUS_FAILED:chrome_focus_failed",
            },
        ]
        for row in local_rows:
            if worker._counts_toward_marketplace_transient_latch(row["ownedDailyOutcome"], row):
                budget.record_transient(now)
        self.assertIsNone(budget.last_hard_stop)
        self.assertEqual(budget.consecutive_transient, 0)

    def test_captcha_mixed_with_local_still_latches(self) -> None:
        from cardscanr_market_engine.continuous_worker_policy import HARD_STOP_OUTCOMES

        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        now = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
        for _ in range(3):
            self.assertFalse(
                worker._counts_toward_marketplace_transient_latch(
                    TEMPORARY_BROWSER_FAILURE,
                    {"error": "Command 'xdotool windowactivate"},
                )
            )
        self.assertIn(CHALLENGE_REQUIRED, HARD_STOP_OUTCOMES)
        self.assertFalse(worker._counts_toward_marketplace_transient_latch(CHALLENGE_REQUIRED, {"error": "captcha"}))
        for i in range(3):
            budget.record_transient(now + timedelta(minutes=i))
        self.assertEqual(budget.last_hard_stop, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")

    def test_record_healthy_clears_transient_history(self) -> None:
        now = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        budget.record_transient(now)
        budget.record_transient(now + timedelta(minutes=1))
        budget.last_hard_stop = "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES"
        budget.record_healthy()
        self.assertEqual(budget.transients, [])
        self.assertIsNone(budget.last_hard_stop)


class CapsAndProbeToolTests(unittest.TestCase):
    def test_au_caps_and_market_allowlist(self) -> None:
        au_cap_values = {
            "MAX_LIVE_SUBMISSIONS_PER_HOUR": "20",
            "MAX_LIVE_SUBMISSIONS_PER_DAY": "200",
            "OWNED_DAILY_ALLOWED_MARKETS": "AU",
            "MARKET_WORKER_CONCURRENCY": "1",
        }
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "budget.json"
            with mock.patch.dict(os.environ, au_cap_values, clear=False):
                self.assertEqual(os.environ.get("MAX_LIVE_SUBMISSIONS_PER_HOUR"), "20")
                self.assertEqual(os.environ.get("MAX_LIVE_SUBMISSIONS_PER_DAY"), "200")
                self.assertEqual(os.environ.get("OWNED_DAILY_ALLOWED_MARKETS"), "AU")
                budget = ContinuousSafetyBudget.from_env(path=ledger)
                self.assertEqual(budget.max_submissions_per_hour, 20)
                self.assertEqual(budget.max_submissions_per_day, 200)
            now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
            hour_anchor = now - timedelta(minutes=59)
            for i in range(20):
                budget.record_submission(hour_anchor + timedelta(seconds=30 * i))
            self.assertEqual(budget.budget_exhausted(now), "HOURLY_SUBMISSION_CEILING")
            budget2 = ContinuousSafetyBudget.from_env(path=ledger)
            for i in range(199):
                budget2.record_submission(now - timedelta(hours=3) - timedelta(minutes=i))
            budget2.record_submission(now)
            self.assertEqual(budget2.budget_exhausted(now), "DAILY_SUBMISSION_CEILING")
        self.assertTrue(is_region_dispatchable(region_definition("AU").region))
        self.assertFalse(is_region_dispatchable(region_definition("JP").region))
        self.assertFalse(is_region_dispatchable(region_definition("EU").region))

    def test_recovery_probe_tool_offline_mock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ebay_availability_state.json"
            record_sorry(now=datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc), path=path)
            after = datetime(2026, 11, 16, 7, 5, tzinfo=timezone.utc)
            allowed, _, snap = browser_work_allowed(now=after, path=path, for_probe=True)
            self.assertTrue(allowed)
            self.assertEqual(snap.state, "PROBE_REQUIRED")
            begin_probe(now=after, path=path)
            allowed2, reason2, _ = browser_work_allowed(now=after, path=path, for_probe=True)
            self.assertFalse(allowed2)
            self.assertEqual(reason2, "EBAY_AVAILABILITY_PROBE_IN_FLIGHT")
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(doc.get("probeInFlight") or doc.get("probe_in_flight"))


if __name__ == "__main__":
    unittest.main()