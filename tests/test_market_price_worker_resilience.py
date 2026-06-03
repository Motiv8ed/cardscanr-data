from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.supabase_client import SupabaseRpcError
import workers.market_price_worker as worker


class FakeResponse:
    def __init__(self, *, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class FakeRunner:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.max_jobs_seen: list[int] = []

    def run_once(self, *, max_jobs: int | None = None) -> list[dict]:
        self.calls += 1
        self.max_jobs_seen.append(int(max_jobs or 0))
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


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class MarketPriceWorkerResilienceTests(unittest.TestCase):
    def test_claim_connection_error_reports_backs_off_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            error = requests.exceptions.ConnectionError(
                "ConnectionResetError [WinError 10054] https://example.supabase.co/rest/v1/rpc/"
                "claim_market_price_refresh_jobs?apikey=service-role-secret Authorization: Bearer secret-token"
            )
            runner = FakeRunner([error, [{"jobId": "job-1", "status": "completed"}]])
            sleeps: list[int] = []

            result = worker.run_worker_loop(
                args=fake_args(max_cycles=2),
                config=config,  # type: ignore[arg-type]
                runner=runner,  # type: ignore[arg-type]
                poll_seconds=5,
                max_jobs=1,
                sleep_func=sleeps.append,
                logger=lambda *_args, **_kwargs: None,
            )

            self.assertEqual(result, 0)
            self.assertEqual(runner.calls, 2)
            self.assertEqual(sleeps, [10])
            reports = read_jsonl(config.runs_report_path)
            self.assertEqual(reports[0]["status"], "transient_error")
            self.assertEqual(reports[0]["error_type"], "ConnectionError")
            self.assertEqual(reports[0]["backoff_seconds"], 10)
            self.assertEqual(reports[0]["retry_after_seconds"], 10)
            self.assertNotIn("service-role-secret", reports[0]["error_message"])
            self.assertNotIn("secret-token", reports[0]["error_message"])
            self.assertEqual(reports[1]["jobCount"], 1)
            self.assertEqual(reports[1]["results"][0]["status"], "completed")

    def test_transient_timeout_latest_report_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            runner = FakeRunner([requests.exceptions.Timeout("claim rpc timed out")])

            result = worker.run_worker_loop(
                args=fake_args(max_cycles=1),
                config=config,  # type: ignore[arg-type]
                runner=runner,  # type: ignore[arg-type]
                poll_seconds=20,
                max_jobs=1,
                sleep_func=lambda _seconds: None,
                logger=lambda *_args, **_kwargs: None,
            )

            self.assertEqual(result, 0)
            latest = json.loads(config.latest_report_path.read_text(encoding="utf-8"))
            self.assertEqual(latest["status"], "transient_error")
            self.assertEqual(latest["provider"], "mock")
            self.assertEqual(latest["cycle"], 1)
            self.assertEqual(latest["jobCount"], 0)
            self.assertEqual(latest["error_type"], "Timeout")
            self.assertEqual(latest["error_message"], "claim rpc timed out")
            self.assertEqual(latest["backoff_seconds"], 20)

    def test_supabase_5xx_rpc_error_is_transient_but_4xx_is_not(self) -> None:
        transient = SupabaseRpcError(
            rpc_name="claim_market_price_refresh_jobs",
            response=FakeResponse(status_code=503, text="temporarily unavailable"),  # type: ignore[arg-type]
            payload={"p_worker_id": "worker-test", "p_max_jobs": 1},
        )
        permanent = SupabaseRpcError(
            rpc_name="claim_market_price_refresh_jobs",
            response=FakeResponse(status_code=404, text="missing rpc"),  # type: ignore[arg-type]
            payload={"p_worker_id": "worker-test", "p_max_jobs": 1},
        )

        self.assertTrue(worker.is_transient_worker_error(transient))
        self.assertFalse(worker.is_transient_worker_error(permanent))

    def test_backoff_starts_from_poll_seconds_and_caps_at_sixty(self) -> None:
        self.assertEqual(worker.next_transient_backoff_seconds(0, poll_seconds=5), 10)
        self.assertEqual(worker.next_transient_backoff_seconds(0, poll_seconds=30), 30)
        self.assertEqual(worker.next_transient_backoff_seconds(30, poll_seconds=5), 60)
        self.assertEqual(worker.next_transient_backoff_seconds(60, poll_seconds=5), 60)

    def test_job_processing_failed_result_is_reported_without_transient_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            failed_result = [{"jobId": "job-2", "status": "failed", "error": "provider boom"}]
            runner = FakeRunner([failed_result])
            sleeps: list[int] = []

            result = worker.run_worker_loop(
                args=fake_args(once=True),
                config=config,  # type: ignore[arg-type]
                runner=runner,  # type: ignore[arg-type]
                poll_seconds=5,
                max_jobs=1,
                sleep_func=sleeps.append,
                logger=lambda *_args, **_kwargs: None,
            )

            self.assertEqual(result, 0)
            self.assertEqual(sleeps, [])
            latest = json.loads(config.latest_report_path.read_text(encoding="utf-8"))
            self.assertNotIn("status", latest)
            self.assertEqual(latest["jobCount"], 1)
            self.assertEqual(latest["results"][0]["status"], "failed")
            self.assertEqual(latest["results"][0]["error"], "provider boom")

    def test_non_transient_coding_error_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = fake_config(root)
            runner = FakeRunner([KeyError("missing field")])

            with self.assertRaises(KeyError):
                worker.run_worker_loop(
                    args=fake_args(max_cycles=1),
                    config=config,  # type: ignore[arg-type]
                    runner=runner,  # type: ignore[arg-type]
                    poll_seconds=5,
                    max_jobs=1,
                    sleep_func=lambda _seconds: None,
                    logger=lambda *_args, **_kwargs: None,
                )

            self.assertFalse(config.latest_report_path.exists())


if __name__ == "__main__":
    unittest.main()
