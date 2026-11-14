"""Pricing outage #33: staged budget gates + gated-branch pipeline heartbeats."""

from __future__ import annotations

import contextlib
import json
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

from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.owned_daily_enablement import BUDGET_LEDGER_PATH, STATUS_PATH
from cardscanr_market_engine.owned_daily_outcomes import TEMPORARY_EBAY_SERVER_FAILURE
from cardscanr_market_engine.owned_daily_pacing import OwnedDailyPacingController
from cardscanr_market_engine.scheduler import utc_iso
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


def _write_fresh_runtime_pair(
    *,
    status_path: Path,
    ledger_path: Path,
    worker_state: str = "SELECTING",
    last_hard_stop: str | None = None,
    ledger_hard_stop: str | None = None,
) -> None:
    now = utc_iso()
    status_path.write_text(
        json.dumps(
            {
                "workerState": worker_state,
                "lastHardStopReason": last_hard_stop,
                "updatedAtUtc": now,
            }
        ),
        encoding="utf-8",
    )
    ledger_path.write_text(
        json.dumps(
            {
                "lastHardStop": ledger_hard_stop,
                "updatedAtUtc": now,
            }
        ),
        encoding="utf-8",
    )


def _fresh_db_worker_heartbeat() -> dict:
    return {
        "component": "worker",
        "state": "processing",
        "last_heartbeat_at": utc_iso(),
        "meta": {},
    }


@contextlib.contextmanager
def health_report_runtime_mocks(health, *, status: Path, ledger: Path, seed: bool = True):
    """Fresh local runtime + DB heartbeat so health tests isolate the gate under test."""
    if seed:
        _write_fresh_runtime_pair(status_path=status, ledger_path=ledger)
    with mock.patch.object(health, "STATUS_PATH", status):
        with mock.patch.object(health, "BUDGET_LEDGER_PATH", ledger):
            with mock.patch.object(
                health,
                "_fetch_worker_db_heartbeat",
                return_value=_fresh_db_worker_heartbeat(),
            ):
                yield


@contextlib.contextmanager
def isolated_worker_loop(
    *,
    budget: ContinuousSafetyBudget | None = None,
    operation_mode: str = "CONTINUOUS",
):
    """Hermetic patches so worker-loop tests never touch repo reports/runtime."""
    injected = budget if budget is not None else _clean_budget()
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch.dict(
                "os.environ",
                {"CARDSCANR_OPERATION_MODE": operation_mode},
                clear=False,
            )
        )
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

    def test_continuous_budget_wait_emits_heartbeat_without_run_once(self) -> None:
        gate = {
            "workerState": "SELECTING",
            "hardStop": None,
            "cooldownUntil": None,
            "nextProbeAt": None,
            "activeChallenges": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            client = FakeHeartbeatClient()
            runner = FakeRunner(client=client)
            budget = ContinuousSafetyBudget(max_submissions_per_hour=1)
            budget.record_submission()
            with mock.patch.object(worker, "owned_daily_full_enable", return_value=True):
                with mock.patch.object(worker, "classify_continuous_gate", return_value=gate):
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
            self.assertEqual(client.heartbeats[0]["meta"]["gateMode"], "continuous")

    def test_probe_hard_stop_preserves_gate_mode_in_heartbeat(self) -> None:
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
            with mock.patch.object(worker, "owned_daily_full_enable", return_value=False):
                with mock.patch.object(worker, "classify_continuous_gate", return_value=gate):
                    with isolated_worker_loop(operation_mode="PROBE"):
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
            self.assertEqual(client.heartbeats[0]["meta"]["gateMode"], "PROBE")


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

    def test_transient_halt_branch_emits_gated_heartbeat(self) -> None:
        pacing = OwnedDailyPacingController()
        pacing.state.browser_halted = True
        pacing.state.halt_reason = TEMPORARY_EBAY_SERVER_FAILURE
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
            self.assertEqual(client.heartbeats[0]["state"], "transient_halt")


class HealthReportRuntimeEvidenceTests(unittest.TestCase):
    def test_future_timestamp_beyond_tolerance_is_stale(self) -> None:
        from scripts import report_ebay_pricing_health as health

        now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
        future = (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        payload = {"updatedAtUtc": future}
        label, age = health._runtime_file_evidence(
            payload,
            path=Path("/unused/status.json"),
            now=now,
        )
        self.assertEqual(label, "stale")
        self.assertIsNotNone(age)
        self.assertLess(age, -2)

    def test_future_timestamp_within_tolerance_is_fresh(self) -> None:
        from scripts import report_ebay_pricing_health as health

        now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
        future = (now + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        payload = {"updatedAtUtc": future}
        label, _age = health._runtime_file_evidence(
            payload,
            path=Path("/unused/status.json"),
            now=now,
        )
        self.assertEqual(label, "fresh")


class HealthReportDbHeartbeatSelectionTests(unittest.TestCase):
    def test_unconfigured_worker_id_fails_closed_when_multiple_workers(self) -> None:
        from scripts import report_ebay_pricing_health as health

        older_healthy = {
            "component": "worker",
            "worker_id": "worker-a",
            "state": "idle",
            "last_heartbeat_at": "2026-11-13T10:00:00Z",
            "meta": {},
        }
        newer_failing = {
            "component": "worker",
            "worker_id": "worker-b",
            "state": "hard_stop",
            "last_heartbeat_at": "2026-11-13T10:10:00Z",
            "meta": {},
        }

        class Client:
            def __init__(self, rows: list[dict]) -> None:
                self.rows = rows
                self.last_params: dict | None = None

            def _table_get(self, _table: str, *, params: dict) -> list[dict]:  # noqa: SLF001
                self.last_params = params
                return list(self.rows)

        client = Client([older_healthy, newer_failing])
        with mock.patch.dict(os.environ, {"MARKET_WORKER_ID": ""}, clear=False):
            picked = health._fetch_worker_db_heartbeat(client)  # type: ignore[arg-type]
        self.assertIsNone(picked)
        self.assertNotIn("worker_id", client.last_params or {})

    def test_unconfigured_worker_id_uses_sole_worker_newest_row(self) -> None:
        from scripts import report_ebay_pricing_health as health

        older = {
            "component": "worker",
            "worker_id": "worker-only",
            "state": "idle",
            "last_heartbeat_at": "2026-11-13T10:00:00Z",
            "meta": {},
        }
        newer_gated = {
            "component": "worker",
            "worker_id": "worker-only",
            "state": "hard_stop",
            "last_heartbeat_at": "2026-11-13T10:10:00Z",
            "meta": {},
        }

        class Client:
            def _table_get(self, _table: str, *, params: dict) -> list[dict]:  # noqa: SLF001
                return [older, newer_gated]

        with mock.patch.dict(os.environ, {"MARKET_WORKER_ID": ""}, clear=False):
            picked = health._fetch_worker_db_heartbeat(Client())  # type: ignore[arg-type]
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["worker_id"], "worker-only")
        self.assertEqual(picked["state"], "hard_stop")

    def test_worker_id_filter_prefers_matching_row_over_newer_other_worker(self) -> None:
        from scripts import report_ebay_pricing_health as health

        older_target = {
            "component": "worker",
            "worker_id": "worker-target",
            "state": "idle",
            "last_heartbeat_at": "2026-11-13T10:00:00Z",
            "meta": {},
        }
        newer_other = {
            "component": "worker",
            "worker_id": "worker-other",
            "state": "hard_stop",
            "last_heartbeat_at": "2026-11-13T10:20:00Z",
            "meta": {},
        }

        class Client:
            def _table_get(self, _table: str, *, params: dict) -> list[dict]:  # noqa: SLF001
                if params.get("worker_id") == "eq.worker-target":
                    return [older_target]
                return [older_target, newer_other]

        picked = health._fetch_worker_db_heartbeat(Client(), worker_id="worker-target")  # type: ignore[arg-type]
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["worker_id"], "worker-target")
        self.assertEqual(picked["state"], "idle")

    def test_newer_matching_row_wins_over_older_matching_row(self) -> None:
        from scripts import report_ebay_pricing_health as health

        rows = [
            {
                "component": "worker",
                "worker_id": "worker-target",
                "state": "idle",
                "last_heartbeat_at": "2026-11-13T10:00:00Z",
                "meta": {},
            },
            {
                "component": "worker",
                "worker_id": "worker-target",
                "state": "hard_stop",
                "last_heartbeat_at": "2026-11-13T10:30:00Z",
                "meta": {},
            },
        ]

        class Client:
            def _table_get(self, _table: str, *, params: dict) -> list[dict]:  # noqa: SLF001
                return rows

        picked = health._fetch_worker_db_heartbeat(Client(), worker_id="worker-target")  # type: ignore[arg-type]
        self.assertEqual(picked["state"], "hard_stop")

    def test_unconfigured_one_nonblank_and_blank_worker_id_fails_closed(self) -> None:
        from scripts import report_ebay_pricing_health as health

        identified = {
            "component": "worker",
            "worker_id": "worker-a",
            "state": "idle",
            "last_heartbeat_at": "2026-11-13T12:00:00Z",
            "meta": {},
        }
        blank = {
            "component": "worker",
            "worker_id": "",
            "state": "idle",
            "last_heartbeat_at": "2026-11-13T12:05:00Z",
            "meta": {},
        }

        class Client:
            def _table_get(self, _table: str, *, params: dict) -> list[dict]:  # noqa: SLF001
                return [identified, blank]

        with mock.patch.dict(os.environ, {"MARKET_WORKER_ID": ""}, clear=False):
            picked = health._fetch_worker_db_heartbeat(Client())  # type: ignore[arg-type]
        self.assertIsNone(picked)

    def test_unconfigured_one_nonblank_and_null_worker_id_fails_closed(self) -> None:
        from scripts import report_ebay_pricing_health as health

        identified = {
            "component": "worker",
            "worker_id": "worker-a",
            "state": "idle",
            "last_heartbeat_at": "2026-11-13T12:00:00Z",
            "meta": {},
        }
        null_row = {
            "component": "worker",
            "worker_id": None,
            "state": "idle",
            "last_heartbeat_at": "2026-11-13T12:05:00Z",
            "meta": {},
        }

        class Client:
            def _table_get(self, _table: str, *, params: dict) -> list[dict]:  # noqa: SLF001
                return [identified, null_row]

        with mock.patch.dict(os.environ, {"MARKET_WORKER_ID": ""}, clear=False):
            picked = health._fetch_worker_db_heartbeat(Client())  # type: ignore[arg-type]
        self.assertIsNone(picked)

    def test_unconfigured_whitespace_only_worker_id_treated_as_blank(self) -> None:
        from scripts import report_ebay_pricing_health as health

        identified = {
            "component": "worker",
            "worker_id": "worker-a",
            "state": "idle",
            "last_heartbeat_at": "2026-11-13T12:00:00Z",
            "meta": {},
        }
        whitespace = {
            "component": "worker",
            "worker_id": "   ",
            "state": "idle",
            "last_heartbeat_at": "2026-11-13T12:05:00Z",
            "meta": {},
        }

        class Client:
            def _table_get(self, _table: str, *, params: dict) -> list[dict]:  # noqa: SLF001
                return [identified, whitespace]

        with mock.patch.dict(os.environ, {"MARKET_WORKER_ID": ""}, clear=False):
            picked = health._fetch_worker_db_heartbeat(Client())  # type: ignore[arg-type]
        self.assertIsNone(picked)

    def test_configured_worker_id_ignores_blank_rows(self) -> None:
        from scripts import report_ebay_pricing_health as health

        target = {
            "component": "worker",
            "worker_id": "worker-target",
            "state": "idle",
            "last_heartbeat_at": "2026-11-13T10:00:00Z",
            "meta": {},
        }
        blank_newer = {
            "component": "worker",
            "worker_id": "",
            "state": "hard_stop",
            "last_heartbeat_at": "2026-11-13T10:30:00Z",
            "meta": {},
        }

        class Client:
            def _table_get(self, _table: str, *, params: dict) -> list[dict]:  # noqa: SLF001
                if params.get("worker_id") == "eq.worker-target":
                    return [target]
                return [target, blank_newer]

        picked = health._fetch_worker_db_heartbeat(Client(), worker_id="worker-target")  # type: ignore[arg-type]
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["worker_id"], "worker-target")

    def test_unconfigured_all_blank_worker_ids_fails_closed(self) -> None:
        from scripts import report_ebay_pricing_health as health

        rows = [
            {
                "component": "worker",
                "worker_id": "",
                "state": "idle",
                "last_heartbeat_at": "2026-11-13T10:00:00Z",
                "meta": {},
            },
            {
                "component": "worker",
                "worker_id": None,
                "state": "idle",
                "last_heartbeat_at": "2026-11-13T10:30:00Z",
                "meta": {},
            },
        ]

        class Client:
            def _table_get(self, _table: str, *, params: dict) -> list[dict]:  # noqa: SLF001
                return rows

        with mock.patch.dict(os.environ, {"MARKET_WORKER_ID": ""}, clear=False):
            picked = health._fetch_worker_db_heartbeat(Client())  # type: ignore[arg-type]
        self.assertIsNone(picked)

    def test_unconfigured_multi_worker_health_not_healthy_despite_newest_idle(self) -> None:
        from scripts import report_ebay_pricing_health as health

        class MultiWorkerClient:
            def _table_get(self, _table: str, *, params: dict) -> list[dict]:  # noqa: SLF001
                return [
                    {
                        "component": "worker",
                        "worker_id": "worker-a",
                        "state": "idle",
                        "last_heartbeat_at": "2026-11-13T12:00:00Z",
                        "meta": {},
                    },
                    {
                        "component": "worker",
                        "worker_id": "worker-b",
                        "state": "idle",
                        "last_heartbeat_at": "2026-11-13T12:05:00Z",
                        "meta": {},
                    },
                ]

        now = datetime(2026, 11, 13, 12, 6, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            ledger = Path(tmp) / "ledger.json"
            fresh_ts = "2026-11-13T12:05:30Z"
            status.write_text(
                json.dumps({"workerState": "SELECTING", "updatedAtUtc": fresh_ts}),
                encoding="utf-8",
            )
            ledger.write_text(json.dumps({"lastHardStop": None, "updatedAtUtc": fresh_ts}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"MARKET_WORKER_ID": ""}, clear=False):
                with mock.patch.object(health, "STATUS_PATH", status):
                    with mock.patch.object(health, "BUDGET_LEDGER_PATH", ledger):
                        with mock.patch.object(health, "_count_python_workers", return_value=[90001]):
                            report = health.build_health_report(
                                client=MultiWorkerClient(),  # type: ignore[arg-type]
                                now=now,
                            )
        self.assertFalse(report["worker"]["healthy"])
        self.assertTrue(report["worker"]["dbHeartbeat"]["stale"])


class HealthReportDbHeartbeatFutureTests(unittest.TestCase):
    def test_db_heartbeat_future_beyond_tolerance_is_stale(self) -> None:
        from scripts import report_ebay_pricing_health as health

        class MinimalClient:
            def _table_get(self, *_args, **_kwargs):  # noqa: SLF001
                return []

        now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
        for future_minutes in (5, 180):
            future_ts = (now + timedelta(minutes=future_minutes)).isoformat().replace("+00:00", "Z")
            db_hb = {
                "component": "worker",
                "worker_id": "worker-only",
                "state": "idle",
                "last_heartbeat_at": future_ts,
                "meta": {},
            }
            with self.subTest(future_minutes=future_minutes):
                with tempfile.TemporaryDirectory() as tmp:
                    status = Path(tmp) / "status.json"
                    ledger = Path(tmp) / "ledger.json"
                    fresh_ts = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
                    status.write_text(
                        json.dumps({"workerState": "SELECTING", "updatedAtUtc": fresh_ts}),
                        encoding="utf-8",
                    )
                    ledger.write_text(json.dumps({"lastHardStop": None, "updatedAtUtc": fresh_ts}), encoding="utf-8")
                    with mock.patch.object(health, "STATUS_PATH", status):
                        with mock.patch.object(health, "BUDGET_LEDGER_PATH", ledger):
                            with mock.patch.object(health, "_fetch_worker_db_heartbeat", return_value=db_hb):
                                with mock.patch.object(health, "_count_python_workers", return_value=[12345]):
                                    report = health.build_health_report(
                                        client=MinimalClient(),  # type: ignore[arg-type]
                                        now=now,
                                    )
                self.assertTrue(report["worker"]["dbHeartbeat"]["stale"])
                self.assertFalse(report["worker"]["healthy"])

    def test_db_heartbeat_future_within_tolerance_is_fresh(self) -> None:
        from scripts import report_ebay_pricing_health as health

        class MinimalClient:
            def _table_get(self, *_args, **_kwargs):  # noqa: SLF001
                return []

        now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
        future_ts = (now + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
        db_hb = {
            "component": "worker",
            "worker_id": "worker-only",
            "state": "idle",
            "last_heartbeat_at": future_ts,
            "meta": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            ledger = Path(tmp) / "ledger.json"
            fresh_ts = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
            status.write_text(
                json.dumps({"workerState": "SELECTING", "updatedAtUtc": fresh_ts}),
                encoding="utf-8",
            )
            ledger.write_text(json.dumps({"lastHardStop": None, "updatedAtUtc": fresh_ts}), encoding="utf-8")
            with mock.patch.object(health, "STATUS_PATH", status):
                with mock.patch.object(health, "BUDGET_LEDGER_PATH", ledger):
                    with mock.patch.object(health, "_fetch_worker_db_heartbeat", return_value=db_hb):
                        with mock.patch.object(health, "_count_python_workers", return_value=[54321]):
                            report = health.build_health_report(
                                client=MinimalClient(),  # type: ignore[arg-type]
                                now=now,
                            )
        self.assertFalse(report["worker"]["dbHeartbeat"]["stale"])
        self.assertTrue(report["worker"]["healthy"])


class ContinuousLedgerRefreshTests(unittest.TestCase):
    def test_continuous_idle_cycle_persists_ledger_timestamp(self) -> None:
        gate = {
            "workerState": "SELECTING",
            "hardStop": None,
            "cooldownUntil": None,
            "nextProbeAt": None,
            "activeChallenges": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            client = FakeHeartbeatClient()
            runner = FakeRunner(client=client, outcomes=[[]])
            ledger_path = root / "continuous_au_safety_ledger.json"
            status_path = root / "continuous_au_worker_status.json"
            stale = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
            ledger_path.write_text(
                json.dumps(
                    {
                        "submissions": [],
                        "transients": [],
                        "consecutiveTransient": 0,
                        "lastHardStop": None,
                        "updatedAtUtc": stale,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch(
                "cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH",
                ledger_path,
            ):
                with mock.patch(
                    "cardscanr_market_engine.continuous_au_status.STATUS_PATH",
                    status_path,
                ):
                    with mock.patch.object(worker, "owned_daily_full_enable", return_value=True):
                        with mock.patch.object(worker, "classify_continuous_gate", return_value=gate):
                            with mock.patch.object(worker, "write_owned_daily_flag"):
                                with mock.patch.object(worker, "append_daily_metrics"):
                                    with mock.patch.object(worker, "write_json"):
                                        with mock.patch.object(worker, "append_jsonl"):
                                            budget = _clean_budget()
                                            with mock.patch.object(
                                                worker.ContinuousSafetyBudget, "from_env", return_value=budget
                                            ):
                                                worker.run_worker_loop(
                                                    args=fake_args(once=True),
                                                    config=config,  # type: ignore[arg-type]
                                                    runner=runner,  # type: ignore[arg-type]
                                                    poll_seconds=5,
                                                    max_jobs=1,
                                                    sleep_func=lambda _s: None,
                                                    logger=lambda *_a, **_k: None,
                                                )
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
            refreshed = parse_maybe_utc(data.get("updatedAtUtc"))
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            age_min = (datetime.now(timezone.utc) - refreshed).total_seconds() / 60.0
            self.assertLess(age_min, 2.0)


def parse_maybe_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


class HealthReportIdleLedgerTests(unittest.TestCase):
    def test_healthy_idle_over_ten_minutes_with_refreshed_ledger(self) -> None:
        from scripts import report_ebay_pricing_health as health

        class MinimalClient:
            def _table_get(self, *_args, **_kwargs):  # noqa: SLF001
                return []

        t0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=timezone.utc)
        now = t0 + timedelta(minutes=15)
        fresh_ts = (t0 + timedelta(minutes=14)).isoformat().replace("+00:00", "Z")
        stale_ts = (t0 - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")

        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            ledger_fresh = Path(tmp) / "ledger_fresh.json"
            ledger_stale = Path(tmp) / "ledger_stale.json"
            status.write_text(json.dumps({"workerState": "SELECTING", "updatedAtUtc": fresh_ts}), encoding="utf-8")
            ledger_fresh.write_text(json.dumps({"lastHardStop": None, "updatedAtUtc": fresh_ts}), encoding="utf-8")
            ledger_stale.write_text(json.dumps({"lastHardStop": None, "updatedAtUtc": stale_ts}), encoding="utf-8")

            db_hb = {
                "component": "worker",
                "state": "idle",
                "last_heartbeat_at": fresh_ts,
                "meta": {},
            }
            with mock.patch.object(health, "STATUS_PATH", status):
                with mock.patch.object(health, "BUDGET_LEDGER_PATH", ledger_fresh):
                    with mock.patch.object(health, "_fetch_worker_db_heartbeat", return_value=db_hb):
                        with mock.patch.object(health, "_count_python_workers", return_value=[424242]):
                            ok = health.build_health_report(
                                client=MinimalClient(),  # type: ignore[arg-type]
                                now=now,
                            )
            self.assertTrue(ok["worker"]["healthy"])

            with mock.patch.object(health, "STATUS_PATH", status):
                with mock.patch.object(health, "BUDGET_LEDGER_PATH", ledger_stale):
                    with mock.patch.object(health, "_fetch_worker_db_heartbeat", return_value=db_hb):
                        with mock.patch.object(health, "_count_python_workers", return_value=[424242]):
                            bad = health.build_health_report(
                                client=MinimalClient(),  # type: ignore[arg-type]
                                now=now,
                            )
            self.assertFalse(bad["worker"]["healthy"])
            self.assertEqual(bad["worker"]["runtimeEvidence"]["ledger"], "stale")


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
            _write_fresh_runtime_pair(
                status_path=status,
                ledger_path=ledger,
                worker_state="HARD_STOP",
                last_hard_stop="MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
                ledger_hard_stop="MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
            )
            with mock.patch.object(health, "STATUS_PATH", status):
                with mock.patch.object(health, "BUDGET_LEDGER_PATH", ledger):
                    with mock.patch.object(
                        health,
                        "_fetch_worker_db_heartbeat",
                        return_value=_fresh_db_worker_heartbeat(),
                    ):
                        with mock.patch.object(health, "_count_python_workers", return_value=[11111]):
                            report = health.build_health_report(client=MinimalClient())  # type: ignore[arg-type]
            worker_row = report["worker"]
            self.assertFalse(worker_row["healthy"])
            self.assertFalse(report["pricingWorkingNow"])
            self.assertEqual(worker_row["gateState"], "HARD_STOP")
            self.assertEqual(
                worker_row["gateReason"],
                "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
            )
            self.assertIn("awaiting owner review", worker_row["note"])

    def test_budget_wait_and_cooldown_mark_worker_unhealthy(self) -> None:
        from scripts import report_ebay_pricing_health as health

        class MinimalClient:
            def _table_get(self, *_args, **_kwargs):  # noqa: SLF001
                return []

        for worker_state, reason_field in (
            ("BUDGET_WAIT", "HOURLY_SUBMISSION_CEILING"),
            ("COOLDOWN", "2099-01-01T00:00:00Z"),
        ):
            with self.subTest(worker_state=worker_state):
                with tempfile.TemporaryDirectory() as tmp:
                    status = Path(tmp) / "status.json"
                    ledger = Path(tmp) / "ledger.json"
                    extra = {"cooldownUntil": reason_field} if worker_state == "COOLDOWN" else {}
                    now = utc_iso()
                    status.write_text(
                        json.dumps({"workerState": worker_state, "updatedAtUtc": now, **extra}),
                        encoding="utf-8",
                    )
                    ledger.write_text(json.dumps({"lastHardStop": None, "updatedAtUtc": now}), encoding="utf-8")
                    with health_report_runtime_mocks(health, status=status, ledger=ledger, seed=False):
                        with mock.patch.object(health, "_count_python_workers", return_value=[33333]):
                            report = health.build_health_report(client=MinimalClient())  # type: ignore[arg-type]
                    self.assertFalse(report["worker"]["healthy"])
                    self.assertFalse(report["pricingWorkingNow"])
                    self.assertEqual(report["worker"]["gateState"], worker_state)

    def test_hard_stop_cleared_in_ledger_not_reported(self) -> None:
        from scripts import report_ebay_pricing_health as health

        class MinimalClient:
            def _table_get(self, *_args, **_kwargs):  # noqa: SLF001
                return []

        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            ledger = Path(tmp) / "ledger.json"
            now = utc_iso()
            status.write_text(
                json.dumps(
                    {
                        "workerState": "HARD_STOP",
                        "lastHardStopReason": "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
                        "updatedAtUtc": now,
                    }
                ),
                encoding="utf-8",
            )
            ledger.write_text(
                json.dumps({"lastHardStop": None, "updatedAtUtc": now}),
                encoding="utf-8",
            )
            with mock.patch.object(health, "STATUS_PATH", status):
                with mock.patch.object(health, "BUDGET_LEDGER_PATH", ledger):
                    with mock.patch.object(
                        health,
                        "_fetch_worker_db_heartbeat",
                        return_value=_fresh_db_worker_heartbeat(),
                    ):
                        with mock.patch.object(health, "_count_python_workers", return_value=[44444]):
                            report = health.build_health_report(client=MinimalClient())  # type: ignore[arg-type]
            self.assertNotEqual(report["worker"]["gateState"], "HARD_STOP")

    def test_cleared_latch_does_not_report_selecting_without_fresh_runtime(self) -> None:
        from scripts import report_ebay_pricing_health as health

        class MinimalClient:
            def _table_get(self, *_args, **_kwargs):  # noqa: SLF001
                return []

        stale = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat().replace("+00:00", "Z")
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            ledger = Path(tmp) / "ledger.json"
            status.write_text(
                json.dumps(
                    {
                        "workerState": "HARD_STOP",
                        "lastHardStopReason": "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
                        "updatedAtUtc": stale,
                    }
                ),
                encoding="utf-8",
            )
            ledger.write_text(json.dumps({"lastHardStop": None, "updatedAtUtc": stale}), encoding="utf-8")
            stale_db_hb = {
                "component": "worker",
                "state": "idle",
                "last_heartbeat_at": stale,
                "meta": {},
            }
            with mock.patch.object(health, "STATUS_PATH", status):
                with mock.patch.object(health, "BUDGET_LEDGER_PATH", ledger):
                    with mock.patch.object(
                        health,
                        "_fetch_worker_db_heartbeat",
                        return_value=stale_db_hb,
                    ):
                        with mock.patch.object(health, "_count_python_workers", return_value=[66666]):
                            report = health.build_health_report(client=MinimalClient())  # type: ignore[arg-type]
        self.assertNotEqual(report["worker"]["gateState"], "SELECTING")
        self.assertFalse(report["worker"]["healthy"])
        self.assertEqual(report["worker"]["gateReason"], "STALE_RUNTIME_AFTER_LATCH_CLEAR")

    def test_stale_hard_stop_promoted_to_selecting_with_fresh_db_still_not_healthy(self) -> None:
        from scripts import report_ebay_pricing_health as health

        class MinimalClient:
            def _table_get(self, *_args, **_kwargs):  # noqa: SLF001
                return []

        stale = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat().replace("+00:00", "Z")
        fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            ledger = Path(tmp) / "ledger.json"
            status.write_text(
                json.dumps(
                    {
                        "workerState": "HARD_STOP",
                        "lastHardStopReason": "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
                        "updatedAtUtc": stale,
                    }
                ),
                encoding="utf-8",
            )
            ledger.write_text(json.dumps({"lastHardStop": None, "updatedAtUtc": fresh}), encoding="utf-8")
            fresh_db_hb = {
                "component": "worker",
                "state": "idle",
                "last_heartbeat_at": fresh,
                "meta": {},
            }
            with mock.patch.object(health, "STATUS_PATH", status):
                with mock.patch.object(health, "BUDGET_LEDGER_PATH", ledger):
                    with mock.patch.object(
                        health,
                        "_fetch_worker_db_heartbeat",
                        return_value=fresh_db_hb,
                    ):
                        with mock.patch.object(health, "_count_python_workers", return_value=[77777]):
                            report = health.build_health_report(
                                client=MinimalClient(),  # type: ignore[arg-type]
                                now=now,
                            )
        self.assertEqual(report["worker"]["gateState"], "SELECTING")
        self.assertFalse(report["worker"]["healthy"])
        self.assertEqual(report["worker"]["runtimeEvidence"]["status"], "stale")

    def test_stale_runtime_files_with_live_pid_not_healthy(self) -> None:
        from scripts import report_ebay_pricing_health as health

        class MinimalClient:
            def _table_get(self, *_args, **_kwargs):  # noqa: SLF001
                return []

        stale = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat().replace("+00:00", "Z")
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            ledger = Path(tmp) / "ledger.json"
            status.write_text(
                json.dumps({"workerState": "SELECTING", "updatedAtUtc": stale}),
                encoding="utf-8",
            )
            ledger.write_text(json.dumps({"updatedAtUtc": stale}), encoding="utf-8")
            with mock.patch.object(health, "STATUS_PATH", status):
                with mock.patch.object(health, "BUDGET_LEDGER_PATH", ledger):
                    with mock.patch.object(
                        health,
                        "_fetch_worker_db_heartbeat",
                        return_value=_fresh_db_worker_heartbeat(),
                    ):
                        with mock.patch.object(health, "_count_python_workers", return_value=[55555]):
                            report = health.build_health_report(client=MinimalClient())  # type: ignore[arg-type]
            self.assertFalse(report["worker"]["healthy"])
            self.assertEqual(report["worker"]["runtimeEvidence"]["status"], "stale")

    def test_missing_runtime_files_with_live_pid_not_healthy(self) -> None:
        from scripts import report_ebay_pricing_health as health

        class MinimalClient:
            def _table_get(self, *_args, **_kwargs):  # noqa: SLF001
                return []

        with tempfile.TemporaryDirectory() as tmp:
            missing_status = Path(tmp) / "missing_status.json"
            missing_ledger = Path(tmp) / "missing_ledger.json"
            with mock.patch.object(health, "STATUS_PATH", missing_status):
                with mock.patch.object(health, "BUDGET_LEDGER_PATH", missing_ledger):
                    with mock.patch.object(
                        health,
                        "_fetch_worker_db_heartbeat",
                        return_value=_fresh_db_worker_heartbeat(),
                    ):
                        with mock.patch.object(health, "_count_python_workers", return_value=[22222]):
                            report = health.build_health_report(client=MinimalClient())  # type: ignore[arg-type]
            self.assertFalse(report["worker"]["healthy"])
            self.assertEqual(report["worker"]["runtimeEvidence"]["status"], "missing")
            self.assertEqual(report["worker"]["runtimeEvidence"]["ledger"], "missing")


if __name__ == "__main__":
    unittest.main()
