"""Worker loop + ledger accounting for 20/h and 60/24h caps (offline)."""

from __future__ import annotations

import contextlib
import multiprocessing as mp
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cardscanr_market_engine import continuous_safety
from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.live_ebay_navigation_accounting import (
    LIVE_EBAY_NAVIGATION_CONSUMED,
    LIVE_SUBMISSION_RESERVED,
    live_ebay_navigation_consumed_from_metadata,
    result_consumes_submission_cap,
    supplementary_submission_needs_cap,
)
from cardscanr_market_engine.pipeline_phase_diagnostics import build_provider_diagnostics_for_result
import workers.market_price_worker as worker

NOW = datetime(2026, 11, 19, 1, 0, tzinfo=timezone.utc)


def _fake_config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        provider_name="ebay_browser",
        worker_id="test-worker",
        latest_report_path=root / "latest.json",
        runs_report_path=root / "runs.jsonl",
    )


def _fake_args(*, once: bool = True) -> SimpleNamespace:
    return SimpleNamespace(once=once, max_cycles=0)


class _ProbeRunner:
    _ebay_probe_mode = False

    def __init__(self, *, reserved: bool = False) -> None:
        self.calls = 0
        self.client = SimpleNamespace()
        self._reserved = reserved

    def run_once(self, *, max_jobs: int | None = None) -> list[dict]:
        self.calls += 1
        row: dict = {
            "status": "completed",
            "ownedDailyOutcome": "UNCHANGED_FROM_EBAY",
            "marketCountry": "AU",
        }
        if self._reserved:
            row[LIVE_SUBMISSION_RESERVED] = True
            row["searchSubmissionStarted"] = True
        else:
            row[LIVE_EBAY_NAVIGATION_CONSUMED] = True
            row["searchSubmissionStarted"] = True
        return [row]


@contextlib.contextmanager
def _worker_isolated(
    *,
    budget: ContinuousSafetyBudget,
    ledger: Path,
    reload_budget_each_cycle: bool = False,
    utc_now: Any | None = None,
):
    with contextlib.ExitStack() as stack:
        if utc_now is not None:
            if callable(utc_now):
                stack.enter_context(
                    mock.patch("cardscanr_market_engine.continuous_safety.utc_now", side_effect=utc_now)
                )
            else:
                stack.enter_context(
                    mock.patch("cardscanr_market_engine.continuous_safety.utc_now", return_value=utc_now)
                )
        else:
            stack.enter_context(
                mock.patch("cardscanr_market_engine.continuous_safety.utc_now", return_value=NOW)
            )
        stack.enter_context(mock.patch.object(continuous_safety, "BUDGET_LEDGER_PATH", ledger))
        if reload_budget_each_cycle:
            _real_from_env = worker.ContinuousSafetyBudget.from_env

            def _from_env(*, path: Path | None = None) -> ContinuousSafetyBudget:
                return _real_from_env(path=path or ledger)

            stack.enter_context(mock.patch.object(worker.ContinuousSafetyBudget, "from_env", _from_env))
        else:
            stack.enter_context(
                mock.patch.object(worker.ContinuousSafetyBudget, "from_env", return_value=budget)
            )
        stack.enter_context(mock.patch.object(worker, "ensure_continuous_au_safety_env"))
        stack.enter_context(
            mock.patch(
                "cardscanr_market_engine.navigation_runtime_context.mint_nav_handoff_writer_run_id"
            )
        )
        stack.enter_context(mock.patch.object(worker, "owned_daily_full_enable", return_value=False))
        stack.enter_context(mock.patch.object(worker, "write_owned_daily_flag"))
        stack.enter_context(mock.patch.object(worker, "write_continuous_status"))
        stack.enter_context(mock.patch.object(worker, "append_daily_metrics"))
        stack.enter_context(mock.patch.object(worker, "write_json"))
        stack.enter_context(mock.patch.object(worker, "append_jsonl"))
        stack.enter_context(
            mock.patch.object(
                worker,
                "_should_classify_ebay_availability_gate",
                return_value=False,
            )
        )
        yield


class WorkerLoopCapTests(unittest.TestCase):
    def test_twenty_first_job_hour_blocked_without_run_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            anchor = NOW - timedelta(minutes=30)
            with mock.patch("cardscanr_market_engine.continuous_safety.utc_now", return_value=NOW):
                for i in range(20):
                    self.assertIsNone(
                        budget.reserve_live_submission(
                            path=ledger,
                            market="AU",
                            now=anchor + timedelta(seconds=30 * i),
                        )
                    )
            budget.reload(ledger)
            runner = _ProbeRunner()
            with _worker_isolated(budget=budget, ledger=ledger):
                code = worker.run_worker_loop(
                    args=_fake_args(once=True),
                    config=_fake_config(root),
                    runner=runner,  # type: ignore[arg-type]
                    poll_seconds=1,
                    max_jobs=1,
                    sleep_func=lambda _s: None,
                    logger=lambda *_a, **_k: None,
                )
            self.assertEqual(code, 0)
            self.assertEqual(runner.calls, 0)
            self.assertGreaterEqual(budget.submissions_1h(NOW), 20)

    def test_supplementary_nav_rows_increment_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            runner = _ProbeRunner(reserved=False)
            with _worker_isolated(budget=budget, ledger=ledger):
                for _ in range(3):
                    worker.run_worker_loop(
                        args=_fake_args(once=True),
                        config=_fake_config(root),
                        runner=runner,  # type: ignore[arg-type]
                        poll_seconds=1,
                        max_jobs=1,
                        sleep_func=lambda _s: None,
                        logger=lambda *_a, **_k: None,
                    )
            budget.reload(ledger)
            self.assertEqual(runner.calls, 3)
            self.assertEqual(budget.submissions_1h(NOW), 3)
            self.assertEqual(len(budget.submission_events), 3)

    def test_reserved_rows_do_not_double_count_in_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            runner = _ProbeRunner(reserved=True)
            with _worker_isolated(budget=budget, ledger=ledger):
                worker.run_worker_loop(
                    args=_fake_args(once=True),
                    config=_fake_config(root),
                    runner=runner,  # type: ignore[arg-type]
                    poll_seconds=1,
                    max_jobs=1,
                    sleep_func=lambda _s: None,
                    logger=lambda *_a, **_k: None,
                )
            budget.reload(ledger)
            self.assertEqual(budget.submissions_1h(NOW), 0)

    def test_probe_mode_supplementary_jobs_count_toward_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            runner = _ProbeRunner(reserved=False)
            with _worker_isolated(budget=budget, ledger=ledger):
                with mock.patch.dict(os.environ, {"CARDSCANR_OPERATION_MODE": "PROBE"}, clear=False):
                    worker.run_worker_loop(
                        args=_fake_args(once=True),
                        config=_fake_config(root),
                        runner=runner,  # type: ignore[arg-type]
                        poll_seconds=1,
                        max_jobs=1,
                        sleep_func=lambda _s: None,
                        logger=lambda *_a, **_k: None,
                    )
            budget.reload(ledger)
            self.assertEqual(budget.submissions_1h(NOW), 1)
            self.assertEqual(len(budget.submission_events), 1)

    def test_record_nothing_when_supplementary_disabled_fails_guard(self) -> None:
        """Guards against re-introducing parallel post-job counting for reserved jobs."""
        row = {LIVE_EBAY_NAVIGATION_CONSUMED: True, "marketCountry": "AU"}
        self.assertTrue(supplementary_submission_needs_cap(row))
        with mock.patch(
            "workers.market_price_worker.row_needs_supplementary_cap_slots",
            return_value=0,
        ):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ledger = root / "ledger.json"
                budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
                runner = _ProbeRunner(reserved=False)
                with _worker_isolated(budget=budget, ledger=ledger):
                    worker.run_worker_loop(
                        args=_fake_args(once=True),
                        config=_fake_config(root),
                        runner=runner,  # type: ignore[arg-type]
                        poll_seconds=1,
                        max_jobs=1,
                        sleep_func=lambda _s: None,
                        logger=lambda *_a, **_k: None,
                    )
                budget.reload(ledger)
                self.assertEqual(budget.submissions_1h(NOW), 0)


class ProviderRowShapeCapTests(unittest.TestCase):
    def test_failed_desktop_nav_row_from_provider_diagnostics(self) -> None:
        meta = {
            "desktopNav": {
                "searchSuccess": True,
                "soldClickSuccess": False,
                "submitted": True,
                "searchSubmissionStarted": True,
            },
            LIVE_EBAY_NAVIGATION_CONSUMED: True,
        }
        provider_diag = build_provider_diagnostics_for_result(raw_metadata=meta)
        row = {
            "status": "failed",
            "ownedDailyOutcome": "TEMPORARY_BROWSER_FAILURE",
            "providerDiagnostics": provider_diag,
        }
        row.update(
            {
                k: v
                for k, v in (provider_diag.get("diagnostics") or {}).items()
                if k in ("searchSubmissionStarted", LIVE_EBAY_NAVIGATION_CONSUMED)
            }
        )
        self.assertTrue(live_ebay_navigation_consumed_from_metadata(meta))
        self.assertTrue(supplementary_submission_needs_cap(row))

    def test_reserved_submission_not_double_counted(self) -> None:
        row = {"status": "completed", LIVE_SUBMISSION_RESERVED: True}
        self.assertFalse(result_consumes_submission_cap(row))

    def test_bare_completed_without_reserve_does_not_supplementary_count(self) -> None:
        row = {"status": "completed", "ownedDailyOutcome": "UNCHANGED_FROM_EBAY"}
        self.assertFalse(supplementary_submission_needs_cap(row))


def _concurrent_reserve_submission(path_str: str, ready: mp.Value, go: mp.Event) -> None:
    ready.value += 1
    go.wait(timeout=30)
    budget = ContinuousSafetyBudget(max_submissions_per_hour=999, max_submissions_per_day=999)
    budget.reserve_live_submission(market="AU", path=Path(path_str), now=NOW)


class LedgerConcurrencyTests(unittest.TestCase):
    def test_parallel_reserve_never_loses_writes(self) -> None:
        workers = 8
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            ready = mp.Value("i", 0)
            go = mp.Event()
            procs = [
                mp.Process(
                    target=_concurrent_reserve_submission,
                    args=(str(ledger), ready, go),
                )
                for _ in range(workers)
            ]
            for proc in procs:
                proc.start()
            deadline = time.monotonic() + 15
            while ready.value < workers and time.monotonic() < deadline:
                time.sleep(0.02)
            go.set()
            for proc in procs:
                proc.join(timeout=30)
            budget = ContinuousSafetyBudget(max_submissions_per_hour=999, max_submissions_per_day=999)
            budget.reload(ledger)
            self.assertEqual(len(budget.submission_events), workers)


class _AlternatingFailSuccessRunner:
    _ebay_probe_mode = False

    def __init__(self) -> None:
        self.calls = 0
        self.client = SimpleNamespace()

    def run_once(self, *, max_jobs: int | None = None) -> list[dict]:
        self.calls += 1
        if self.calls % 2 == 1:
            from cardscanr_market_engine.owned_daily_outcomes import TEMPORARY_EBAY_SERVER_FAILURE

            return [
                {
                    "status": "failed",
                    "ownedDailyOutcome": TEMPORARY_EBAY_SERVER_FAILURE,
                    "marketCountry": "AU",
                }
            ]
        return [
            {
                "status": "completed",
                "ownedDailyOutcome": "UNCHANGED_FROM_EBAY",
                "marketCountry": "AU",
            }
        ]


def _reload_budget_from_ledger(ledger: Path, *, now: datetime) -> ContinuousSafetyBudget:
    with mock.patch("cardscanr_market_engine.continuous_safety.utc_now", return_value=now):
        return ContinuousSafetyBudget.from_env(path=ledger)


def _run_fs_alternating_worker_cycles(
    *,
    root: Path,
    ledger: Path,
    cycle_times: list[datetime],
) -> list[int]:
    """Five worker cycles with F/S/F/S/F results; each call reloads budget from disk (owned_daily OFF)."""
    runner = _AlternatingFailSuccessRunner()
    exit_codes: list[int] = []
    budget_seed = ContinuousSafetyBudget(
        max_submissions_per_hour=20,
        max_submissions_per_day=60,
        max_consecutive_transient=3,
    )
    budget_seed.persist(ledger)
    clock = {"now": cycle_times[0]}

    def _utc_now() -> datetime:
        return clock["now"]

    with _worker_isolated(
        budget=budget_seed,
        ledger=ledger,
        reload_budget_each_cycle=True,
        utc_now=_utc_now,
    ):
        for when in cycle_times:
            clock["now"] = when
            code = worker.run_worker_loop(
                args=_fake_args(once=True),
                config=_fake_config(root),
                runner=runner,  # type: ignore[arg-type]
                poll_seconds=1,
                max_jobs=1,
                sleep_func=lambda _s: None,
                logger=lambda *_a, **_k: None,
            )
            exit_codes.append(code)
    return exit_codes


class WorkerTransientHealthyPersistTests(unittest.TestCase):
    def test_alternating_fail_success_does_not_false_latch_hard_stop(self) -> None:
        """owned_daily OFF: record_healthy() resets must hit disk or F/S stacks latch across cycles."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            base = NOW
            cycle_times = [base + timedelta(minutes=2 * i) for i in range(5)]
            exit_codes = _run_fs_alternating_worker_cycles(
                root=root,
                ledger=ledger,
                cycle_times=cycle_times,
            )
            self.assertEqual(exit_codes, [0, 0, 0, 0, 0], msg="no HARD_STOP exit mid F/S/F/S/F")
            final = _reload_budget_from_ledger(ledger, now=cycle_times[-1])
            self.assertIsNone(
                final.transient_hard_stop(cycle_times[-1]),
                msg="ledger must not false-latch after alternating F/S",
            )
            self.assertEqual(final.consecutive_transient, 1)
            self.assertIsNone(final.last_hard_stop)

class SupplementaryCapAtCeilingTests(unittest.TestCase):
    def test_supplementary_at_hourly_cap_does_not_add_slots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            with mock.patch("cardscanr_market_engine.continuous_safety.utc_now", return_value=NOW):
                for _ in range(20):
                    self.assertIsNone(budget.reserve_live_submission(path=ledger, market="AU", now=NOW))
            runner = _ProbeRunner(reserved=False)
            with _worker_isolated(budget=budget, ledger=ledger):
                worker.run_worker_loop(
                    args=_fake_args(once=True),
                    config=_fake_config(root),
                    runner=runner,  # type: ignore[arg-type]
                    poll_seconds=1,
                    max_jobs=1,
                    sleep_func=lambda _s: None,
                    logger=lambda *_a, **_k: None,
                )
            budget.reload(ledger)
            self.assertEqual(budget.submissions_1h(NOW), 20)
            self.assertEqual(len(budget.submission_events), 20)


class SubmissionEventMergeTests(unittest.TestCase):
    def test_identical_timestamp_submission_events_do_not_collapse(self) -> None:
        from cardscanr_market_engine.continuous_safety import SubmissionEvent, merge_budget_payloads

        disk = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
        local = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
        disk.submission_events.append(SubmissionEvent(id="evt-a", at=NOW, market="AU"))
        local.submission_events.append(SubmissionEvent(id="evt-b", at=NOW, market="AU"))
        merged = merge_budget_payloads(disk.to_payload(), local.to_payload())
        events = merged.get("submissionEvents") or []
        self.assertEqual(len(events), 2)
        ids = {str(row.get("id")) for row in events}
        self.assertEqual(ids, {"evt-a", "evt-b"})


class RunJobLedgerFetchParityTests(unittest.TestCase):
    def test_run_job_one_submission_per_fetch_completed(self) -> None:
        from cardscanr_market_engine.job_runner import MarketPriceJobRunner
        from cardscanr_market_engine.models import MarketPriceRefreshJob
        from cardscanr_market_engine.providers.errors import ProviderTemporaryError
        from tests.test_data63_reqa_r5_20261119 import _job_runner_patches

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
            runner._continuous_worker = False
            runner._enforce_safety_budget = True
            job = MarketPriceRefreshJob(
                id="job-ok",
                price_key_id="pk1",
                reason="stale",
                priority=1,
                status="running",
                attempt_count=1,
            )
            fetch_calls = 0

            def _fetch(*_a, **_k):
                nonlocal fetch_calls
                fetch_calls += 1
                raise ProviderTemporaryError("sim", diagnostics={"searchSubmissionStarted": True})

            with _job_runner_patches(runner, ledger):
                with mock.patch.object(runner, "fetch_fallback_result", side_effect=_fetch):
                    runner.run_job(job)
            budget.reload(ledger)
            self.assertEqual(fetch_calls, 1)
            self.assertEqual(len(budget.submission_events), 1)

    def test_run_job_one_submission_per_fetch_failed(self) -> None:
        from cardscanr_market_engine.job_runner import MarketPriceJobRunner
        from cardscanr_market_engine.models import MarketPriceRefreshJob
        from cardscanr_market_engine.providers.errors import ProviderError
        from tests.test_data63_reqa_r5_20261119 import _job_runner_patches

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
            runner._continuous_worker = False
            runner._enforce_safety_budget = True
            job = MarketPriceRefreshJob(
                id="job-fail",
                price_key_id="pk1",
                reason="stale",
                priority=1,
                status="running",
                attempt_count=1,
            )
            fetch_calls = 0

            def _fetch(*_a, **_k):
                nonlocal fetch_calls
                fetch_calls += 1
                raise ProviderError("sim")

            with _job_runner_patches(runner, ledger):
                with mock.patch.object(runner, "fetch_fallback_result", side_effect=_fetch):
                    runner.run_job(job)
            budget.reload(ledger)
            self.assertEqual(fetch_calls, 1)
            self.assertEqual(len(budget.submission_events), 1)


if __name__ == "__main__":
    unittest.main()