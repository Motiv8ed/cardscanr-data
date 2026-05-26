"""
Phase 4B — Unit tests for the local market price engine runner.

All tests are cloud-safe and do not require real Supabase credentials.
Scheduler and worker adapters are injected via factory arguments.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.scheduler import MarketSchedulerConfig
from workers.market_price_engine_local import (
    _assert_mock_safe,
    _redact_env_summary,
    run_local_engine,
)


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

def _tmp_reports_dir(tmp_path: Path) -> Path:
    d = tmp_path / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fixed_scheduler_config() -> MarketSchedulerConfig:
    return MarketSchedulerConfig(
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="secret",
        max_keys_per_run=10,
        max_enqueues_per_run=5,
        include_missing_cache=True,
        include_stale_cache=True,
        min_popularity_score=0,
        min_inventory_count=0,
        dry_run=False,
        poll_seconds=300,
        latest_report_path=ROOT / "reports" / "market_price_scheduler_latest.json",
        runs_report_path=ROOT / "reports" / "market_price_scheduler_runs.jsonl",
    )


class FakeScheduler:
    """Stub scheduler that records calls and returns configurable reports."""

    def __init__(self, *, enqueued: int = 2, fail: bool = False) -> None:
        self.call_count = 0
        self._enqueued = enqueued
        self._fail = fail

    def run_and_write_reports(self) -> dict[str, Any]:
        self.call_count += 1
        if self._fail:
            raise RuntimeError("scheduler boom")
        return {
            "status": "success",
            "startedAtUtc": "2026-05-26T00:00:00Z",
            "finishedAtUtc": "2026-05-26T00:00:01Z",
            "dryRun": False,
            "summary": {
                "candidatesScanned": 4,
                "jobsEnqueued": self._enqueued,
                "jobsDryRunOnly": 0,
                "jobsSkippedAlreadyActive": 0,
                "jobsSkippedByLimit": 0,
                "jobsSkippedFresh": 0,
            },
        }


class FakeWorkerRunner:
    """Stub worker runner that records calls and returns configurable results."""

    def __init__(self, *, jobs: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self.call_count = 0
        self._jobs = [
            {"jobId": "j1", "status": "completed"},
            {"jobId": "j2", "status": "completed"},
        ] if jobs is None else jobs
        self._fail = fail

    def run_once(self, *, max_jobs: int | None = None) -> list[dict[str, Any]]:
        self.call_count += 1
        if self._fail:
            raise RuntimeError("worker boom")
        return list(self._jobs)


def _make_scheduler_factory(fake_scheduler: FakeScheduler):
    def _factory(config):
        return fake_scheduler
    return _factory


def _make_worker_factory(fake_runner: FakeWorkerRunner, max_jobs: int = 5):
    def _factory():
        return fake_runner, None, max_jobs
    return _factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TmpDirMixin(unittest.TestCase):
    """Mixin providing a per-test temporary directory."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.reports_dir = Path(self._tmpdir.name) / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()


class TestOneCycleOrchestration(TmpDirMixin):
    """One-cycle run calls scheduler then worker."""

    def test_one_cycle_calls_scheduler_then_worker(self) -> None:
        fake_scheduler = FakeScheduler(enqueued=3)
        fake_runner = FakeWorkerRunner()

        report = run_local_engine(
            cycles=1,
            poll_seconds=0,
            scheduler_max_keys=10,
            scheduler_max_enqueues=5,
            worker_max_jobs=5,
            dry_run=False,
            reports_dir=self.reports_dir,
            scheduler_factory=_make_scheduler_factory(fake_scheduler),
            worker_factory=_make_worker_factory(fake_runner),
        )

        self.assertEqual(fake_scheduler.call_count, 1)
        self.assertEqual(fake_runner.call_count, 1)
        self.assertEqual(report["cycles_completed"], 1)
        self.assertEqual(report["cycles_requested"], 1)
        self.assertEqual(report["total_jobs_enqueued"], 3)
        self.assertEqual(report["total_jobs_completed"], 2)
        self.assertEqual(report["errors"], [])

    def test_reports_are_written(self) -> None:
        fake_scheduler = FakeScheduler()
        fake_runner = FakeWorkerRunner()

        run_local_engine(
            cycles=1,
            poll_seconds=0,
            reports_dir=self.reports_dir,
            scheduler_factory=_make_scheduler_factory(fake_scheduler),
            worker_factory=_make_worker_factory(fake_runner),
        )

        latest = self.reports_dir / "market_price_engine_local_latest.json"
        runs = self.reports_dir / "market_price_engine_local_runs.jsonl"
        self.assertTrue(latest.exists(), "latest.json not written")
        self.assertTrue(runs.exists(), "runs.jsonl not written")
        payload = json.loads(latest.read_text(encoding="utf-8"))
        self.assertEqual(payload["cycles_completed"], 1)


class TestDryRun(TmpDirMixin):
    """Dry-run must not process worker jobs."""

    def test_dry_run_skips_worker_processing(self) -> None:
        fake_scheduler = FakeScheduler()
        fake_runner = FakeWorkerRunner()

        report = run_local_engine(
            cycles=1,
            poll_seconds=0,
            dry_run=True,
            reports_dir=self.reports_dir,
            scheduler_factory=_make_scheduler_factory(fake_scheduler),
            worker_factory=_make_worker_factory(fake_runner),
        )

        # Worker runner.run_once() must NOT be called in dry-run mode
        self.assertEqual(fake_runner.call_count, 0)
        self.assertEqual(report["total_jobs_processed"], 0)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["cycles_completed"], 1)

    def test_dry_run_flag_propagated_in_report(self) -> None:
        fake_scheduler = FakeScheduler()
        fake_runner = FakeWorkerRunner()

        run_local_engine(
            cycles=1,
            dry_run=True,
            reports_dir=self.reports_dir,
            scheduler_factory=_make_scheduler_factory(fake_scheduler),
            worker_factory=_make_worker_factory(fake_runner),
        )

        latest = self.reports_dir / "market_price_engine_local_latest.json"
        payload = json.loads(latest.read_text(encoding="utf-8"))
        self.assertTrue(payload["dry_run"])
        worker_sum = payload["worker_summaries"]
        self.assertEqual(len(worker_sum), 1)
        self.assertEqual(worker_sum[0].get("status"), "dry_run_skipped")


class TestMultipleCycles(TmpDirMixin):
    """Multiple cycles aggregate summaries correctly."""

    def test_three_cycles_aggregate_totals(self) -> None:
        fake_scheduler = FakeScheduler(enqueued=2)
        fake_runner = FakeWorkerRunner(jobs=[
            {"jobId": "j1", "status": "completed"},
            {"jobId": "j2", "status": "failed"},
        ])

        report = run_local_engine(
            cycles=3,
            poll_seconds=0,
            reports_dir=self.reports_dir,
            scheduler_factory=_make_scheduler_factory(fake_scheduler),
            worker_factory=_make_worker_factory(fake_runner),
        )

        self.assertEqual(fake_scheduler.call_count, 3)
        self.assertEqual(fake_runner.call_count, 3)
        self.assertEqual(report["cycles_completed"], 3)
        self.assertEqual(report["total_jobs_enqueued"], 6)   # 2 per cycle × 3
        self.assertEqual(report["total_jobs_processed"], 6)  # 2 per cycle × 3
        self.assertEqual(report["total_jobs_completed"], 3)  # 1 completed per cycle × 3
        self.assertEqual(report["total_jobs_failed"], 3)     # 1 failed per cycle × 3
        self.assertEqual(len(report["scheduler_summaries"]), 3)
        self.assertEqual(len(report["worker_summaries"]), 3)

    def test_runs_jsonl_accumulates_entries(self) -> None:
        fake_scheduler = FakeScheduler()
        fake_runner = FakeWorkerRunner()

        run_local_engine(
            cycles=2,
            poll_seconds=0,
            reports_dir=self.reports_dir,
            scheduler_factory=_make_scheduler_factory(fake_scheduler),
            worker_factory=_make_worker_factory(fake_runner),
        )
        run_local_engine(
            cycles=1,
            poll_seconds=0,
            reports_dir=self.reports_dir,
            scheduler_factory=_make_scheduler_factory(fake_scheduler),
            worker_factory=_make_worker_factory(fake_runner),
        )

        runs_path = self.reports_dir / "market_price_engine_local_runs.jsonl"
        lines = [line for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)  # Two separate run_local_engine invocations


class TestNoJobs(TmpDirMixin):
    """No-jobs case completes successfully with zero processed."""

    def test_no_jobs_is_successful(self) -> None:
        fake_scheduler = FakeScheduler(enqueued=0)
        fake_runner = FakeWorkerRunner(jobs=[])

        report = run_local_engine(
            cycles=1,
            poll_seconds=0,
            reports_dir=self.reports_dir,
            scheduler_factory=_make_scheduler_factory(fake_scheduler),
            worker_factory=_make_worker_factory(fake_runner),
        )

        self.assertEqual(report["cycles_completed"], 1)
        self.assertEqual(report["total_jobs_enqueued"], 0)
        self.assertEqual(report["total_jobs_processed"], 0)
        self.assertEqual(report["total_jobs_completed"], 0)
        self.assertEqual(report["total_jobs_failed"], 0)
        self.assertEqual(report["errors"], [])


class TestWorkerFailure(TmpDirMixin):
    """Worker failure appears in report and stops gracefully."""

    def test_worker_failure_appears_in_report(self) -> None:
        fake_scheduler = FakeScheduler(enqueued=1)
        fake_runner = FakeWorkerRunner(fail=True)

        report = run_local_engine(
            cycles=2,
            poll_seconds=0,
            reports_dir=self.reports_dir,
            scheduler_factory=_make_scheduler_factory(fake_scheduler),
            worker_factory=_make_worker_factory(fake_runner),
        )

        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("worker boom", report["errors"][0])
        self.assertEqual(report["cycles_completed"], 0)
        # Verify it writes the partial report
        latest = self.reports_dir / "market_price_engine_local_latest.json"
        self.assertTrue(latest.exists())

    def test_scheduler_failure_appears_in_report(self) -> None:
        fake_scheduler = FakeScheduler(fail=True)
        fake_runner = FakeWorkerRunner()

        report = run_local_engine(
            cycles=1,
            poll_seconds=0,
            reports_dir=self.reports_dir,
            scheduler_factory=_make_scheduler_factory(fake_scheduler),
            worker_factory=_make_worker_factory(fake_runner),
        )

        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("scheduler boom", report["errors"][0])
        self.assertEqual(report["cycles_completed"], 0)
        # Worker should NOT have been called if scheduler failed
        self.assertEqual(fake_runner.call_count, 0)


class TestReportRedactsSecrets(TmpDirMixin):
    """Report must redact sensitive values."""

    def test_report_redacts_secrets(self) -> None:
        fake_scheduler = FakeScheduler()
        fake_runner = FakeWorkerRunner()

        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "super-secret-key",
        }):
            run_local_engine(
                cycles=1,
                poll_seconds=0,
                reports_dir=self.reports_dir,
                scheduler_factory=_make_scheduler_factory(fake_scheduler),
                worker_factory=_make_worker_factory(fake_runner),
            )

        latest = self.reports_dir / "market_price_engine_local_latest.json"
        raw_text = latest.read_text(encoding="utf-8")
        self.assertNotIn("super-secret-key", raw_text)

    def test_env_summary_never_exposes_key_value(self) -> None:
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "super-secret-key",
        }):
            summary = _redact_env_summary()
        self.assertTrue(summary["supabase_url_present"])
        self.assertTrue(summary["supabase_service_role_key_present"])
        # Summary must only contain a boolean, never the actual value
        self.assertNotIn("super-secret-key", str(summary))
        self.assertNotIn("https://example.supabase.co", str(summary))


class TestMockProviderEnforcement(unittest.TestCase):
    """Mock-only provider enforcement."""

    def test_assert_mock_safe_passes_for_mock(self) -> None:
        with patch.dict(os.environ, {"MARKET_LOOKUP_PROVIDER": "mock"}):
            _assert_mock_safe()  # Should not raise

    def test_assert_mock_safe_passes_when_unset(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "MARKET_LOOKUP_PROVIDER"}
        with patch.dict(os.environ, env, clear=True):
            _assert_mock_safe()  # Should not raise

    def test_assert_mock_safe_fails_for_live_provider(self) -> None:
        with patch.dict(os.environ, {"MARKET_LOOKUP_PROVIDER": "ebay_live"}):
            with self.assertRaises(ValueError) as ctx:
                _assert_mock_safe()
        self.assertIn("mock", str(ctx.exception))
        self.assertIn("ebay_live", str(ctx.exception))


class TestMissingSupabaseEnv(TmpDirMixin):
    """Missing Supabase env is handled clearly — no crash in tests."""

    def test_missing_supabase_env_does_not_crash_dry_run(self) -> None:
        """Dry-run with missing Supabase env must complete without crashing."""
        fake_scheduler = FakeScheduler()
        fake_runner = FakeWorkerRunner()

        env = {k: v for k, v in os.environ.items()
               if k not in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")}
        with patch.dict(os.environ, env, clear=True):
            report = run_local_engine(
                cycles=1,
                dry_run=True,
                reports_dir=self.reports_dir,
                scheduler_factory=_make_scheduler_factory(fake_scheduler),
                worker_factory=_make_worker_factory(fake_runner),
            )

        self.assertFalse(report["supabase_env_present"])
        self.assertEqual(report["cycles_completed"], 1)

    def test_report_reflects_missing_supabase_env(self) -> None:
        fake_scheduler = FakeScheduler()
        fake_runner = FakeWorkerRunner()

        env = {k: v for k, v in os.environ.items()
               if k not in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")}
        with patch.dict(os.environ, env, clear=True):
            run_local_engine(
                cycles=1,
                dry_run=True,
                reports_dir=self.reports_dir,
                scheduler_factory=_make_scheduler_factory(fake_scheduler),
                worker_factory=_make_worker_factory(fake_runner),
            )

        latest = self.reports_dir / "market_price_engine_local_latest.json"
        payload = json.loads(latest.read_text(encoding="utf-8"))
        self.assertFalse(payload["supabase_env_present"])
        self.assertFalse(payload["env_summary"]["supabase_url_present"])


class TestMainCLI(TmpDirMixin):
    """CLI entry point smoke test."""

    def test_main_dry_run_exits_zero(self) -> None:
        from workers.market_price_engine_local import main

        fake_scheduler = FakeScheduler()
        fake_runner = FakeWorkerRunner()

        env = {k: v for k, v in os.environ.items()
               if k not in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")}
        env["MARKET_LOOKUP_PROVIDER"] = "mock"

        with patch.dict(os.environ, env, clear=True):
            with patch("workers.market_price_engine_local.run_local_engine") as mock_run:
                mock_run.return_value = {
                    "cycles_completed": 1,
                    "cycles_requested": 1,
                    "total_jobs_enqueued": 0,
                    "total_jobs_processed": 0,
                    "total_jobs_completed": 0,
                    "total_jobs_failed": 0,
                    "errors": [],
                }
                exit_code = main(["--dry-run", "--cycles", "1", "--reports-dir", str(self.reports_dir)])
        self.assertEqual(exit_code, 0)

    def test_main_live_provider_exits_one(self) -> None:
        from workers.market_price_engine_local import main

        with patch.dict(os.environ, {"MARKET_LOOKUP_PROVIDER": "ebay_live"}):
            exit_code = main(["--dry-run", "--cycles", "1"])
        self.assertEqual(exit_code, 1)

    def test_main_with_errors_exits_one(self) -> None:
        from workers.market_price_engine_local import main

        env = {k: v for k, v in os.environ.items()
               if k not in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")}
        env["MARKET_LOOKUP_PROVIDER"] = "mock"

        with patch.dict(os.environ, env, clear=True):
            with patch("workers.market_price_engine_local.run_local_engine") as mock_run:
                mock_run.return_value = {
                    "cycles_completed": 0,
                    "cycles_requested": 1,
                    "total_jobs_enqueued": 0,
                    "total_jobs_processed": 0,
                    "total_jobs_completed": 0,
                    "total_jobs_failed": 0,
                    "errors": ["cycle=1 scheduler error: boom"],
                }
                exit_code = main(["--dry-run", "--cycles", "1", "--reports-dir", str(self.reports_dir)])
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
