"""Revert-proof: set-sweep shadow on live AU worker with owned_daily OFF (DATA-63)."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
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
from cardscanr_market_engine.owned_daily_outcomes import CHALLENGE_REQUIRED
from cardscanr_market_engine.ebay_scale.set_sweep_safety import (
    MARKETPLACE_GENERIC_ERROR_PAGE,
    handle_set_sweep_marketplace_outcome,
)
from cardscanr_market_engine.ebay_scale.set_sweep_worker_hook import (
    SET_SWEEP_LAST_RUN_PATH,
    SET_SWEEP_PLAN_PATH,
    SetSweepExecutionResult,
    live_au_set_sweep_shadow_hook_enabled,
    read_set_sweep_plan,
    should_attempt_set_sweep,
)
from cardscanr_market_engine.providers.errors import ProviderTemporaryError
import workers.market_price_worker as worker

# Revert guard: worker must reference live_au_set_sweep_shadow_hook_enabled on the staged path.
_WORKER_SRC = (ROOT / "workers" / "market_price_worker.py").read_text(encoding="utf-8")


def _fake_config(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        worker_id="worker-test",
        provider_name="ebay_browser",
        latest_report_path=root / "latest.json",
        runs_report_path=root / "runs.jsonl",
    )


class IdleQueueRunner:
    def __init__(self, *, results: list[dict] | None = None) -> None:
        self.client = mock.Mock()
        self._results = list(results or [])
        self._ebay_probe_mode = False

    def run_once(self, *, max_jobs: int | None = None) -> list[dict]:
        return list(self._results)


@contextlib.contextmanager
def _staged_au_env(*, owned_daily: bool = False):
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch.dict(
                os.environ,
                {
                    "EBAY_SCALE_ENABLED": "true",
                    "EBAY_SCALE_SET_SWEEP": "true",
                    "EBAY_SCALE_SHADOW_AUDIT": "true",
                    "EBAY_SCALE_SET_SWEEP_LIVE": "false",
                    "EBAY_SCALE_SET_SWEEP_EVERY_N_PER_CARD_JOBS": "1",
                    "CARDSCANR_OPERATION_MODE": "",
                },
                clear=False,
            )
        )
        stack.enter_context(mock.patch.object(worker, "owned_daily_full_enable", return_value=owned_daily))
        yield stack


class LiveAuSetSweepShadowWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._runtime_tmp = tempfile.TemporaryDirectory()
        self._last_run_path = Path(self._runtime_tmp.name) / "set_sweep_last_run.json"
        self._last_run_patch = mock.patch(
            "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_LAST_RUN_PATH",
            self._last_run_path,
        )
        self._last_run_patch.start()

    def tearDown(self) -> None:
        self._last_run_patch.stop()
        self._runtime_tmp.cleanup()

    def test_revert_guard_live_au_shadow_hook_wired_in_worker(self) -> None:
        self.assertIn("live_au_set_sweep_shadow_hook_enabled", _WORKER_SRC)
        self.assertIn("not continuous", _WORKER_SRC)
        self.assertIn("live_au_set_sweep_interleave_due", _WORKER_SRC)

    def test_shadow_hook_enabled_requires_live_off(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "EBAY_SCALE_ENABLED": "true",
                "EBAY_SCALE_SET_SWEEP": "true",
                "EBAY_SCALE_SHADOW_AUDIT": "true",
                "EBAY_SCALE_SET_SWEEP_LIVE": "true",
            },
            clear=False,
        ):
            self.assertFalse(live_au_set_sweep_shadow_hook_enabled())

    def test_hook_fires_with_owned_daily_off_idle_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            runner = IdleQueueRunner(results=[])
            calls: list[str] = []

            def _sweep(**_kwargs):
                calls.append("sweep")
                return SetSweepExecutionResult(ok=False, reason="no_plan")

            gate = {
                "workerState": "SELECTING",
                "hardStop": None,
                "availabilityState": "HEALTHY",
                "probeAllowed": False,
                "cooldownUntil": None,
                "nextProbeAt": None,
                "activeChallenges": 0,
            }
            with _staged_au_env(owned_daily=False):
                with (
                    mock.patch.object(worker.ContinuousSafetyBudget, "from_env", return_value=budget),
                    mock.patch.object(worker, "write_json"),
                    mock.patch.object(worker, "append_jsonl"),
                    mock.patch.object(worker, "append_daily_metrics"),
                    mock.patch.object(worker, "classify_continuous_gate", return_value=gate),
                    mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger),
                    mock.patch(
                        "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.try_set_sweep_worker_cycle",
                        side_effect=_sweep,
                    ),
                ):
                    worker.run_worker_loop(
                        args=SimpleNamespace(once=True, max_cycles=0),
                        config=_fake_config(root),  # type: ignore[arg-type]
                        runner=runner,  # type: ignore[arg-type]
                        poll_seconds=1,
                        max_jobs=3,
                        sleep_func=lambda _s: None,
                        logger=lambda *_a, **_k: None,
                    )
            self.assertEqual(calls, ["sweep"])

    def test_busy_queue_defers_sweep_until_n_per_card_jobs_multi_cycle(self) -> None:
        old_worker = subprocess.run(
            ["git", "show", "8b88b918:workers/market_price_worker.py"],
            check=True,
            capture_output=True,
            text=True,
            cwd=ROOT,
        ).stdout
        self.assertNotIn("live_au_set_sweep_interleave_due", old_worker)

        class _IncrementalRunner:
            def __init__(self) -> None:
                self.client = mock.Mock()
                self._ebay_probe_mode = False

            def run_once(self, *, max_jobs: int | None = None) -> list[dict]:
                return [
                    {
                        "jobId": "j1",
                        "status": "completed",
                        "searchSubmissionStarted": True,
                    }
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            budget = ContinuousSafetyBudget()
            runner = _IncrementalRunner()
            calls: list[int] = []

            def _sweep(**_kwargs):
                calls.append(1)
                return None

            with _staged_au_env(owned_daily=False):
                with (
                    mock.patch.dict(
                        os.environ,
                        {"EBAY_SCALE_SET_SWEEP_EVERY_N_PER_CARD_JOBS": "3"},
                        clear=False,
                    ),
                    mock.patch.object(worker.ContinuousSafetyBudget, "from_env", return_value=budget),
                    mock.patch.object(worker, "write_json"),
                    mock.patch.object(worker, "append_jsonl"),
                    mock.patch.object(worker, "append_daily_metrics"),
                    mock.patch.object(
                        worker,
                        "classify_continuous_gate",
                        return_value={"workerState": "SELECTING", "hardStop": None, "availabilityState": "HEALTHY"},
                    ),
                    mock.patch(
                        "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.try_set_sweep_worker_cycle",
                        side_effect=_sweep,
                    ),
                ):
                    worker.run_worker_loop(
                        args=SimpleNamespace(once=False, max_cycles=4),
                        config=_fake_config(root),  # type: ignore[arg-type]
                        runner=runner,  # type: ignore[arg-type]
                        poll_seconds=1,
                        max_jobs=1,
                        sleep_func=lambda _s: None,
                        logger=lambda *_a, **_k: None,
                    )
            self.assertEqual(len(calls), 1)

    def test_flags_off_no_sweep_on_live_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            budget = ContinuousSafetyBudget()
            runner = IdleQueueRunner(results=[])
            calls: list[str] = []

            def _sweep(**_kwargs):
                calls.append("sweep")
                return None

            with (
                mock.patch.dict(os.environ, {"EBAY_SCALE_ENABLED": "false"}, clear=False),
                mock.patch.object(worker.ContinuousSafetyBudget, "from_env", return_value=budget),
                mock.patch.object(worker, "owned_daily_full_enable", return_value=False),
                mock.patch.object(worker, "write_json"),
                mock.patch.object(worker, "append_jsonl"),
                mock.patch.object(worker, "append_daily_metrics"),
                mock.patch.object(
                    worker,
                    "classify_continuous_gate",
                    return_value={"workerState": "SELECTING", "hardStop": None, "availabilityState": "HEALTHY"},
                ),
                mock.patch(
                    "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.try_set_sweep_worker_cycle",
                    side_effect=_sweep,
                ),
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
            self.assertEqual(calls, [])

    def test_hard_stop_blocks_should_attempt(self) -> None:
        budget = ContinuousSafetyBudget()
        budget.latch_hard_stop("CHALLENGE_REQUIRED", hard_stop_source=HARD_STOP_SOURCE_SET_SWEEP)
        plan = {"setCode": "sv3", "marketCountry": "AU", "consumed": False}
        with mock.patch.dict(
            os.environ,
            {
                "EBAY_SCALE_ENABLED": "true",
                "EBAY_SCALE_SET_SWEEP": "true",
                "EBAY_SCALE_SHADOW_AUDIT": "true",
            },
            clear=False,
        ):
            ok, reason = should_attempt_set_sweep(
                plan=plan,
                budget=budget,
                worker_state="SELECTING",
                availability_state="HEALTHY",
                hard_stop=None,
            )
        self.assertFalse(ok)
        self.assertEqual(reason, "hard_stop")

    def test_shadow_execution_does_not_call_live_price_gate(self) -> None:
        from cardscanr_market_engine.ebay_scale.set_sweep_worker_hook import execute_set_sweep_shadow

        html = (ROOT / "tests" / "fixtures" / "ebay_scale" / "sv3_set_sweep_fixture.html").read_text(
            encoding="utf-8"
        )
        plan = {"setCode": "sv3", "setName": "Obsidian Flames", "marketCountry": "AU", "consumed": False}
        targets = [
            {
                "fingerprint": "fp-charizard",
                "catalogue_card_id": "charizard-ex",
                "card_name": "Charizard ex",
                "set_name": "Obsidian Flames",
                "set_id": "sv3",
                "collector_number": "223/197",
                "market_country": "AU",
                "current_market_price": 120.0,
            }
        ]

        class _Client:
            def list_owned_market_pricing_targets(self, **_kwargs):
                return {"targets": targets}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            plan_path = root / "ebay_scale_set_sweep_plan.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            attempt_paths = {
                "ledger": ledger,
                "circuit": root / "circuit.json",
                "attempts": root / "attempts.jsonl",
            }
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "EBAY_SCALE_ENABLED": "true",
                        "EBAY_SCALE_SET_SWEEP": "true",
                        "EBAY_SCALE_SHADOW_AUDIT": "true",
                        "EBAY_SCALE_SET_SWEEP_LIVE": "false",
                        "CARDSCANR_TEST_RUNTIME_ROOT": str(root),
                    },
                    clear=False,
                ),
                mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger),
                mock.patch(
                    "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_PLAN_PATH",
                    plan_path,
                ),
                mock.patch(
                    "cardscanr_market_engine.ebay_scale.audit_report.audit_reports_dir",
                    return_value=root / "ebay_scale_audit",
                ),
                mock.patch(
                    "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.apply_set_sweep_pricing_batch",
                    wraps=__import__(
                        "cardscanr_market_engine.ebay_scale.price_write_gate",
                        fromlist=["apply_set_sweep_pricing_batch"],
                    ).apply_set_sweep_pricing_batch,
                ) as batch_mock,
            ):
                result = execute_set_sweep_shadow(
                    client=_Client(),
                    plan=plan,
                    fetch_html=lambda *_a: html,
                    budget=budget,
                    attempt_paths=attempt_paths,
                )
            self.assertTrue(result.ok)
            self.assertTrue(batch_mock.called)
            from cardscanr_market_engine.ebay_scale.config import should_write_set_sweep_live_price

            self.assertFalse(should_write_set_sweep_live_price())
            audit_payload = result.audit or {}
            self.assertTrue(audit_payload.get("recorded"))
            self.assertNotIn("livePriceWrite", audit_payload)
            reloaded = ContinuousSafetyBudget.from_env(path=ledger)
            self.assertGreaterEqual(reloaded.submissions_1h(), 1)
            updated = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertTrue(updated.get("consumed"))

    def test_consumed_plan_does_not_refire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_path = root / "plan.json"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(
                json.dumps({"setCode": "sv3", "marketCountry": "AU", "consumed": True}) + "\n",
                encoding="utf-8",
            )
            budget = ContinuousSafetyBudget()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "EBAY_SCALE_ENABLED": "true",
                        "EBAY_SCALE_SET_SWEEP": "true",
                        "EBAY_SCALE_SHADOW_AUDIT": "true",
                    },
                    clear=False,
                ),
                mock.patch(
                    "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_PLAN_PATH",
                    plan_path,
                ),
            ):
                ok, reason = should_attempt_set_sweep(plan=read_set_sweep_plan(), budget=budget)
            self.assertFalse(ok)
            self.assertEqual(reason, "no_plan")

    def test_generic_error_page_does_not_transient_latch_set_sweep(self) -> None:
        budget = ContinuousSafetyBudget(max_consecutive_transient=3)
        for _ in range(3):
            stop = handle_set_sweep_marketplace_outcome(MARKETPLACE_GENERIC_ERROR_PAGE, budget=budget)
            self.assertIsNone(stop)
        self.assertEqual(budget.consecutive_transient, 0)
        self.assertIsNone(budget.last_hard_stop)

    def test_staged_set_sweep_hard_stop_writes_continuous_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            runner = IdleQueueRunner(results=[])
            sweep_result = SetSweepExecutionResult(
                ok=False,
                reason="marketplace_unhealthy_html",
                submission_consumed=True,
                hard_stop_reason=CHALLENGE_REQUIRED,
            )
            status_calls: list[dict] = []

            def _capture_status(payload: dict) -> None:
                status_calls.append(payload)

            with _staged_au_env(owned_daily=False):
                with (
                    mock.patch.object(worker.ContinuousSafetyBudget, "from_env", return_value=budget),
                    mock.patch.object(worker, "write_json"),
                    mock.patch.object(worker, "append_jsonl"),
                    mock.patch.object(worker, "append_daily_metrics"),
                    mock.patch.object(worker, "write_continuous_status", side_effect=_capture_status),
                    mock.patch.object(
                        worker,
                        "classify_continuous_gate",
                        return_value={"workerState": "SELECTING", "hardStop": None, "availabilityState": "HEALTHY"},
                    ),
                    mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger),
                    mock.patch(
                        "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.try_set_sweep_worker_cycle",
                        return_value=sweep_result,
                    ),
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
            self.assertTrue(status_calls)
            self.assertEqual(status_calls[-1].get("lastHardStopReason"), CHALLENGE_REQUIRED)
            self.assertEqual(status_calls[-1].get("workerState"), "HARD_STOP")

    def test_fetch_set_sweep_generic_error_page_retries_with_second_reserve(self) -> None:
        from cardscanr_market_engine.providers.ebay_browser_provider import (
            EbayBrowserProviderConfig,
            EbayBrowserSoldCompsProvider,
        )
        from cardscanr_market_engine.providers.linux_x11_ebay_nav import LinuxNavResult
        from tests.test_au_ebay_variant_resilience_20261120 import (
            ERROR_TITLE,
            REAL_GENERIC_ERROR_BODY,
            SOLD_URL,
        )

        provider = EbayBrowserSoldCompsProvider(config=EbayBrowserProviderConfig.from_env())
        budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
        provider.bind_live_submission_budget(budget, first_slot_pre_reserved=True)
        nav_calls: list[int] = []
        error_nav = LinuxNavResult(
            ok=False,
            query="Obsidian Flames sv3 pokemon",
            search_success=True,
            sold_click_success=False,
            sold_state_verified=False,
            url=SOLD_URL,
            title=ERROR_TITLE,
            sorry=True,
            challenge=False,
            sold_date_lines=0,
            error="ebay_error_page",
            diagnostics={
                "sold": {
                    "title": ERROR_TITLE,
                    "sorry": True,
                    "error": "ebay_error_page",
                    "body": REAL_GENERIC_ERROR_BODY,
                }
            },
        )

        def _nav(*_a, **_k):
            nav_calls.append(1)
            return error_nav

        reserve_calls: list[int] = []

        def _reserve(**_kwargs):
            reserve_calls.append(1)
            return None

        with (
            mock.patch.object(provider, "_linux_x11_nav_mode_enabled", return_value=True),
            mock.patch.object(provider, "_ensure_linux_chrome_cdp_ready", return_value=9444),
            mock.patch(
                "cardscanr_market_engine.providers.linux_x11_ebay_nav.navigate_query_to_sold",
                side_effect=_nav,
            ),
            mock.patch.object(provider, "_reserve_live_navigation_slot", side_effect=_reserve),
            mock.patch(
                "cardscanr_market_engine.providers.au_ebay_variant_resilience.generic_error_page_retry_delay_seconds",
                return_value=0.0,
            ),
        ):
            with self.assertRaises(ProviderTemporaryError):
                provider.fetch_set_sweep_results_html(query_text="Obsidian Flames sv3 pokemon")
        self.assertEqual(len(nav_calls), 2)
        self.assertGreaterEqual(len(reserve_calls), 2)

    def test_live_path_hard_stop_skips_future_sweep_attempt_same_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "ledger.json"
            budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
            runner = IdleQueueRunner(results=[])
            sweep_result = SetSweepExecutionResult(
                ok=False,
                reason="marketplace_unhealthy_html",
                submission_consumed=True,
                hard_stop_reason="CHALLENGE_REQUIRED",
            )

            with _staged_au_env(owned_daily=False):
                with (
                    mock.patch.object(worker.ContinuousSafetyBudget, "from_env", return_value=budget),
                    mock.patch.object(worker, "write_json"),
                    mock.patch.object(worker, "append_jsonl"),
                    mock.patch.object(worker, "append_daily_metrics"),
                    mock.patch.object(
                        worker,
                        "classify_continuous_gate",
                        return_value={"workerState": "SELECTING", "hardStop": None, "availabilityState": "HEALTHY"},
                    ),
                    mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger),
                    mock.patch(
                        "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.try_set_sweep_worker_cycle",
                        return_value=sweep_result,
                    ),
                ):
                    code = worker.run_worker_loop(
                        args=SimpleNamespace(once=True, max_cycles=0),
                        config=_fake_config(root),  # type: ignore[arg-type]
                        runner=runner,  # type: ignore[arg-type]
                        poll_seconds=1,
                        max_jobs=1,
                        sleep_func=lambda _s: None,
                        logger=lambda *_a, **_k: None,
                    )
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()