"""Worker-loop regressions for DATA-63 QA (HIGH-1/HIGH-2/MEDIUM-6)."""
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
    HARD_STOP_SOURCE_SET_SWEEP,
    ContinuousSafetyBudget,
)
from cardscanr_market_engine.ebay_scale.set_sweep_worker_hook import SetSweepExecutionResult
import workers.market_price_worker as worker


def _fake_config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        worker_id="worker-test",
        provider_name="ebay_browser",
        latest_report_path=root / "latest.json",
        runs_report_path=root / "runs.jsonl",
    )


class SweepTrackingRunner:
    def __init__(self) -> None:
        self.client = mock.Mock()
        self.calls = 0
        self._ebay_probe_mode = False

    def run_once(self, *, max_jobs: int | None = None) -> list[dict]:
        self.calls += 1
        return [
            {
                "jobId": "j1",
                "status": "completed",
                "ownedDailyOutcome": "UNCHANGED_FROM_EBAY",
                "searchSubmissionStarted": True,
            }
        ]


@contextlib.contextmanager
def _isolated_worker_loop(*, budget: ContinuousSafetyBudget, root: Path):
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch.dict(
                os.environ,
                {
                    "EBAY_SCALE_ENABLED": "true",
                    "EBAY_SCALE_SET_SWEEP": "true",
                    "EBAY_SCALE_SHADOW_AUDIT": "true",
                    "CARDSCANR_OPERATION_MODE": "",
                },
                clear=False,
            )
        )
        stack.enter_context(mock.patch.object(worker.ContinuousSafetyBudget, "from_env", return_value=budget))
        stack.enter_context(mock.patch.object(worker, "owned_daily_full_enable", return_value=True))
        stack.enter_context(mock.patch.object(worker, "write_json"))
        stack.enter_context(mock.patch.object(worker, "append_jsonl"))
        stack.enter_context(mock.patch.object(worker, "write_owned_daily_flag"))
        stack.enter_context(mock.patch.object(worker, "write_continuous_status"))
        stack.enter_context(mock.patch.object(worker, "append_daily_metrics"))
        stack.enter_context(
            mock.patch.object(
                worker,
                "classify_continuous_gate",
                return_value={
                    "workerState": "SELECTING",
                    "hardStop": None,
                    "availabilityState": "HEALTHY",
                    "probeAllowed": False,
                    "cooldownUntil": None,
                    "nextProbeAt": None,
                    "activeChallenges": 0,
                },
            )
        )
        yield


class SetSweepWorkerLoopTests(unittest.TestCase):
    def test_sweep_hard_stop_skips_run_once_same_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            runner = SweepTrackingRunner()
            sweep_result = SetSweepExecutionResult(
                ok=False,
                reason="fetch_failed",
                submission_consumed=True,
                hard_stop_reason="MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
            )

            def _fake_sweep(**_kwargs):
                budget.latch_hard_stop(
                    str(sweep_result.hard_stop_reason),
                    hard_stop_source=HARD_STOP_SOURCE_SET_SWEEP,
                )
                budget.persist(ledger)
                return sweep_result

            with _isolated_worker_loop(budget=budget, root=root):
                with (
                    mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger),
                    mock.patch(
                        "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.try_set_sweep_worker_cycle",
                        side_effect=_fake_sweep,
                    ),
                ):
                    code = worker.run_worker_loop(
                        args=SimpleNamespace(once=True, max_cycles=0),
                        config=_fake_config(root),  # type: ignore[arg-type]
                        runner=runner,  # type: ignore[arg-type]
                        poll_seconds=1,
                        max_jobs=3,
                        sleep_func=lambda _s: None,
                        logger=lambda *_a, **_k: None,
                    )
            budget.reload(ledger)
            self.assertEqual(code, 2)
            self.assertEqual(runner.calls, 0)
            self.assertEqual(budget.last_hard_stop, sweep_result.hard_stop_reason)
            self.assertEqual(budget.hard_stop_source, HARD_STOP_SOURCE_SET_SWEEP)

    def test_record_healthy_does_not_clear_set_sweep_latch(self) -> None:
        budget = ContinuousSafetyBudget()
        budget.latch_hard_stop("CHALLENGE_REQUIRED", hard_stop_source=HARD_STOP_SOURCE_SET_SWEEP)
        budget.record_healthy()
        self.assertEqual(budget.last_hard_stop, "CHALLENGE_REQUIRED")
        self.assertEqual(budget.hard_stop_source, HARD_STOP_SOURCE_SET_SWEEP)

    def test_probe_required_does_not_invoke_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            runner = SweepTrackingRunner()
            gate = {
                "workerState": "PROBE_REQUIRED",
                "hardStop": None,
                "availabilityState": "PROBE_REQUIRED",
                "probeAllowed": True,
                "cooldownUntil": None,
                "nextProbeAt": "2020-01-01T00:00:00Z",
                "activeChallenges": 0,
            }
            sweep_calls = {"n": 0}

            def _sweep(**_kwargs):
                sweep_calls["n"] += 1
                return None

            with _isolated_worker_loop(budget=budget, root=root):
                with mock.patch.object(worker, "classify_continuous_gate", return_value=gate):
                    with mock.patch(
                        "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.try_set_sweep_worker_cycle",
                        side_effect=_sweep,
                    ):
                        worker.run_worker_loop(
                            args=SimpleNamespace(once=True, max_cycles=0),
                            config=_fake_config(root),  # type: ignore[arg-type]
                            runner=runner,  # type: ignore[arg-type]
                            poll_seconds=1,
                            max_jobs=1,
                            sleep_func=lambda _s: None,
                            logger=lambda *_a, **_k: None,
                        )
            self.assertEqual(sweep_calls["n"], 0)


if __name__ == "__main__":
    unittest.main()