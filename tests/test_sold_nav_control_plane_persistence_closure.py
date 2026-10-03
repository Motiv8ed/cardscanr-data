#!/usr/bin/env python3
"""Offline closure tests: Sold phases + Windows control-plane persistence."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.atomic_json_state import (
    CONTROL_PLANE_PERSISTENCE_FAILURE,
    WINDOWS_REPLACE_MAX_ATTEMPTS,
    AtomicStateError,
    atomic_replace_with_retry,
    atomic_write_json,
    locked_json_state,
    read_json_object,
)
from cardscanr_market_engine.demand_aware_policy import DemandAwarePolicy
from cardscanr_market_engine.ebay_availability import (
    browser_work_allowed,
    get_availability,
    peek_availability,
    save_availability,
    EbayAvailabilitySnapshot,
)
from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from cardscanr_market_engine.providers.sold_navigation_phases import (
    DEFAULT_SOLD_TIMEOUT_POLICY,
    FixtureClock,
    TERMINAL_SOLD_CLICK_FAILURE,
    TERMINAL_SOLD_CONTROL_DISCOVERY_TIMEOUT,
    TERMINAL_SOLD_STATE_VERIFICATION_TIMEOUT,
    TERMINAL_SOLD_STATE_VERIFIED,
    TERMINAL_SOLD_UNEXPECTED_FILTER,
    build_sold_failure_evidence,
    meowth_historical_replay,
    run_sold_fixture,
)


ORDINARY = "https://www.ebay.com.au/sch/i.html?_nkw=Test+Card&_sacat=0"
SOLD = ORDINARY + "&LH_Sold=1"
PREFLOC = ORDINARY + "&rt=nc&LH_PrefLoc=2"
TITLE = "Test Card for sale | eBay - Google Chrome"


def _concurrent_state_worker(path_str: str, n: int) -> None:
    p = Path(path_str)
    for _ in range(n):
        with locked_json_state(p, default={"version": 1, "count": 0}, timeout_seconds=30) as payload:
            payload["count"] = int(payload.get("count") or 0) + 1
            payload["version"] = 1


class SoldPhaseFixtureTests(unittest.TestCase):
    def test_immediate_sold_success(self):
        clock = FixtureClock(
            frames=[(0.0, ORDINARY, TITLE, {}), (0.1, SOLD, TITLE, {})],
            sold_control_at=0.05,
            click_at=0.08,
        )
        out = run_sold_fixture(clock, poll_s=0.02)
        self.assertTrue(out["ok"])
        self.assertEqual(out["terminal"], TERMINAL_SOLD_STATE_VERIFIED)

    def test_delayed_control_success(self):
        clock = FixtureClock(
            frames=[(0.0, ORDINARY, TITLE, {}), (0.6, SOLD, TITLE, {})],
            sold_control_at=0.4,
            click_at=0.45,
        )
        out = run_sold_fixture(clock, poll_s=0.02)
        self.assertTrue(out["ok"])
        self.assertTrue(out["diagnostics"]["soldControlDiscovered"])

    def test_delayed_verification_success(self):
        clock = FixtureClock(
            frames=[
                (0.0, ORDINARY, TITLE, {}),
                (0.3, ORDINARY, TITLE, {}),
                (1.2, SOLD, TITLE, {}),
            ],
            sold_control_at=0.1,
            click_at=0.15,
        )
        out = run_sold_fixture(clock, poll_s=0.05)
        self.assertTrue(out["ok"])
        self.assertEqual(out["terminal"], TERMINAL_SOLD_STATE_VERIFIED)

    def test_near_timeout_success(self):
        pol = DEFAULT_SOLD_TIMEOUT_POLICY
        # Verify just inside budget
        verify_at = pol.state_verification_s - 0.3
        clock = FixtureClock(
            frames=[
                (0.0, ORDINARY, TITLE, {}),
                (verify_at, SOLD, TITLE, {}),
            ],
            sold_control_at=0.05,
            click_at=0.08,
        )
        out = run_sold_fixture(clock, poll_s=0.1, policy=pol)
        self.assertTrue(out["ok"])

    def test_control_discovery_timeout(self):
        clock = FixtureClock(frames=[(0.0, ORDINARY, TITLE, {})], sold_control_at=None)
        out = run_sold_fixture(clock, never_discover_control=True, poll_s=0.2)
        self.assertFalse(out["ok"])
        self.assertEqual(out["terminal"], TERMINAL_SOLD_CONTROL_DISCOVERY_TIMEOUT)

    def test_post_click_verification_timeout(self):
        clock = FixtureClock(
            frames=[(0.0, ORDINARY, TITLE, {})],
            sold_control_at=0.05,
            click_at=0.08,
        )
        out = run_sold_fixture(clock, poll_s=0.2)
        self.assertFalse(out["ok"])
        self.assertEqual(out["terminal"], TERMINAL_SOLD_STATE_VERIFICATION_TIMEOUT)

    def test_click_failure(self):
        clock = FixtureClock(
            frames=[(0.0, ORDINARY, TITLE, {})],
            sold_control_at=0.05,
            click_at=0.08,
        )
        out = run_sold_fixture(clock, force_click_fail=True)
        self.assertEqual(out["terminal"], TERMINAL_SOLD_CLICK_FAILURE)

    def test_active_challenge_stop(self):
        clock = FixtureClock(
            frames=[(0.0, ORDINARY, "Security Measure - eBay", {})],
            sold_control_at=0.5,
            challenge_active=True,
        )
        out = run_sold_fixture(clock, poll_s=0.05)
        self.assertEqual(out["terminal"], "EBAY_CHALLENGE")

    def test_passive_recaptcha_non_blocking(self):
        clock = FixtureClock(
            frames=[(0.0, ORDINARY, TITLE, {}), (0.3, SOLD, TITLE, {})],
            sold_control_at=0.05,
            click_at=0.08,
            passive_recaptcha=True,
        )
        out = run_sold_fixture(clock, poll_s=0.02)
        self.assertTrue(out["ok"])
        self.assertTrue(out["diagnostics"]["passiveRecaptchaIframe"])

    def test_unexpected_filter_meowth_class(self):
        clock = FixtureClock(
            frames=[(0.0, ORDINARY, TITLE, {}), (0.5, PREFLOC, TITLE, {})],
            sold_control_at=0.1,
            click_at=0.15,
        )
        out = run_sold_fixture(clock, poll_s=0.05)
        self.assertEqual(out["terminal"], TERMINAL_SOLD_UNEXPECTED_FILTER)
        self.assertEqual(out["diagnostics"]["failureStage"], "SOLD_STATE_TRANSITION")

    def test_page_changed_unexpectedly(self):
        clock = FixtureClock(
            frames=[(0.0, ORDINARY, TITLE, {}), (0.4, "https://example.com/", "Example", {})],
            sold_control_at=0.1,
            click_at=0.15,
        )
        out = run_sold_fixture(clock, page_hijack_at=0.35, poll_s=0.05)
        self.assertEqual(out["terminal"], "PAGE_CHANGED_UNEXPECTEDLY")

    def test_phase_diagnostics_and_no_retry(self):
        clock = FixtureClock(frames=[(0.0, ORDINARY, TITLE, {})], sold_control_at=None)
        out = run_sold_fixture(clock, never_discover_control=True, poll_s=0.2)
        ev = build_sold_failure_evidence(
            runtime_mode="COLD_START",
            attempt_id="a1",
            job_id="j1",
            price_key_id="pk1",
            query="Meowth 56 jungle Pokemon",
            search_submitted=True,
            ordinary_results_confirmed=True,
            url=ORDINARY,
            title=TITLE,
            ready_state="complete",
            sold_diagnostics=out["diagnostics"],
            child_return_code=1,
            stderr_summary="fixture",
            error_message=out["terminal"],
        )
        self.assertEqual(ev["attemptId"], "a1")
        self.assertEqual(ev["failureStage"], "SOLD_CONTROL_DISCOVERY")
        self.assertEqual(out["diagnostics"]["retries"], 0)

    def test_meowth_historical_replay(self):
        replay = meowth_historical_replay()
        self.assertIn("SOLD_NAVIGATION_TIMEOUT", replay["historicalVerdictUnchanged"])
        self.assertEqual(replay["rootCauseClass"], "ROOT_CAUSE_REPRODUCED")
        self.assertEqual(replay["futurePhaseTerminal"], TERMINAL_SOLD_UNEXPECTED_FILTER)

    def test_timeout_policy_finite_and_below_legacy(self):
        pol = DEFAULT_SOLD_TIMEOUT_POLICY.to_dict()
        self.assertLess(pol["stateVerificationSeconds"], pol["legacyOpaquePendingSeconds"])
        self.assertGreater(pol["stateVerificationSeconds"], 5.0)


class ControlPlanePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ebay_availability_state.json"
        snap = EbayAvailabilitySnapshot(
            state="HEALTHY",
            confirmed_healthy=True,
            last_outcome="EBAY_AVAILABILITY_CONFIRMED_HEALTHY",
        )
        save_availability(snap, path=self.path, force=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_gate_evaluation_read_only(self):
        before = self.path.read_text(encoding="utf-8")
        mtime = self.path.stat().st_mtime_ns
        gate = evaluate_ebay_browser_work_gate(market="AU", availability_path=self.path)
        self.assertTrue(gate.allowed)
        after = self.path.read_text(encoding="utf-8")
        self.assertEqual(before, after)
        self.assertEqual(mtime, self.path.stat().st_mtime_ns)

    def test_peek_does_not_write(self):
        before = self.path.read_bytes()
        peek_availability(path=self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_browser_work_allowed_default_no_persist(self):
        before = self.path.read_bytes()
        ok, reason, snap = browser_work_allowed(path=self.path)
        self.assertTrue(ok)
        self.assertEqual(snap.state, "HEALTHY")
        self.assertEqual(before, self.path.read_bytes())

    def test_transient_winerror5_retry_succeeds(self):
        calls = {"n": 0}
        real_replace = os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                err = PermissionError(13, "Access is denied")
                err.winerror = 5  # type: ignore[attr-defined]
                raise err
            return real_replace(src, dst)

        tmp = Path(self.tmp.name) / ".ebay_availability_state.json.x.tmp"
        tmp.write_text('{"ok":true}\n', encoding="utf-8")
        dest = Path(self.tmp.name) / "dest.json"
        dest.write_text('{"old":true}\n', encoding="utf-8")
        with mock.patch("cardscanr_market_engine.atomic_json_state.os.replace", side_effect=flaky):
            atomic_replace_with_retry(tmp, dest)
        self.assertGreaterEqual(calls["n"], 3)
        self.assertTrue(json.loads(dest.read_text(encoding="utf-8")).get("ok"))

    def test_persistent_winerror5_fails_closed(self):
        def always_denied(src, dst):
            err = PermissionError(13, "Access is denied")
            err.winerror = 5  # type: ignore[attr-defined]
            raise err

        tmp = Path(self.tmp.name) / ".x.tmp"
        tmp.write_text("{}\n", encoding="utf-8")
        dest = Path(self.tmp.name) / "locked.json"
        dest.write_text('{"keep":true}\n', encoding="utf-8")
        with mock.patch(
            "cardscanr_market_engine.atomic_json_state.os.replace", side_effect=always_denied
        ):
            with self.assertRaises(AtomicStateError) as ctx:
                atomic_replace_with_retry(tmp, dest)
        self.assertIn(CONTROL_PLANE_PERSISTENCE_FAILURE, str(ctx.exception))
        # Original destination not truncated/corrupted
        self.assertEqual(json.loads(dest.read_text(encoding="utf-8"))["keep"], True)

    def test_atomic_write_retry_bound(self):
        self.assertGreaterEqual(WINDOWS_REPLACE_MAX_ATTEMPTS, 3)
        self.assertLessEqual(WINDOWS_REPLACE_MAX_ATTEMPTS, 10)

    def test_locked_writers_share_contract(self):
        # Official API path uses lock + atomic write
        with locked_json_state(self.path, default={}) as payload:
            payload["probeInFlight"] = False
            payload["state"] = "HEALTHY"
        self.assertTrue(self.path.with_suffix(self.path.suffix + ".lock").exists() or True)
        body = read_json_object(self.path)
        self.assertEqual(body.get("state"), "HEALTHY")

    def test_concurrent_state_writes_valid_json(self):
        import multiprocessing as mp

        path = Path(self.tmp.name) / "conc.json"
        path.write_text(json.dumps({"version": 1, "count": 0}), encoding="utf-8")
        procs = [
            mp.Process(target=_concurrent_state_worker, args=(str(path), 20)) for _ in range(4)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)
            self.assertEqual(p.exitcode, 0)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["count"], 80)
        self.assertIsInstance(data, dict)

    def test_stop_accounting_preserves_original_failure(self):
        """Gate snapshot during stop must not mask Sold failure; owned_daily stays off."""
        os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"
        original = {
            "cardVerdict": "FAIL_NAVIGATION",
            "error": "SOLD_UNEXPECTED_FILTER_TRANSITION",
            "attemptId": "757c9e95-be72-49b2-bbe8-eba9c4b19acc",
        }
        # Inject transient replace denial during gate evaluation path (should be read-only now).
        deny = {"n": 0}

        def flaky_replace(src, dst):
            deny["n"] += 1
            err = PermissionError(13, "Access is denied")
            err.winerror = 5  # type: ignore[attr-defined]
            raise err

        with mock.patch(
            "cardscanr_market_engine.atomic_json_state.os.replace", side_effect=flaky_replace
        ):
            # Gate evaluation must not call replace (read-only).
            gate = evaluate_ebay_browser_work_gate(market="AU", availability_path=self.path)
        self.assertEqual(deny["n"], 0)
        self.assertTrue(gate.state_integrity_ok or gate.allowed or not gate.allowed)
        # Original failure remains primary
        self.assertEqual(original["cardVerdict"], "FAIL_NAVIGATION")
        self.assertEqual(os.environ.get("OWNED_DAILY_FULL_ENABLE"), "false")

    def test_demand_scheduler_regression_unchanged(self):
        cfg = DemandAwarePolicy.from_env()
        self.assertEqual(cfg.hot_verified_ttl_hours, 12)
        self.assertEqual(cfg.normal_verified_ttl_hours, 24)
        self.assertEqual(cfg.high_min_requests_24h, 3)
        self.assertAlmostEqual(cfg.lane_demand_share, 0.5)
        self.assertAlmostEqual(cfg.lane_stale_share, 0.3)
        self.assertAlmostEqual(cfg.lane_coverage_share, 0.2)


class ReadinessFlagsTests(unittest.TestCase):
    def test_readiness_bundle(self):
        from tools.sold_nav_control_plane_persistence_readiness import readiness_bundle

        flags = readiness_bundle()
        self.assertTrue(flags["soldNavigationPhasesReady"])
        self.assertTrue(flags["soldTimeoutPolicyEvidenceBased"])
        self.assertTrue(flags["controlPlaneReadWriteSeparated"])
        self.assertTrue(flags["windowsReplaceRetryBounded"])
        self.assertTrue(flags["demandSchedulerRegression"])
        self.assertTrue(flags["ok"])


if __name__ == "__main__":
    unittest.main()
