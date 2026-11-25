"""Offline unit tests for one-time owner-reviewed AU pricing recovery.

Never accesses the user's checkout, Task Scheduler, eBay or production Supabase.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from datetime import datetime, timezone

from tools import owner_reviewed_au_pricing_recovery as recovery


class RecoverySafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.files = {
            "INC_FILE": d / "incidents.json",
            "OPS_FILE": d / "ops.json",
            "AV_FILE": d / "availability.json",
            "PID_FILE": d / "worker.pid.json",
            "BACKUPS": d / "backups",
            "RECEIPT_FILE": d / "backups" / "receipt.json",
        }
        patches = [mock.patch.object(recovery, key, value) for key, value in self.files.items()]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.files["INC_FILE"].write_text(json.dumps({
            "version": 1, "revision": 1, "incidents": {
                recovery.INCIDENT: {
                    "incidentId": recovery.INCIDENT,
                    "market": "AU",
                    "incidentType": "CHALLENGE",
                    "status": "active",
                    "classification": "CHALLENGE_REQUIRED",
                    "recordedAt": recovery.RECORDED,
                    "evidence": {},
                }
            }
        }), encoding="utf-8")
        self.files["OPS_FILE"].write_text(json.dumps({
            "version": 1, "revision": 1, "markets": {
                "AU": {
                    "reason": "CHALLENGE_REQUIRED",
                    "incidentId": recovery.INCIDENT,
                    "recordedAt": recovery.RECORDED,
                    "until": recovery.UNTIL,
                }
            }
        }), encoding="utf-8")
        self.files["AV_FILE"].write_text(json.dumps({
            "version": 1, "revision": 1, "market": "AU",
            "state": "CHALLENGE_REQUIRED",
            "lastChallengeIncidentId": recovery.INCIDENT,
            "probeInFlight": False,
            "lastOutcome": "EBAY_CHALLENGE_REQUIRED",
        }), encoding="utf-8")
        self.files["PID_FILE"].write_text('{"pid":22008}', encoding="utf-8")

    def test_frozen_incident_and_expired_cooldown_pass(self):
        out = recovery.state_preflight()
        self.assertEqual(out["incident"], recovery.INCIDENT)
        self.assertTrue(out["cooldown_expired"])
        self.assertEqual(len(out["state_hashes"]), 3)

    def test_owner_resolution_preserves_truth_and_requires_probe(self):
        recovery.resolve_incident()
        ledger = json.loads(self.files["INC_FILE"].read_text())
        row = ledger["incidents"][recovery.INCIDENT]
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["classification"], "CHALLENGE_REQUIRED")
        self.assertEqual(row["recordedAt"], recovery.RECORDED)
        self.assertEqual(row["resolutionReason"], "OWNER_REVIEWED_PAGE_NORMAL_AFTER_COOLDOWN")
        self.assertEqual(row["evidence"]["reconciliationHistory"][0]["action"],
                         "owner_reviewed_resolution")
        ops = json.loads(self.files["OPS_FILE"].read_text())
        self.assertNotIn("AU", ops.get("markets", {}))
        avail = recovery.load_availability(path=self.files["AV_FILE"], market="AU")
        self.assertEqual(avail.state, "PROBE_REQUIRED")
        self.assertFalse(avail.probe_in_flight)
        self.assertIsNone(avail.last_challenge_incident_id)
        self.assertNotEqual(avail.state, "HEALTHY")
        with self.assertRaisesRegex(RuntimeError, "pinned_incident_mismatch"):
            recovery.state_preflight()

    def test_new_challenge_preserved_and_blocks_recovery(self):
        ledger = json.loads(self.files["INC_FILE"].read_text())
        ledger["incidents"]["cpi_another"] = {
            "incidentId": "cpi_another", "status": "active",
            "market": "AU", "incidentType": "CHALLENGE",
        }
        self.files["INC_FILE"].write_text(json.dumps(ledger), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "new_or_unrelated_active_challenge_present"):
            recovery.state_preflight()
        with self.assertRaisesRegex(RuntimeError, "new_challenge_found_under_lock"):
            recovery.resolve_incident()
        ledger_after = json.loads(self.files["INC_FILE"].read_text())
        self.assertEqual(ledger_after["incidents"][recovery.INCIDENT]["status"], "active")

    def test_wrong_cooldown_provenance_stops_without_changes(self):
        ops = json.loads(self.files["OPS_FILE"].read_text())
        ops["markets"]["AU"]["incidentId"] = "cpi_newer"
        self.files["OPS_FILE"].write_text(json.dumps(ops), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "cooldown_provenance_mismatch"):
            recovery.state_preflight()
        self.assertEqual(json.loads(self.files["INC_FILE"].read_text())[
            "incidents"][recovery.INCIDENT]["status"], "active")

    def test_wrong_availability_reference_stops(self):
        a = json.loads(self.files["AV_FILE"].read_text())
        a["lastChallengeIncidentId"] = "cpi_newer"
        self.files["AV_FILE"].write_text(json.dumps(a), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "availability_reference_mismatch"):
            recovery.state_preflight()

    def test_backup_files_hashes_and_task_xml(self):
        with mock.patch.object(recovery, "ps", return_value="<Task>original</Task>"):
            path = Path(recovery.backup_states())
        for original in (self.files["INC_FILE"], self.files["OPS_FILE"],
                         self.files["AV_FILE"], self.files["PID_FILE"]):
            self.assertEqual((path / original.name).read_bytes(), original.read_bytes())
        self.assertEqual((path / "scheduled_task_original.xml").read_text(),
                         "<Task>original</Task>")

    def test_no_op_without_explicit_owner_approval(self):
        with mock.patch.object(recovery, "apply") as run:
            with mock.patch("sys.argv", ["owner_reviewed_au_pricing_recovery.py",
                                          "--mode", "apply"]):
                code = recovery.main()
        self.assertEqual(code, 2)
        run.assert_not_called()

    def test_probe_status_cannot_claim_healthy_just_because_process_exited(self):
        self.files["BACKUPS"].mkdir()
        pdir = self.files["BACKUPS"] / "probe_testing"
        pdir.mkdir()
        (pdir / "exit_code.txt").write_text("0", encoding="utf-8")
        recovery._atomic_receipt({
            "status": "PROBE_STARTED", "probeDir": str(pdir),
            "probeStartedUtc": "2026-11-23T00:00:00Z",
        })
        with mock.patch.object(recovery, "scheduler", return_value={"state": "Disabled"}):
            status = recovery.probe_status()
        self.assertEqual(status["status"], "PROBE_EXITED")
        self.assertEqual(status["exit_code"], 0)
        self.assertEqual(status["availability_state"], "CHALLENGE_REQUIRED")
        with mock.patch.object(recovery, "scheduler", return_value={"state": "Disabled"}), \
             mock.patch.object(recovery, "ps") as ps:
            with self.assertRaisesRegex(RuntimeError, "single_probe_not_healthy"):
                recovery.resume()
            ps.assert_not_called()

    def test_preview_page_does_not_automatically_unlock_incident(self):
        with mock.patch.object(recovery, "existing_ebay_page", return_value=True):
            self.assertEqual(json.loads(self.files["INC_FILE"].read_text())[
                "incidents"][recovery.INCIDENT]["status"], "active")
            self.assertEqual(recovery.load_availability(
                path=self.files["AV_FILE"], market="AU").state,
                "CHALLENGE_REQUIRED")

    def test_unrelated_transient_incident_remains_active_without_blocking_probe(self):
        ledger = json.loads(self.files["INC_FILE"].read_text())
        ledger["incidents"]["cpi_prior_transient"] = {
            "incidentId": "cpi_prior_transient", "status": "active",
            "market": "AU", "incidentType": "TRANSIENT_EBAY_SORRY",
        }
        self.files["INC_FILE"].write_text(json.dumps(ledger), encoding="utf-8")
        recovery.resolve_incident()
        after = json.loads(self.files["INC_FILE"].read_text())
        self.assertEqual(after["incidents"]["cpi_prior_transient"]["status"], "active")
        self.assertEqual(after["incidents"][recovery.INCIDENT]["status"], "resolved")
        self.assertEqual(recovery.active_blocking_au_incidents(), [])

    def test_status_counts_only_blocking_incidents(self):
        ledger = json.loads(self.files["INC_FILE"].read_text())
        ledger["incidents"]["cpi_prior_transient"] = {
            "incidentId": "cpi_prior_transient", "status": "active",
            "market": "AU", "incidentType": "TRANSIENT_EBAY_SORRY",
        }
        self.files["INC_FILE"].write_text(json.dumps(ledger), encoding="utf-8")
        self.assertEqual(len(recovery.active_blocking_au_incidents()), 1)
        ledger["incidents"][recovery.INCIDENT]["status"] = "resolved"
        self.files["INC_FILE"].write_text(json.dumps(ledger), encoding="utf-8")
        self.assertEqual(recovery.active_blocking_au_incidents(), [])


if __name__ == "__main__":
    unittest.main()
