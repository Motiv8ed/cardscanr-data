"""Pricing outage #33: staged budget gates + gated-branch pipeline heartbeats."""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.owned_daily_enablement import BUDGET_LEDGER_PATH, STATUS_PATH
from cardscanr_market_engine.owned_daily_pacing import OwnedDailyPacingController
import workers.market_price_worker as worker


class FakeHeartbeatClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.heartbeats: list[dict] = []
        self.fail = fail

    def upsert_pipeline_heartbeat(self, **kwargs) -> dict:
        if self.fail:
            raise RuntimeError("heartbeat_unavailable")
        self.heartbeats.append(dict(kwargs))
        return {"ok": True}


class FakeRunner:
    def __init__(
        self,
        *,
        outcomes: list[object] | None = None,
        client: FakeHeartbeatClient | None = None,
    ) -> None:
        self.client = client or FakeHeartbeatClient()
        self.outcomes = list(outcomes or [[{"jobId": "j1", "status": "completed"}]])
        self.calls = 0

    def run_once(self, *, max_jobs: int | None = None) -> list[dict]:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


def fake_args(*, once: bool = False, max_cycles: int = 0) -> SimpleNamespace:
    return SimpleNamespace(once=once, max_cycles=max_cycles)


def fake_config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        worker_id="worker-test",
        provider_name="mock",
        latest_report_path=root / "market_price_worker_latest.json",
        runs_report_path=root / "market_price_worker_runs.jsonl",
    )


def _clean_budget() -> ContinuousSafetyBudget:
    return ContinuousSafetyBudget(
        max_submissions_per_hour=20,
        max_submissions_per_day=200,
        max_consecutive_transient=3,
        max_transient_per_hour=5,
    )


def _staged_latched_budget() -> ContinuousSafetyBudget:
    budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
    budget.last_hard_stop = "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES"
    return budget


@contextlib.contextmanager
def isolated_worker_loop(*, budget: ContinuousSafetyBudget | None = None):
    """Hermetic patches so worker-loop tests never touch repo reports/runtime."""
    injected = budget if budget is not None else _clean_budget()
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch.object(worker.ContinuousSafetyBudget, "from_env", return_value=injected)
        )
        stack.enter_context(mock.patch.object(worker.ContinuousSafetyBudget, "persist"))
        stack.enter_context(mock.patch.object(worker, "write_owned_daily_flag"))
        stack.enter_context(mock.patch.object(worker, "write_continuous_status"))
        stack.enter_context(mock.patch.object(worker, "append_daily_metrics"))
        stack.enter_context(mock.patch.object(worker, "write_json"))
        stack.enter_context(mock.patch.object(worker, "append_jsonl"))
        yield injected


class StagedGatedHeartbeatTests(unittest.TestCase):
    def test_staged_hard_stop_once_emits_heartbeat_and_skips_run_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            client = FakeHeartbeatClient()
            runner = FakeRunner(client=client)
            budget = _staged_latched_budget()
            latch_before = budget.last_hard_stop
            env = {
                "MARKET_WORKER_ENFORCE_SAFETY_BUDGET": "true",
            }
            with mock.patch.dict("os.environ", env, clear=False):
                with mock.patch.object(worker, "owned_daily_full_enable", return_value=False):
                    with isolated_worker_loop(budget=budget):
                        code = worker.run_worker_loop(
                            args=fake_args(once=True),
                            config=config,  # type: ignore[arg-type]
                            runner=runner,  # type: ignore[arg-type]
                            poll_seconds=5,
                            max_jobs=1,
                            sleep_func=lambda _s: None,
                            logger=lambda *_a, **_k: None,
                        )
            self.assertEqual(code, 2)
            self.assertEqual(runner.calls, 0)
            self.assertEqual(len(client.heartbeats), 1)
            hb = client.heartbeats[0]
            self.assertEqual(hb["state"], "hard_stop")
            self.assertTrue(hb["meta"]["awaitingOwnerReview"])
            self.assertEqual(
                hb["meta"]["gateReason"],
                "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
            )
            self.assertEqual(hb["meta"]["gateMode"], "staged")
            self.assertEqual(budget.last_hard_stop, latch_before)

    def test_staged_budget_wait_emits_heartbeat_and_skips_run_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            client = FakeHeartbeatClient()
            runner = FakeRunner(client=client)
            budget = ContinuousSafetyBudget(max_submissions_per_hour=1)
            budget.record_submission()
            env = {"MARKET_WORKER_ENFORCE_SAFETY_BUDGET": "true"}
            with mock.patch.dict("os.environ", env, clear=False):
                with mock.patch.object(worker, "owned_daily_full_enable", return_value=False):
                    with isolated_worker_loop(budget=budget):
                        code = worker.run_worker_loop(
                            args=fake_args(once=True),
                            config=config,  # type: ignore[arg-type]
                            runner=runner,  # type: ignore[arg-type]
                            poll_seconds=5,
                            max_jobs=1,
                            sleep_func=lambda _s: None,
                            logger=lambda *_a, **_k: None,
                        )
            self.assertEqual(code, 0)
            self.assertEqual(runner.calls, 0)
            self.assertEqual(client.heartbeats[0]["state"], "budget_wait")
            self.assertEqual(client.heartbeats[0]["meta"]["gateMode"], "staged")


class ContinuousGatedHeartbeatTests(unittest.TestCase):
    def test_continuous_hard_stop_emits_heartbeat_without_run_once(self) -> None:
        gate = {
            "workerState": "HARD_STOP",
            "hardStop": "ACTIVE_CHALLENGE",
            "cooldownUntil": None,
            "nextProbeAt": None,
            "activeChallenges": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            client = FakeHeartbeatClient()
            runner = FakeRunner(client=client)
            with mock.patch.object(worker, "owned_daily_full_enable", return_value=True):
                with mock.patch.object(worker, "classify_continuous_gate", return_value=gate):
                    with isolated_worker_loop():
                        code = worker.run_worker_loop(
                            args=fake_args(once=True),
                            config=config,  # type: ignore[arg-type]
                            runner=runner,  # type: ignore[arg-type]
                            poll_seconds=5,
                            max_jobs=1,
                            sleep_func=lambda _s: None,
                            logger=lambda *_a, **_k: None,
                        )
            self.assertEqual(code, 2)
            self.assertEqual(runner.calls, 0)
            self.assertEqual(client.heartbeats[0]["state"], "hard_stop")
            self.assertEqual(client.heartbeats[0]["meta"]["gateMode"], "continuous")

    def test_continuous_cooldown_emits_heartbeat_without_run_once(self) -> None:
        gate = {
            "workerState": "COOLDOWN",
            "hardStop": None,
            "cooldownUntil": "2099-01-01T00:00:00Z",
            "nextProbeAt": "2099-01-01T01:00:00Z",
            "activeChallenges": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            client = FakeHeartbeatClient()
            runner = FakeRunner(client=client)
            with mock.patch.object(worker, "owned_daily_full_enable", return_value=True):
                with mock.patch.object(worker, "classify_continuous_gate", return_value=gate):
                    with isolated_worker_loop():
                        code = worker.run_worker_loop(
                            args=fake_args(once=True),
                            config=config,  # type: ignore[arg-type]
                            runner=runner,  # type: ignore[arg-type]
                            poll_seconds=5,
                            max_jobs=1,
                            sleep_func=lambda _s: None,
                            logger=lambda *_a, **_k: None,
                        )
            self.assertEqual(code, 0)
            self.assertEqual(runner.calls, 0)
            self.assertEqual(client.heartbeats[0]["state"], "cooldown")

    def test_continuous_cooldown_uses_injected_budget_despite_latched_ledger_file(self) -> None:
        """Regression: on-disk latched ledger must not affect loop when from_env is injected."""
        gate = {
            "workerState": "COOLDOWN",
            "hardStop": None,
            "cooldownUntil": "2099-01-01T00:00:00Z",
            "nextProbeAt": "2099-01-01T01:00:00Z",
            "activeChallenges": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            client = FakeHeartbeatClient()
            runner = FakeRunner(client=client)
            latched_ledger = root / "continuous_au_safety_ledger.json"
            latched_ledger.write_text(
                json.dumps({"lastHardStop": "ACTIVE_CHALLENGE", "consecutiveTransient": 3}),
                encoding="utf-8",
            )
            injected = _clean_budget()
            self.assertIsNone(injected.last_hard_stop)
            with mock.patch.object(worker, "owned_daily_full_enable", return_value=True):
                with mock.patch.object(worker, "classify_continuous_gate", return_value=gate):
                    with mock.patch(
                        "cardscanr_market_engine.owned_daily_enablement.BUDGET_LEDGER_PATH",
                        latched_ledger,
                    ):
                        with isolated_worker_loop(budget=injected):
                            code = worker.run_worker_loop(
                                args=fake_args(once=True),
                                config=config,  # type: ignore[arg-type]
                                runner=runner,  # type: ignore[arg-type]
                                poll_seconds=5,
                                max_jobs=1,
                                sleep_func=lambda _s: None,
                                logger=lambda *_a, **_k: None,
                            )
            self.assertEqual(code, 0)
            self.assertEqual(runner.calls, 0)
            self.assertEqual(client.heartbeats[0]["state"], "cooldown")
            self.assertIsNone(injected.last_hard_stop)


class GatedHeartbeatResilienceTests(unittest.TestCase):
    def test_heartbeat_exception_does_not_crash_staged_hard_stop_loop(self) -> None:
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            client = FakeHeartbeatClient(fail=True)
            runner = FakeRunner(client=client)
            budget = _staged_latched_budget()
            with mock.patch.dict("os.environ", {"MARKET_WORKER_ENFORCE_SAFETY_BUDGET": "true"}, clear=False):
                with mock.patch.object(worker, "owned_daily_full_enable", return_value=False):
                    with isolated_worker_loop(budget=budget):
                        code = worker.run_worker_loop(
                            args=fake_args(once=True),
                            config=config,  # type: ignore[arg-type]
                            runner=runner,  # type: ignore[arg-type]
                            poll_seconds=5,
                            max_jobs=1,
                            sleep_func=lambda _s: None,
                            logger=logs.append,
                        )
            self.assertEqual(code, 2)
            self.assertTrue(any("heartbeat_warning=RuntimeError" in line for line in logs))

    def test_normal_path_single_processing_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            client = FakeHeartbeatClient()
            runner = FakeRunner(client=client)
            with mock.patch.object(worker, "owned_daily_full_enable", return_value=False):
                with isolated_worker_loop():
                    code = worker.run_worker_loop(
                        args=fake_args(once=True),
                        config=config,  # type: ignore[arg-type]
                        runner=runner,  # type: ignore[arg-type]
                        poll_seconds=5,
                        max_jobs=1,
                        sleep_func=lambda _s: None,
                        logger=lambda *_a, **_k: None,
                    )
            self.assertEqual(code, 0)
            self.assertEqual(runner.calls, 1)
            self.assertEqual(len(client.heartbeats), 1)
            self.assertEqual(client.heartbeats[0]["state"], "processing")
            self.assertNotIn("gateReason", client.heartbeats[0]["meta"])

    def test_browser_halt_branches_emit_gated_heartbeat(self) -> None:
        pacing = OwnedDailyPacingController()
        pacing.observe_outcome("CHALLENGE_REQUIRED")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            client = FakeHeartbeatClient()
            runner = FakeRunner(client=client)
            with mock.patch.object(worker, "owned_daily_full_enable", return_value=False):
                with isolated_worker_loop():
                    code = worker.run_worker_loop(
                        args=fake_args(once=True),
                        config=config,  # type: ignore[arg-type]
                        runner=runner,  # type: ignore[arg-type]
                        poll_seconds=5,
                        max_jobs=1,
                        sleep_func=lambda _s: None,
                        logger=lambda *_a, **_k: None,
                        pacing=pacing,
                    )
            self.assertEqual(code, 0)
            self.assertEqual(runner.calls, 0)
            self.assertEqual(client.heartbeats[0]["state"], "browser_halted")


class HealthReportGateTests(unittest.TestCase):
    def test_latch_present_marks_worker_unhealthy(self) -> None:
        from scripts import report_ebay_pricing_health as health

        class MinimalClient:
            def _table_get(self, *_args, **_kwargs):  # noqa: SLF001
                return []

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            status = runtime / "continuous_au_worker_status.json"
            ledger = runtime / "continuous_au_safety_ledger.json"
            status.write_text(
                json.dumps(
                    {
                        "workerState": "HARD_STOP",
                        "lastHardStopReason": "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
                    }
                ),
                encoding="utf-8",
            )
            ledger.write_text(
                json.dumps({"lastHardStop": "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES"}),
                encoding="utf-8",
            )
            with mock.patch.object(health, "STATUS_PATH", status):
                with mock.patch.object(health, "BUDGET_LEDGER_PATH", ledger):
                    with mock.patch.object(health, "_count_python_workers", return_value=[11111]):
                        report = health.build_health_report(client=MinimalClient())  # type: ignore[arg-type]
            worker_row = report["worker"]
            self.assertFalse(worker_row["healthy"])
            self.assertEqual(worker_row["gateState"], "HARD_STOP")
            self.assertEqual(
                worker_row["gateReason"],
                "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
            )
            self.assertIn("awaiting owner review", worker_row["note"])

    def test_missing_runtime_files_do_not_crash_health_report(self) -> None:
        from scripts import report_ebay_pricing_health as health

        class MinimalClient:
            def _table_get(self, *_args, **_kwargs):  # noqa: SLF001
                return []

        with tempfile.TemporaryDirectory() as tmp:
            missing_status = Path(tmp) / "missing_status.json"
            missing_ledger = Path(tmp) / "missing_ledger.json"
            with mock.patch.object(health, "STATUS_PATH", missing_status):
                with mock.patch.object(health, "BUDGET_LEDGER_PATH", missing_ledger):
                    with mock.patch.object(health, "_count_python_workers", return_value=[22222]):
                        report = health.build_health_report(client=MinimalClient())  # type: ignore[arg-type]
            self.assertIsNone(report["worker"]["gateState"])
            self.assertIsNone(report["worker"]["gateReason"])


if __name__ == "__main__":
    unittest.main()
