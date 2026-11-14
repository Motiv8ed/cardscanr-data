from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cardscanr_market_engine.live_ebay_component_health import classify_live_ebay_worker_component_health


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class LiveEbayComponentHealthTests(unittest.TestCase):
    def test_no_process(self) -> None:
        row = classify_live_ebay_worker_component_health(process_present=False)
        self.assertEqual(row.status, "no_process")

    def test_hard_stop_with_fresh_runtime(self) -> None:
        now = datetime.now(timezone.utc)
        with self.subTest("ledger latch"):
            status = Path(self._tmp()) / "status.json"
            ledger = Path(self._tmp()) / "ledger.json"
            status.write_text(
                json.dumps({"workerState": "HARD_STOP", "updatedAtUtc": _utc_iso(now)}),
                encoding="utf-8",
            )
            ledger.write_text(
                json.dumps({"lastHardStop": "AU_THREE_TRANSIENT", "updatedAtUtc": _utc_iso(now)}),
                encoding="utf-8",
            )
            row = classify_live_ebay_worker_component_health(
                process_present=True,
                process_id=4242,
                status_path=status,
                ledger_path=ledger,
                now=now,
            )
            self.assertEqual(row.status, "stopped_hard_stop")
            self.assertEqual(row.gate_reason, "AU_THREE_TRANSIENT")

    def test_unreadable_runtime_fail_closed(self) -> None:
        now = datetime.now(timezone.utc)
        status = Path(self._tmp()) / "status.json"
        ledger = Path(self._tmp()) / "ledger.json"
        old = now - timedelta(hours=2)
        status.write_text(json.dumps({"workerState": "SELECTING", "updatedAtUtc": _utc_iso(old)}), encoding="utf-8")
        ledger.write_text(json.dumps({"updatedAtUtc": _utc_iso(now)}), encoding="utf-8")
        row = classify_live_ebay_worker_component_health(
            process_present=True,
            status_path=status,
            ledger_path=ledger,
            now=now,
        )
        self.assertEqual(row.status, "unknown_fail_closed")

    def test_healthy_with_fresh_selecting(self) -> None:
        now = datetime.now(timezone.utc)
        status = Path(self._tmp()) / "status.json"
        ledger = Path(self._tmp()) / "ledger.json"
        payload_time = _utc_iso(now)
        status.write_text(json.dumps({"workerState": "SELECTING", "updatedAtUtc": payload_time}), encoding="utf-8")
        ledger.write_text(json.dumps({"lastHardStop": None, "updatedAtUtc": payload_time}), encoding="utf-8")
        row = classify_live_ebay_worker_component_health(
            process_present=True,
            status_path=status,
            ledger_path=ledger,
            now=now,
        )
        self.assertEqual(row.status, "healthy")

    def _tmp(self) -> str:
        import tempfile

        return tempfile.mkdtemp()


if __name__ == "__main__":
    unittest.main()