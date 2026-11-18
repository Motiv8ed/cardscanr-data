"""DATA-47 corrective QA: set-sweep budget, HARD_STOP, backoff, worker gates."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.continuous_safety import (
    SAFETY_LEDGER_FAIL_CLOSED,
    ContinuousSafetyBudget,
)
from cardscanr_market_engine.ebay_scale import set_sweep_safety as sweep_safety_mod
from cardscanr_market_engine.ebay_scale.set_sweep_worker_hook import execute_set_sweep_shadow
from cardscanr_market_engine.ebay_scale.set_sweep_safety import (
    classify_set_sweep_html,
    load_set_sweep_circuit,
    record_set_sweep_circuit_failure,
    sweep_worker_gate_blocks,
)
from cardscanr_market_engine.ebay_scale.set_sweep_worker_hook import (
    SET_SWEEP_LAST_RUN_PATH,
    SET_SWEEP_PLAN_PATH,
    should_attempt_set_sweep,
    try_set_sweep_worker_cycle,
)
from cardscanr_market_engine.owned_daily_outcomes import TEMPORARY_EBAY_SERVER_FAILURE
from cardscanr_market_engine.providers.errors import ProviderTemporaryError


class FakeOwnedClient:
    def __init__(self, targets: list[dict]) -> None:
        self.targets = targets

    def list_owned_market_pricing_targets(self, **_kwargs) -> dict:
        return {"targets": list(self.targets)}


def _enable_scale_env() -> dict[str, str]:
    return {
        "EBAY_SCALE_ENABLED": "true",
        "EBAY_SCALE_SET_SWEEP": "true",
        "EBAY_SCALE_SHADOW_AUDIT": "true",
        "EBAY_SCALE_SET_SWEEP_LIVE": "false",
    }


def _targets() -> list[dict]:
    return [
        {
            "fingerprint": "fp-charizard",
            "catalogue_card_id": "charizard-ex",
            "card_name": "Charizard ex",
            "set_name": "Obsidian Flames",
            "set_id": "sv3",
            "collector_number": "223/197",
            "market_country": "AU",
            "current_market_price": 120.0,
        },
    ]


class SetSweepSafety20261119Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._env_patch = mock.patch.dict(os.environ, _enable_scale_env(), clear=False)
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()

    def _paths(self) -> dict[str, Path]:
        return {
            "plan": self._root / "plan.json",
            "last": self._root / "last.json",
            "circuit": self._root / "circuit.json",
            "attempts": self._root / "attempts.jsonl",
            "ledger": self._root / "budget.json",
        }

    def _write_plan(self, plan_path: Path) -> None:
        plan_path.write_text(
            json.dumps(
                {
                    "setCode": "sv3",
                    "setName": "Obsidian Flames",
                    "marketCountry": "AU",
                    "consumed": False,
                }
            ),
            encoding="utf-8",
        )

    def test_code_default_daily_submission_cap_is_60(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MAX_LIVE_SUBMISSIONS_PER_DAY", None)
            budget = ContinuousSafetyBudget()
            self.assertEqual(budget.max_submissions_per_day, 60)

    def test_probe_required_blocks_sweep(self) -> None:
        budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
        plan = {"setCode": "sv3", "marketCountry": "AU", "consumed": False}
        ok, reason = should_attempt_set_sweep(
            plan=plan,
            budget=budget,
            worker_state="PROBE_REQUIRED",
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "probe_required")
        blocked, gate_reason = sweep_worker_gate_blocks(worker_state="HARD_STOP")
        self.assertTrue(blocked)
        self.assertEqual(gate_reason, "worker_hard_stop")

    def test_all_runtime_paths_use_temp_files_not_repo_reports(self) -> None:
        paths = self._paths()
        ledger = paths["ledger"]
        with (
            mock.patch("cardscanr_market_engine.continuous_safety.BUDGET_LEDGER_PATH", ledger),
            mock.patch("cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.BUDGET_LEDGER_PATH", ledger),
        ):
            with mock.patch("cardscanr_market_engine.ebay_scale.set_sweep_safety.SET_SWEEP_ATTEMPTS_JSONL", paths["attempts"]):
                with mock.patch("cardscanr_market_engine.ebay_scale.set_sweep_safety.SET_SWEEP_CIRCUIT_PATH", paths["circuit"]):
                    self.assertEqual(sweep_safety_mod.SET_SWEEP_ATTEMPTS_JSONL, paths["attempts"])
                    budget = ContinuousSafetyBudget.from_env(path=ledger)
                    budget.persist(ledger)
                    self.assertTrue(ledger.is_file())
                    self.assertFalse(str(ROOT / "reports" / "runtime" / "continuous_au_safety_ledger.json") == str(ledger))

    def test_post_reserve_exception_applies_backoff_and_counts_submission(self) -> None:
        paths = self._paths()
        self._write_plan(paths["plan"])
        budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
        plan = json.loads(paths["plan"].read_text(encoding="utf-8"))
        with (
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_PLAN_PATH",
                paths["plan"],
            ),
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.apply_set_sweep_pricing_batch",
                side_effect=RuntimeError("batch exploded"),
            ),
        ):
            result = execute_set_sweep_shadow(
                client=FakeOwnedClient(_targets()),
                plan=plan,
                fetch_html=lambda *_a: "<html><body>ok</body></html>",
                budget=budget,
                attempt_paths={**paths, "ledger": paths["ledger"], "circuit": paths["circuit"], "attempts": paths["attempts"]},
            )
        self.assertFalse(result.ok)
        self.assertTrue(result.submission_consumed)
        self.assertEqual(budget.submissions_24h(), 1)
        circuit = load_set_sweep_circuit(paths["circuit"])
        self.assertGreaterEqual(circuit.last_backoff_seconds, 30.0)
        self.assertTrue(circuit.backoff_active())

    def test_corrupt_ledger_fail_closed_blocks_reservation(self) -> None:
        paths = self._paths()
        ledger = paths["ledger"]
        ledger.write_text("{not-json", encoding="utf-8")
        budget = ContinuousSafetyBudget.from_env(path=ledger)
        self.assertTrue(budget.fail_closed)
        blocked = budget.reserve_live_submission(path=ledger, market="AU")
        self.assertEqual(blocked, SAFETY_LEDGER_FAIL_CLOSED)

    def test_min_failure_backoff_clamped_to_30_seconds(self) -> None:
        with mock.patch.dict(os.environ, {"EBAY_SCALE_SET_SWEEP_MIN_FAILURE_BACKOFF_SECONDS": "0"}, clear=False):
            seconds = sweep_safety_mod.compute_failure_backoff_seconds(1)
        self.assertGreaterEqual(seconds, 30.0)

    def test_attempt_log_rotation_caps_lines(self) -> None:
        paths = self._paths()
        attempts = paths["attempts"]
        with mock.patch.dict(os.environ, {"EBAY_SCALE_SET_SWEEP_ATTEMPTS_MAX_LINES": "5"}, clear=False):
            for i in range(12):
                sweep_safety_mod.append_set_sweep_attempt_receipt({"i": i}, path=attempts)
        lines = attempts.read_text(encoding="utf-8").strip().splitlines()
        self.assertLessEqual(len(lines), 5)

    def test_failed_sweeps_consume_budget_before_fetch(self) -> None:
        paths = self._paths()
        self._write_plan(paths["plan"])
        budget = ContinuousSafetyBudget(
            max_submissions_per_hour=30,
            max_submissions_per_day=60,
            max_consecutive_transient=10,
        )
        fetch_calls = {"n": 0}

        def failing_fetch(_q: str, _c: str, _n: str) -> str:
            fetch_calls["n"] += 1
            raise ProviderTemporaryError(
                "TEMPORARY_EBAY_SERVER_FAILURE: eBay SORRY/error page during desktop navigation",
                diagnostics={"reason": "ebay_sorry_error_page"},
            )

        with (
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_PLAN_PATH",
                paths["plan"],
            ),
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_LAST_RUN_PATH",
                paths["last"],
            ),
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_safety.compute_failure_backoff_seconds",
                return_value=0.0,
            ),
        ):
            for _ in range(5):
                result = try_set_sweep_worker_cycle(
                    client=FakeOwnedClient(_targets()),
                    budget=budget,
                    fetch_html=failing_fetch,
                    attempt_paths={
                        "circuit": paths["circuit"],
                        "attempts": paths["attempts"],
                        "ledger": paths["ledger"],
                    },
                )
                self.assertIsNotNone(result)
                assert result is not None
                self.assertTrue(result.submission_consumed)
        self.assertEqual(fetch_calls["n"], 5)
        self.assertEqual(budget.submissions_24h(), 5)
        self.assertTrue(paths["attempts"].is_file())
        self.assertGreater(len(paths["attempts"].read_text(encoding="utf-8").strip().splitlines()), 0)

    def test_rapid_25_failure_cycles_are_bounded_not_zero_not_25_fetches(self) -> None:
        paths = self._paths()
        self._write_plan(paths["plan"])
        budget = ContinuousSafetyBudget(
            max_submissions_per_hour=30,
            max_submissions_per_day=60,
            max_consecutive_transient=3,
        )
        fetch_calls = {"n": 0}

        def failing_fetch(_q: str, _c: str, _n: str) -> str:
            fetch_calls["n"] += 1
            raise ProviderTemporaryError(
                "temporary",
                diagnostics={"reason": "ebay_sorry_error_page"},
            )

        with (
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_PLAN_PATH",
                paths["plan"],
            ),
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_LAST_RUN_PATH",
                paths["last"],
            ),
        ):
            results: list = []
            for _ in range(25):
                results.append(
                    try_set_sweep_worker_cycle(
                        client=FakeOwnedClient(_targets()),
                        budget=budget,
                        fetch_html=failing_fetch,
                        attempt_paths={
                            "circuit": paths["circuit"],
                            "attempts": paths["attempts"],
                            "ledger": paths["ledger"],
                        },
                    )
                )
        self.assertGreater(budget.submissions_24h(), 0)
        self.assertGreaterEqual(fetch_calls["n"], 1)
        self.assertLess(fetch_calls["n"], 25)
        skip_reasons = {r.reason for r in results if r is not None and not r.submission_consumed}
        self.assertIn("failure_backoff", skip_reasons)

    def test_budget_ledger_persists_across_restart(self) -> None:
        paths = self._paths()
        ledger = paths["ledger"]
        now = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)
        budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
        budget.record_submission(now=now, market="AU")
        budget.persist(ledger)
        reloaded = ContinuousSafetyBudget.from_env(path=ledger)
        self.assertEqual(reloaded.submissions_24h(now=now + timedelta(minutes=1)), 1)
        self.assertEqual(reloaded.submissions_1h_for_market("AU", now=now + timedelta(minutes=1)), 1)

    def test_sorry_html_triggers_transient_hard_stop(self) -> None:
        sorry = "<html><title>Sorry, something went wrong on our end</title><body>sorry</body></html>"
        outcome = classify_set_sweep_html(sorry)
        self.assertEqual(outcome, TEMPORARY_EBAY_SERVER_FAILURE)
        paths = self._paths()
        self._write_plan(paths["plan"])
        budget = ContinuousSafetyBudget(
            max_submissions_per_hour=20,
            max_submissions_per_day=60,
            max_consecutive_transient=3,
        )

        def sorry_html(_q: str, _c: str, _n: str) -> str:
            return sorry

        with (
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_PLAN_PATH",
                paths["plan"],
            ),
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_worker_hook.SET_SWEEP_LAST_RUN_PATH",
                paths["last"],
            ),
            mock.patch(
                "cardscanr_market_engine.ebay_scale.set_sweep_safety.compute_failure_backoff_seconds",
                return_value=0.0,
            ),
        ):
            for _ in range(3):
                try_set_sweep_worker_cycle(
                    client=FakeOwnedClient(_targets()),
                    budget=budget,
                    fetch_html=sorry_html,
                    attempt_paths={"circuit": paths["circuit"], "attempts": paths["attempts"]},
                )
        self.assertIsNotNone(budget.last_hard_stop)

    def test_recovery_after_backoff_window(self) -> None:
        paths = self._paths()
        self._write_plan(paths["plan"])
        circuit = load_set_sweep_circuit(paths["circuit"])
        now = datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)
        record_set_sweep_circuit_failure(circuit, now=now, path=paths["circuit"])
        circuit = load_set_sweep_circuit(paths["circuit"])
        self.assertTrue(circuit.backoff_active(now))
        self.assertFalse(circuit.backoff_active(now + timedelta(hours=1)))
        budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=60)
        budget.record_healthy()
        ok, reason = should_attempt_set_sweep(
            plan=json.loads(paths["plan"].read_text(encoding="utf-8")),
            budget=budget,
            now=now + timedelta(hours=1),
            circuit_path=paths["circuit"],
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")


if __name__ == "__main__":
    unittest.main()