from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cardscanr_market_engine.live_ebay_component_health import classify_live_ebay_worker_component_health
from cardscanr_market_engine.pricing_worker_runtime_gate import RUNTIME_EVIDENCE_STALE_MINUTES


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fresh_runtime_pair(
    tmp: Path,
    *,
    now: datetime,
    worker_state: str = "SELECTING",
    last_hard_stop: str | None = None,
    status_extra: dict | None = None,
    ledger_extra: dict | None = None,
) -> tuple[Path, Path]:
    status = tmp / "status.json"
    ledger = tmp / "ledger.json"
    payload_time = _utc_iso(now)
    status_body: dict = {
        "workerState": worker_state,
        "updatedAtUtc": payload_time,
    }
    if status_extra:
        status_body.update(status_extra)
    ledger_body: dict = {
        "lastHardStop": last_hard_stop,
        "updatedAtUtc": payload_time,
    }
    if ledger_extra:
        ledger_body.update(ledger_extra)
    status.write_text(json.dumps(status_body), encoding="utf-8")
    ledger.write_text(json.dumps(ledger_body), encoding="utf-8")
    return status, ledger


class LiveEbayComponentHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.now = datetime(2026, 11, 14, 8, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _classify(self, **kwargs):
        return classify_live_ebay_worker_component_health(
            process_present=True,
            process_id=4242,
            now=self.now,
            **kwargs,
        )

    def test_no_process(self) -> None:
        row = classify_live_ebay_worker_component_health(process_present=False)
        self.assertEqual(row.status, "no_process")

    def test_positive_fresh_timestamp_valid_gate_and_ledger(self) -> None:
        status, ledger = _fresh_runtime_pair(self.tmp, now=self.now)
        row = self._classify(status_path=status, ledger_path=ledger)
        self.assertEqual(row.status, "healthy")
        self.assertEqual(row.gate_state, "SELECTING")
        self.assertEqual(row.status_evidence, "fresh")
        self.assertEqual(row.ledger_evidence, "fresh")

    def test_hard_stop_with_fresh_runtime(self) -> None:
        status, ledger = _fresh_runtime_pair(
            self.tmp,
            now=self.now,
            worker_state="HARD_STOP",
            status_extra={"lastHardStopReason": "AU_THREE_TRANSIENT"},
            last_hard_stop="AU_THREE_TRANSIENT",
        )
        row = self._classify(status_path=status, ledger_path=ledger)
        self.assertEqual(row.status, "stopped_hard_stop")
        self.assertEqual(row.gate_reason, "AU_THREE_TRANSIENT")
        self.assertNotEqual(row.status, "healthy")

    def test_stale_status_timestamp_not_healthy(self) -> None:
        status = self.tmp / "status.json"
        ledger = self.tmp / "ledger.json"
        stale = self.now - timedelta(minutes=RUNTIME_EVIDENCE_STALE_MINUTES + 5)
        fresh = _utc_iso(self.now)
        status.write_text(
            json.dumps({"workerState": "SELECTING", "updatedAtUtc": _utc_iso(stale)}),
            encoding="utf-8",
        )
        ledger.write_text(json.dumps({"lastHardStop": None, "updatedAtUtc": fresh}), encoding="utf-8")
        row = self._classify(status_path=status, ledger_path=ledger)
        self.assertEqual(row.status, "unknown_fail_closed")
        self.assertNotEqual(row.status, "healthy")

    def test_missing_timestamp_not_healthy(self) -> None:
        status = self.tmp / "status.json"
        ledger = self.tmp / "ledger.json"
        status.write_text(json.dumps({"workerState": "SELECTING"}), encoding="utf-8")
        ledger.write_text(
            json.dumps({"lastHardStop": None, "updatedAtUtc": _utc_iso(self.now)}),
            encoding="utf-8",
        )
        row = self._classify(status_path=status, ledger_path=ledger)
        self.assertEqual(row.status, "unknown_fail_closed")

    def test_future_timestamp_not_healthy(self) -> None:
        status, ledger = _fresh_runtime_pair(self.tmp, now=self.now)
        future = self.now + timedelta(minutes=30)
        status.write_text(
            json.dumps({"workerState": "SELECTING", "updatedAtUtc": _utc_iso(future)}),
            encoding="utf-8",
        )
        row = self._classify(status_path=status, ledger_path=ledger)
        self.assertEqual(row.status, "unknown_fail_closed")

    def test_missing_status_file_not_healthy(self) -> None:
        ledger = self.tmp / "ledger.json"
        ledger.write_text(
            json.dumps({"lastHardStop": None, "updatedAtUtc": _utc_iso(self.now)}),
            encoding="utf-8",
        )
        row = self._classify(
            status_path=self.tmp / "missing_status.json",
            ledger_path=ledger,
        )
        self.assertEqual(row.status, "unknown_fail_closed")

    def test_missing_ledger_file_not_healthy(self) -> None:
        status = self.tmp / "status.json"
        status.write_text(
            json.dumps({"workerState": "SELECTING", "updatedAtUtc": _utc_iso(self.now)}),
            encoding="utf-8",
        )
        row = self._classify(
            status_path=status,
            ledger_path=self.tmp / "missing_ledger.json",
        )
        self.assertEqual(row.status, "unknown_fail_closed")

    def test_malformed_status_json_not_healthy(self) -> None:
        status = self.tmp / "status.json"
        ledger = self.tmp / "ledger.json"
        status.write_text("{not-json", encoding="utf-8")
        ledger.write_text(
            json.dumps({"lastHardStop": None, "updatedAtUtc": _utc_iso(self.now)}),
            encoding="utf-8",
        )
        row = self._classify(status_path=status, ledger_path=ledger)
        self.assertEqual(row.status, "unknown_fail_closed")

    def test_malformed_ledger_json_not_healthy(self) -> None:
        status, ledger = _fresh_runtime_pair(self.tmp, now=self.now)
        ledger.write_text("[1,2,3]", encoding="utf-8")
        row = self._classify(status_path=status, ledger_path=ledger)
        self.assertEqual(row.status, "unknown_fail_closed")

    def test_missing_worker_state_key_not_healthy(self) -> None:
        status = self.tmp / "status.json"
        ledger = self.tmp / "ledger.json"
        status.write_text(json.dumps({"updatedAtUtc": _utc_iso(self.now)}), encoding="utf-8")
        ledger.write_text(
            json.dumps({"lastHardStop": None, "updatedAtUtc": _utc_iso(self.now)}),
            encoding="utf-8",
        )
        row = self._classify(status_path=status, ledger_path=ledger)
        self.assertEqual(row.status, "unknown_fail_closed")

    def test_empty_worker_state_not_healthy(self) -> None:
        status, ledger = _fresh_runtime_pair(self.tmp, now=self.now, worker_state="")
        row = self._classify(status_path=status, ledger_path=ledger)
        self.assertEqual(row.status, "unknown_fail_closed")
        self.assertNotEqual(row.status, "healthy")

    def test_none_worker_state_not_healthy(self) -> None:
        status, ledger = _fresh_runtime_pair(self.tmp, now=self.now, worker_state="SELECTING")
        status.write_text(
            json.dumps({"workerState": None, "updatedAtUtc": _utc_iso(self.now)}),
            encoding="utf-8",
        )
        row = self._classify(status_path=status, ledger_path=ledger)
        self.assertEqual(row.status, "unknown_fail_closed")

    def test_missing_last_hard_stop_key_not_healthy(self) -> None:
        status = self.tmp / "status.json"
        ledger = self.tmp / "ledger.json"
        ts = _utc_iso(self.now)
        status.write_text(json.dumps({"workerState": "SELECTING", "updatedAtUtc": ts}), encoding="utf-8")
        ledger.write_text(json.dumps({"updatedAtUtc": ts}), encoding="utf-8")
        row = self._classify(status_path=status, ledger_path=ledger)
        self.assertEqual(row.status, "unknown_fail_closed")

    def test_ledger_latch_inconsistent_with_selecting_status_not_healthy(self) -> None:
        status, ledger = _fresh_runtime_pair(
            self.tmp,
            now=self.now,
            worker_state="SELECTING",
            last_hard_stop="AU_THREE_TRANSIENT",
        )
        row = self._classify(status_path=status, ledger_path=ledger)
        self.assertEqual(row.status, "stopped_hard_stop")
        self.assertNotEqual(row.status, "healthy")

    def test_non_allowlisted_gate_state_not_healthy(self) -> None:
        for worker_state in ("BLOCKED", "STOPPED", "NOT_RUNNING", "COOLDOWN", "BUDGET_WAIT"):
            with self.subTest(worker_state=worker_state):
                status, ledger = _fresh_runtime_pair(self.tmp, now=self.now, worker_state=worker_state)
                row = self._classify(status_path=status, ledger_path=ledger)
                self.assertNotEqual(row.status, "healthy")

    def test_pricing_and_waiting_states_can_be_healthy(self) -> None:
        for worker_state in ("PRICING", "WAITING"):
            with self.subTest(worker_state=worker_state):
                status, ledger = _fresh_runtime_pair(self.tmp, now=self.now, worker_state=worker_state)
                row = self._classify(status_path=status, ledger_path=ledger)
                self.assertEqual(row.status, "healthy", msg=worker_state)


if __name__ == "__main__":
    unittest.main()