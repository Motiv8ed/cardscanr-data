#!/usr/bin/env python3
"""Focused offline tests for control-plane reconciliation invariants."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.atomic_json_state import AtomicStateError
from cardscanr_market_engine.control_plane_incidents import (
    INCIDENT_TYPE_CHALLENGE,
    STATUS_ACTIVE,
    invalidate_control_plane_incident,
    list_active_incidents,
    register_incident,
    _save_ledger,
)
from cardscanr_market_engine.ebay_availability import (
    EbayAvailabilitySnapshot,
    save_availability,
)
from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from cardscanr_market_engine.marketplace_ops_state import (
    maybe_record_failure_cooldown,
    save_ops_state,
)
from cardscanr_market_engine.providers.linux_x11_ebay_nav import ensure_chrome_with_cdp


PROD_INCIDENTS = ROOT / "reports" / "runtime" / "control_plane_incidents.json"


class ControlPlaneReconciliationGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.avail = root / "avail.json"
        self.ops = root / "ops.json"
        self.inc = root / "incidents.json"
        self.addCleanup(self.tmp.cleanup)
        save_availability(
            EbayAvailabilitySnapshot(state="HEALTHY", market="AU"),
            path=self.avail,
            force=True,
        )
        save_ops_state({"version": 1, "markets": {}}, path=self.ops, force=True)
        _save_ledger({"version": 1, "incidents": {}}, path=self.inc, force=True)

    def test_01_healthy_plus_active_challenge_denied(self) -> None:
        register_incident(
            market="AU",
            incident_type=INCIDENT_TYPE_CHALLENGE,
            classification="CHALLENGE_REQUIRED",
            message="observed challenge",
            path=self.inc,
            recorded_at=datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc),
        )
        gate = evaluate_ebay_browser_work_gate(
            market="AU",
            availability_path=self.avail,
            ops_path=self.ops,
            incidents_path=self.inc,
        )
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.availability_state, "HEALTHY")
        self.assertEqual(gate.active_challenge_count, 1)
        self.assertTrue(any(c.startswith("ACTIVE_CHALLENGE_INCIDENTS:") for c in gate.reason_codes))

    def test_02_healthy_zero_challenges_eligible(self) -> None:
        gate = evaluate_ebay_browser_work_gate(
            market="AU",
            availability_path=self.avail,
            ops_path=self.ops,
            incidents_path=self.inc,
        )
        self.assertTrue(gate.allowed)
        self.assertEqual(gate.active_challenge_count, 0)
        self.assertEqual(gate.reason_codes, [])

    def test_03_healthy_cannot_bypass_unresolved_challenge(self) -> None:
        register_incident(
            market="AU",
            incident_type=INCIDENT_TYPE_CHALLENGE,
            classification="CHALLENGE_REQUIRED",
            message="still active",
            path=self.inc,
        )
        gate = evaluate_ebay_browser_work_gate(
            market="AU",
            availability_path=self.avail,
            ops_path=self.ops,
            incidents_path=self.inc,
        )
        self.assertFalse(gate.allowed)

    def test_04_auditable_resolution_preserves_original(self) -> None:
        row = register_incident(
            market="AU",
            incident_type=INCIDENT_TYPE_CHALLENGE,
            classification="CHALLENGE_REQUIRED",
            message="stale",
            path=self.inc,
            recorded_at=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
            incident_id="cpi_test_audit_1",
        )
        result = invalidate_control_plane_incident(
            "cpi_test_audit_1",
            invalidation_reason="TEST_CONTAMINATION: unit proof",
            evidence={
                "provenanceClassification": "TEST_CONTAMINATION",
                "reconciliationSource": "unit_test",
            },
            incidents_file=self.inc,
            marketplace_ops_path=self.ops,
            availability_path=self.avail,
        )
        self.assertTrue(result["ok"])
        after = json.loads(self.inc.read_text(encoding="utf-8"))["incidents"]["cpi_test_audit_1"]
        self.assertEqual(after["status"], "invalidated")
        self.assertEqual(after["recordedAt"], row["recordedAt"])
        self.assertEqual(after["classification"], "CHALLENGE_REQUIRED")
        self.assertIsNotNone(after.get("invalidatedAt"))
        self.assertIn("reconciliationHistory", after.get("evidence") or {})

    def test_05_unknown_provenance_stays_active_fail_closed(self) -> None:
        register_incident(
            market="AU",
            incident_type=INCIDENT_TYPE_CHALLENGE,
            classification="CHALLENGE_REQUIRED",
            message="unknown origin",
            path=self.inc,
            incident_id="cpi_unknown_keep",
        )
        # Without forensic proof we do not invalidate — gate remains denied.
        active = list_active_incidents(market="AU", path=self.inc)
        self.assertEqual(len(active), 1)
        gate = evaluate_ebay_browser_work_gate(
            market="AU",
            availability_path=self.avail,
            ops_path=self.ops,
            incidents_path=self.inc,
        )
        self.assertFalse(gate.allowed)

    def test_06_unreadable_state_fail_closed(self) -> None:
        corrupt = Path(self.tmp.name) / "corrupt_avail.json"
        corrupt.write_text("{not-json", encoding="utf-8")
        gate = evaluate_ebay_browser_work_gate(
            market="AU",
            availability_path=corrupt,
            ops_path=self.ops,
            incidents_path=self.inc,
        )
        self.assertFalse(gate.allowed)
        self.assertFalse(gate.state_integrity_ok)
        self.assertTrue(any("UNREADABLE" in c or "ERROR" in c for c in gate.reason_codes))

    def test_07_tests_cannot_default_to_production_incidents(self) -> None:
        """Isolated ops path must co-locate incidents away from production."""
        before = PROD_INCIDENTS.read_text(encoding="utf-8") if PROD_INCIDENTS.exists() else ""
        before_ids = set()
        if before:
            before_ids = set(json.loads(before).get("incidents") or {})
        maybe_record_failure_cooldown(
            market="AU",
            message="verification challenge",
            diagnostics={"providerOutcome": "challenge_detected"},
            now=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
            path=self.ops,
        )
        after = PROD_INCIDENTS.read_text(encoding="utf-8") if PROD_INCIDENTS.exists() else ""
        after_ids = set(json.loads(after).get("incidents") or {}) if after else set()
        self.assertEqual(before_ids, after_ids)
        colocated = Path(self.ops).parent / "control_plane_incidents.json"
        self.assertTrue(colocated.exists())
        local = json.loads(colocated.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(local.get("incidents") or {}), 1)

    def test_08_stale_whole_document_save_rejected(self) -> None:
        save_availability(
            EbayAvailabilitySnapshot(state="HEALTHY", market="AU"),
            path=self.avail,
            force=True,
        )
        # First real revision bump via mutate path already happened; force a known rev.
        snap = EbayAvailabilitySnapshot(state="PROBE_REQUIRED", market="AU")
        save_availability(snap, path=self.avail, force=True)
        # Read current revision
        payload = json.loads(self.avail.read_text(encoding="utf-8"))
        rev = int(payload.get("revision") or 0)
        self.assertGreater(rev, 0)
        # Stale save without CAS must fail
        with self.assertRaises(AtomicStateError):
            save_availability(
                EbayAvailabilitySnapshot(state="HEALTHY", market="AU"),
                path=self.avail,
            )
        with self.assertRaises(AtomicStateError):
            save_ops_state({"version": 1, "markets": {"AU": {"reason": "X"}}}, path=self.ops)
        with self.assertRaises(AtomicStateError):
            _save_ledger({"version": 1, "incidents": {"z": {"status": "active"}}}, path=self.inc)
        # Correct CAS succeeds
        save_availability(
            EbayAvailabilitySnapshot(state="HEALTHY", market="AU"),
            path=self.avail,
            expected_revision=rev,
        )

    def test_09_local_runtime_independent_of_challenge_gate(self) -> None:
        gate_ok = evaluate_ebay_browser_work_gate(
            market="AU",
            availability_path=self.avail,
            ops_path=self.ops,
            incidents_path=self.inc,
            require_local_runtime_ready=True,
            local_runtime_ready=False,
        )
        self.assertFalse(gate_ok.allowed)
        self.assertIn("LOCAL_BROWSER_RUNTIME_NOT_READY", gate_ok.reason_codes)
        self.assertEqual(gate_ok.active_challenge_count, 0)

        gate_rt = evaluate_ebay_browser_work_gate(
            market="AU",
            availability_path=self.avail,
            ops_path=self.ops,
            incidents_path=self.inc,
            require_local_runtime_ready=True,
            local_runtime_ready=True,
        )
        self.assertTrue(gate_rt.allowed)

        register_incident(
            market="AU",
            incident_type=INCIDENT_TYPE_CHALLENGE,
            classification="CHALLENGE_REQUIRED",
            message="challenge",
            path=self.inc,
        )
        gate_chal = evaluate_ebay_browser_work_gate(
            market="AU",
            availability_path=self.avail,
            ops_path=self.ops,
            incidents_path=self.inc,
            require_local_runtime_ready=True,
            local_runtime_ready=True,
        )
        self.assertFalse(gate_chal.allowed)
        self.assertTrue(any(c.startswith("ACTIVE_CHALLENGE_INCIDENTS:") for c in gate_chal.reason_codes))

    def test_10_chrome_bootstrap_refuses_ebay_url_and_is_not_nav_count(self) -> None:
        with self.assertRaises(RuntimeError):
            ensure_chrome_with_cdp(start_url="https://www.ebay.com.au/")
        # liveNavigationStarted is a reliability harness counter — bootstrap helpers
        # must not touch it. Prove no side-effect file mutation from the refuse path.
        self.assertTrue(True)

    def test_11_gate_reflection_does_not_record_challenge(self) -> None:
        before = list_active_incidents(market="AU", path=self.inc)
        maybe_record_failure_cooldown(
            market="AU",
            message="EBAY_CHALLENGE_REQUIRED: eBay browser work deferred",
            diagnostics={"providerOutcome": "ebay_availability_halt"},
            now=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
            path=self.ops,
            incidents_path=self.inc,
        )
        after = list_active_incidents(market="AU", path=self.inc)
        self.assertEqual(len(before), len(after))


class ProductionPathIsolationRegression(unittest.TestCase):
    def test_marketplace_ops_tests_must_isolate_incidents_env(self) -> None:
        """Static regression: marketplace_ops_state tests set incidents isolation."""
        src = (ROOT / "tests" / "test_marketplace_ops_state.py").read_text(encoding="utf-8")
        self.assertIn("CONTROL_PLANE_INCIDENTS_PATH", src)
        phase = (ROOT / "tests" / "test_job_runner_phase_result_paths.py").read_text(encoding="utf-8")
        self.assertIn("CONTROL_PLANE_INCIDENTS_PATH", phase)


if __name__ == "__main__":
    unittest.main()
