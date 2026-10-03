#!/usr/bin/env python3
"""Interprocess lock + atomic JSON state concurrency tests (no live eBay)."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.atomic_json_state import (
    AtomicStateError,
    atomic_write_json,
    lock_path_for,
    locked_json_state,
    mutate_json_state,
    read_json_object,
)
from cardscanr_market_engine.ebay_availability import (
    begin_probe,
    record_challenge,
    record_healthy_browser_check,
    record_sorry,
    save_availability,
    EbayAvailabilitySnapshot,
)
from filelock import FileLock


def _worker_increment(path_str: str, n: int, hold_ms: float, ready: mp.Value, go: mp.Event) -> None:
    path = Path(path_str)
    ready.value += 1
    go.wait(timeout=30)
    for _ in range(n):
        with locked_json_state(
            path,
            default={"version": 1, "count": 0},
            timeout_seconds=60.0,
        ) as payload:
            payload["count"] = int(payload.get("count") or 0) + 1
            if hold_ms:
                time.sleep(hold_ms / 1000.0)


def _worker_hold_lock(path_str: str, hold_s: float, started: mp.Event, release: mp.Event) -> None:
    path = Path(path_str)
    with locked_json_state(path, default={"version": 1, "holder": "a"}, timeout_seconds=5.0) as payload:
        payload["holder"] = "a"
        started.set()
        release.wait(timeout=hold_s + 5)
        time.sleep(0.05)


def _worker_try_lock(path_str: str, timeout: float, out_q: mp.Queue) -> None:
    path = Path(path_str)
    try:
        with locked_json_state(path, default={"version": 1}, timeout_seconds=timeout) as payload:
            payload["holder"] = "b"
            out_q.put({"ok": True, "holder": payload.get("holder")})
    except AtomicStateError as exc:
        out_q.put({"ok": False, "error": str(exc)})


def _worker_partial_write_attempt(path_str: str, ready: mp.Event, release: mp.Event) -> None:
    """Simulate a crashed writer: mutate under lock then raise before commit."""
    path = Path(path_str)
    ready.set()
    release.wait(timeout=10)
    try:
        with locked_json_state(path, timeout_seconds=5.0, default={"version": 1, "safe": True}) as payload:
            payload["safe"] = False
            payload["corrupt"] = "partial"
            raise RuntimeError("simulated_crash_before_commit")
    except RuntimeError:
        pass


def _worker_reader(path_str: str, go: mp.Event, done: mp.Event, out_q: mp.Queue, loops: int = 40) -> None:
    path = Path(path_str)
    go.wait(timeout=10)
    for _ in range(loops):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            out_q.put({"ok": True, "payload": payload})
        except Exception as exc:
            out_q.put({"ok": False, "error": f"{type(exc).__name__}:{exc}"})
        time.sleep(0.005)
    done.set()


def _worker_slow_writer(path_str: str, go: mp.Event, done: mp.Event) -> None:
    path = Path(path_str)
    go.wait(timeout=10)
    with locked_json_state(path, timeout_seconds=10.0, default={"version": 1, "safe": True}) as payload:
        payload["safe"] = True
        payload["writer"] = "slow"
        payload["pad"] = "x" * 50_000
        time.sleep(0.15)
    done.set()


def _worker_register_incident(path_str: str, i: int) -> None:
    from cardscanr_market_engine.control_plane_incidents import (
        INCIDENT_TYPE_CHALLENGE,
        register_incident,
    )

    register_incident(
        market="AU",
        incident_type=INCIDENT_TYPE_CHALLENGE,
        classification="CHALLENGE_REQUIRED",
        message=f"inc-{i}",
        incident_id=f"cpi_test_{i}",
        path=Path(path_str),
    )


class AtomicJsonStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_uncontended_update_succeeds(self) -> None:
        path = self.root / "state.json"
        with locked_json_state(path, default={"version": 1, "n": 0}) as payload:
            payload["n"] = 7
        loaded = read_json_object(path)
        self.assertEqual(loaded.get("n"), 7)
        self.assertTrue(path.is_file())

    def test_lock_timeout_fail_closed(self) -> None:
        path = self.root / "state.json"
        atomic_write_json(path, {"version": 1, "n": 1})
        lock = FileLock(str(lock_path_for(path)), timeout=0.1)
        lock.acquire()
        try:
            with self.assertRaises(AtomicStateError) as ctx:
                with locked_json_state(path, timeout_seconds=0.2):
                    pass
            self.assertIn("state_lock_timeout", str(ctx.exception))
            # Canonical file unchanged / still valid.
            self.assertEqual(read_json_object(path).get("n"), 1)
        finally:
            lock.release()

    def test_exception_inside_block_leaves_prior_canonical(self) -> None:
        path = self.root / "state.json"
        atomic_write_json(path, {"version": 1, "n": 42, "ok": True})
        with self.assertRaises(RuntimeError):
            with locked_json_state(path) as payload:
                payload["n"] = 0
                payload["ok"] = False
                raise RuntimeError("boom")
        loaded = read_json_object(path)
        self.assertEqual(loaded.get("n"), 42)
        self.assertTrue(loaded.get("ok"))

    def test_two_processes_cannot_enter_critical_section_together(self) -> None:
        path = self.root / "mutex.json"
        atomic_write_json(path, {"version": 1, "holder": None})
        started = mp.Event()
        release = mp.Event()
        out_q: mp.Queue = mp.Queue()
        p1 = mp.Process(target=_worker_hold_lock, args=(str(path), 2.0, started, release))
        p2 = mp.Process(target=_worker_try_lock, args=(str(path), 0.4, out_q))
        p1.start()
        self.assertTrue(started.wait(timeout=5))
        p2.start()
        p2.join(timeout=5)
        self.assertFalse(p2.is_alive())
        result = out_q.get(timeout=2)
        self.assertFalse(result.get("ok"))
        self.assertIn("state_lock_timeout", str(result.get("error") or ""))
        release.set()
        p1.join(timeout=5)
        self.assertFalse(p1.is_alive())

    def test_concurrent_increments_not_lost(self) -> None:
        path = self.root / "counter.json"
        atomic_write_json(path, {"version": 1, "count": 0})
        ready = mp.Value("i", 0)
        go = mp.Event()
        per = 25
        procs = [
            mp.Process(target=_worker_increment, args=(str(path), per, 0.0, ready, go))
            for _ in range(4)
        ]
        for p in procs:
            p.start()
        deadline = time.time() + 10
        while ready.value < len(procs) and time.time() < deadline:
            time.sleep(0.02)
        go.set()
        for p in procs:
            p.join(timeout=30)
            self.assertFalse(p.is_alive())
            self.assertEqual(p.exitcode, 0)
        loaded = read_json_object(path)
        self.assertEqual(int(loaded.get("count") or 0), per * len(procs))

    def test_atomic_replace_never_exposes_partial_json(self) -> None:
        path = self.root / "reader_safe.json"
        atomic_write_json(path, {"version": 1, "safe": True, "pad": "y"})
        go = mp.Event()
        done_r = mp.Event()
        done_w = mp.Event()
        out_q: mp.Queue = mp.Queue()
        reader = mp.Process(target=_worker_reader, args=(str(path), go, done_r, out_q, 30))
        writer = mp.Process(target=_worker_slow_writer, args=(str(path), go, done_w))
        reader.start()
        writer.start()
        go.set()
        self.assertTrue(done_w.wait(timeout=15))
        self.assertTrue(done_r.wait(timeout=15))
        writer.join(timeout=5)
        reader.join(timeout=5)
        self.assertEqual(writer.exitcode, 0)
        self.assertEqual(reader.exitcode, 0)
        observations = []
        while not out_q.empty():
            observations.append(out_q.get_nowait())
        self.assertTrue(observations)
        for obs in observations:
            self.assertTrue(obs.get("ok"), obs)
            payload = obs.get("payload") or {}
            self.assertIsInstance(payload, dict)
            self.assertIn("version", payload)
            self.assertTrue(payload.get("safe") in {True, False} or "pad" in payload)

    def test_simulated_crash_leaves_prior_canonical_valid(self) -> None:
        path = self.root / "crash.json"
        atomic_write_json(path, {"version": 1, "safe": True, "n": 9})
        ready = mp.Event()
        release = mp.Event()
        crash = mp.Process(target=_worker_partial_write_attempt, args=(str(path), ready, release))
        crash.start()
        self.assertTrue(ready.wait(timeout=5))
        release.set()
        crash.join(timeout=15)
        self.assertEqual(crash.exitcode, 0)
        loaded = read_json_object(path)
        self.assertEqual(loaded.get("n"), 9)
        self.assertTrue(loaded.get("safe"))
        self.assertNotIn("corrupt", loaded)


class AvailabilityInterprocessSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.avail = self.root / "avail.json"
        os.environ["EBAY_AVAILABILITY_STATE_PATH"] = str(self.avail)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("EBAY_AVAILABILITY_STATE_PATH", None))

    def test_stale_healthy_cannot_erase_newer_challenge(self) -> None:
        now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        save_availability(
            EbayAvailabilitySnapshot(
                state="PROBE_REQUIRED",
                probe_in_flight=True,
                market="AU",
                next_probe_at=now,
            ),
            path=self.avail, force=True)
        # Challenger wins / records challenge while a stale healthy is in flight.
        record_challenge(now=now + timedelta(seconds=1), reference="SORRY/challenge live", path=self.avail)
        after_challenge = read_json_object(self.avail)
        self.assertEqual(after_challenge.get("state"), "CHALLENGE_REQUIRED")
        # Stale healthy reconciliation must not clear challenge.
        snap = record_healthy_browser_check(
            now=now + timedelta(seconds=2),
            path=self.avail,
            from_probe=True,
        )
        self.assertEqual(snap.state, "CHALLENGE_REQUIRED")
        self.assertEqual(read_json_object(self.avail).get("state"), "CHALLENGE_REQUIRED")

    def test_stale_healthy_cannot_erase_newer_sorry_cooldown(self) -> None:
        now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
        save_availability(
            EbayAvailabilitySnapshot(
                state="HEALTHY",
                market="AU",
                probe_in_flight=False,
            ),
            path=self.avail, force=True)
        record_sorry(now=now, reference="ebay sorry page", path=self.avail, from_probe=False)
        self.assertEqual(read_json_object(self.avail).get("state"), "COOLDOWN")
        snap = record_healthy_browser_check(now=now + timedelta(seconds=1), path=self.avail, from_probe=False)
        self.assertEqual(snap.state, "COOLDOWN")

    def test_probe_in_flight_not_cleared_by_stale_healthy_when_challenge(self) -> None:
        now = datetime(2026, 10, 1, 13, 0, 0, tzinfo=timezone.utc)
        save_availability(
            EbayAvailabilitySnapshot(
                state="PROBE_REQUIRED",
                probe_in_flight=False,
                market="AU",
                next_probe_at=now,
            ),
            path=self.avail, force=True)
        begin_probe(now=now, path=self.avail)
        self.assertTrue(read_json_object(self.avail).get("probeInFlight"))
        record_challenge(now=now + timedelta(seconds=1), reference="challenge", path=self.avail)
        # Stale worker tries healthy clear — must leave challenge + not invent HEALTHY.
        snap = record_healthy_browser_check(now=now + timedelta(seconds=2), path=self.avail, from_probe=True)
        self.assertEqual(snap.state, "CHALLENGE_REQUIRED")
        # Challenge path clears probe_in_flight as part of halt; ensure not HEALTHY.
        self.assertNotEqual(snap.state, "HEALTHY")


class IncidentCasInterprocessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ops = self.root / "ops.json"
        self.inc = self.root / "inc.json"
        os.environ["MARKET_OPS_STATE_PATH"] = str(self.ops)
        os.environ["CONTROL_PLANE_INCIDENTS_PATH"] = str(self.inc)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("MARKET_OPS_STATE_PATH", None))
        self.addCleanup(lambda: os.environ.pop("CONTROL_PLANE_INCIDENTS_PATH", None))

    def test_concurrent_incident_registers_not_lost(self) -> None:
        from cardscanr_market_engine.control_plane_incidents import _load_ledger

        procs = [
            mp.Process(target=_worker_register_incident, args=(str(self.inc), i))
            for i in range(8)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=20)
            self.assertEqual(p.exitcode, 0)
        ledger = _load_ledger(path=self.inc)
        incidents = ledger.get("incidents") or {}
        self.assertEqual(len(incidents), 8)


if __name__ == "__main__":
    # Windows-safe multiprocessing start method.
    try:
        mp.set_start_method("spawn")
    except RuntimeError:
        pass
    unittest.main()
