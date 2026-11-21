#!/usr/bin/env python3
"""Regression: staged eBay worker must leave transient halt only via safe gate.

No marketplace requests, browser sessions, queues, or production state are used.
"""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

from cardscanr_market_engine.continuous_worker_policy import maybe_resume_transient_halt
from cardscanr_market_engine.owned_daily_outcomes import (
    CHALLENGE_REQUIRED,
    TEMPORARY_EBAY_SERVER_FAILURE,
)
from cardscanr_market_engine.owned_daily_pacing import OwnedDailyPacingController


ROOT = Path(__file__).resolve().parent.parent


def _halted_controller(outcome: str = TEMPORARY_EBAY_SERVER_FAILURE) -> OwnedDailyPacingController:
    controller = OwnedDailyPacingController()
    controller.observe_outcome(outcome)
    assert controller.state.browser_halted
    return controller


class StagedTransientHaltResumeTests(unittest.TestCase):
    def test_staged_worker_invokes_resume_within_availability_gate(self) -> None:
        """Reproduces source defect: 'if continuous and pacing' skipped staged mode."""
        module = ast.parse((ROOT / "workers" / "market_price_worker.py").read_text(encoding="utf-8-sig"))
        loop = next(
            node for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "run_worker_loop"
        )
        availability_branches = [
            node for node in ast.walk(loop)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Call)
            and isinstance(node.test.func, ast.Name)
            and node.test.func.id == "_should_classify_ebay_availability_gate"
        ]
        self.assertEqual(len(availability_branches), 1)
        branch = availability_branches[0]
        # The resume check must be a direct branch child; not conditional on
        # owned_daily_full_enable() being true, or on a fresh pricing task.
        resume_guards = [
            node for node in branch.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "pacing_controller"
            and len(node.test.ops) == 1
            and isinstance(node.test.ops[0], ast.IsNot)
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value is None
        ]
        self.assertEqual(len(resume_guards), 1, "staged worker missing unconditional pacing recovery check")
        calls = [
            node for node in ast.walk(resume_guards[0])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "maybe_resume_transient_halt"
        ]
        self.assertEqual(len(calls), 1)

    def test_gate_healthy_releases_only_temporary_halt(self) -> None:
        controller = _halted_controller()
        self.assertTrue(
            maybe_resume_transient_halt(
                controller, {"workerState": "SELECTING", "hardStop": None}
            )
        )
        self.assertFalse(controller.state.browser_halted)
        self.assertIsNone(controller.state.halt_reason)

    def test_probe_ready_releases_temporary_halt(self) -> None:
        controller = _halted_controller()
        self.assertTrue(
            maybe_resume_transient_halt(
                controller, {"workerState": "PROBE_REQUIRED", "hardStop": None}
            )
        )
        self.assertFalse(controller.state.browser_halted)

    def test_cooldown_and_waiting_cannot_release(self) -> None:
        for state in ("COOLDOWN", "WAITING", "BUDGET_WAIT", "HARD_STOP"):
            with self.subTest(state=state):
                controller = _halted_controller()
                self.assertFalse(
                    maybe_resume_transient_halt(
                        controller, {"workerState": state, "hardStop": None}
                    )
                )
                self.assertTrue(controller.state.browser_halted)

    def test_hard_stop_prevents_recovery_even_when_probe_ready(self) -> None:
        controller = _halted_controller()
        self.assertFalse(
            maybe_resume_transient_halt(
                controller, {"workerState": "PROBE_REQUIRED", "hardStop": "ACTIVE_CHALLENGE"}
            )
        )
        self.assertTrue(controller.state.browser_halted)

    def test_challenge_halt_never_auto_resumes(self) -> None:
        controller = _halted_controller(CHALLENGE_REQUIRED)
        self.assertFalse(
            maybe_resume_transient_halt(
                controller, {"workerState": "SELECTING", "hardStop": None}
            )
        )
        self.assertEqual(controller.state.halt_reason, CHALLENGE_REQUIRED)
        self.assertTrue(controller.state.browser_halted)


if __name__ == "__main__":
    unittest.main()
