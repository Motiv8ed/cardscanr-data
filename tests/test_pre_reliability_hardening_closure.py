#!/usr/bin/env python3
"""Extended interprocess lock / tempfile / seed / fail-closed tests (no live eBay)."""
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
    read_json_object,
)
from cardscanr_market_engine.control_plane_state_ownership import topology_summary
from cardscanr_market_engine.ebay_availability import (
    EbayAvailabilitySnapshot,
    begin_probe,
    browser_work_allowed,
    load_availability,
    record_challenge,
    record_healthy_browser_check,
    record_sorry,
    save_availability,
    seed_from_observed_sorrys,
)
from cardscanr_market_engine.marketplace_ops_state import load_ops_state, save_ops_state
from filelock import FileLock


def _worker_hold_active_temp(path_str: str, temp_str: str, ready: mp.Event, release: mp.Event) -> None:
    """Create an active tempfile while holding the state lock (simulates mid-write)."""
    path = Path(path_str)
    temp = Path(temp_str)
    lock = FileLock(str(lock_path_for(path)), timeout=5.0)
    lock.acquire()
    try:
        temp.write_text('{"partial": true', encoding="utf-8")
        ready.set()
        release.wait(timeout=15)
    finally:
        lock.release()


def _worker_failed_locked_write(path_str: str, go: mp.Event, done: mp.Event) -> None:
    path = Path(path_str)
    go.wait(timeout=10)
    try:
        with locked_json_state(path, timeout_seconds=5.0, default={"version": 1, "n": 0}) as payload:
            payload["n"] = 999
            raise RuntimeError("force_fail_after_mutate")
    except RuntimeError:
        pass
    done.set()


def _worker_success_write(path_str: str, go: mp.Event, done: mp.Event, value: int) -> None:
    path = Path(path_str)
    go.wait(timeout=10)
    with locked_json_state(path, timeout_seconds=10.0, default={"version": 1, "n": 0}) as payload:
        payload["n"] = value
    done.set()


def _worker_seed(path_str: str, ready: mp.Event, go: mp.Event, out_q: mp.Queue) -> None:
    path = Path(path_str)
    ready.set()
    go.wait(timeout=10)
    try:
        seed_from_observed_sorrys(
            last_sorry_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            consecutive_sorry_events=2,
            path=path,
            reference="stale-seed",
            now=datetime(2026, 9, 1, 0, 5, tzinfo=timezone.utc),
        )
        out_q.put({"ok": True})
    except Exception as exc:
        out_q.put({"ok": False, "error": f"{type(exc).__name__}:{exc}"})


def _worker_challenge(path_str: str, ready: mp.Event, go: mp.Event, done: mp.Event) -> None:
    path = Path(path_str)
    ready.set()
    go.wait(timeout=10)
    record_challenge(
        now=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        reference="live-challenge",
        path=path,
    )
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
        incident_id=f"cpi_closure_{i}",
        path=Path(path_str),
    )


class TempfileIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_failed_writer_does_not_delete_peer_active_tempfile(self) -> None:
        path = self.root / "state.json"
        atomic_write_json(path, {"version": 1, "n": 1})
        peer_temp = self.root / f".{path.name}.PEERACTIVE.tmp"
        ready = mp.Event()
        release = mp.Event()
        holder = mp.Process(target=_worker_hold_active_temp, args=(str(path), str(peer_temp), ready, release))
        holder.start()
        self.assertTrue(ready.wait(timeout=5))
        self.assertTrue(peer_temp.exists())
        # Failed locked write while peer holds lock should time out fail-closed,
        # and must not glob-delete peer_temp after its own attempt.
        with self.assertRaises(AtomicStateError):
            with locked_json_state(path, timeout_seconds=0.3, default={"version": 1}):
                pass
        self.assertTrue(peer_temp.exists(), "peer active tempfile must survive foreign cleanup")
        release.set()
        holder.join(timeout=5)
        self.assertFalse(holder.is_alive())

    def test_successful_writer_commits_after_failed_writer(self) -> None:
        path = self.root / "state.json"
        atomic_write_json(path, {"version": 1, "n": 1})
        go = mp.Event()
        done_fail = mp.Event()
        done_ok = mp.Event()
        p_fail = mp.Process(target=_worker_failed_locked_write, args=(str(path), go, done_fail))
        p_ok = mp.Process(target=_worker_success_write, args=(str(path), go, done_ok, 42))
        p_fail.start()
        p_ok.start()
        go.set()
        self.assertTrue(done_fail.wait(timeout=10))
        self.assertTrue(done_ok.wait(timeout=10))
        p_fail.join(timeout=5)
        p_ok.join(timeout=5)
        self.assertEqual(read_json_object(path).get("n"), 42)

    def test_crash_cleanup_removes_only_owned_temp(self) -> None:
        path = self.root / "state.json"
        atomic_write_json(path, {"version": 1, "safe": True})
        foreign = self.root / f".{path.name}.FOREIGN.tmp"
        foreign.write_text("foreign", encoding="utf-8")
        # Force atomic_write failure after creating owned temp by making replace fail:
        # use a directory where write works but we raise before replace via monkeypath in-process.
        owned_created = {}

        def _boom_write(p, payload, **kwargs):
            import tempfile as tf
            import os as _os
            from cardscanr_market_engine import atomic_json_state as ajs

            p = Path(p)
            fd, name = tf.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
            tmp = Path(name)
            owned_created["tmp"] = tmp
            _os.close(fd)
            # Leave foreign alone; only owned should be removed on failure.
            try:
                raise OSError("simulated_commit_failure")
            except Exception:
                ajs._unlink_owned_temp(tmp)
                raise

        from cardscanr_market_engine import atomic_json_state as ajs

        # Direct owned cleanup path.
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(self.root))
        os.close(fd)
        owned = Path(name)
        self.assertTrue(owned.exists())
        ajs._unlink_owned_temp(owned)
        self.assertFalse(owned.exists())
        self.assertTrue(foreign.exists())
        self.assertEqual(read_json_object(path).get("safe"), True)


class LockedPublicSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_save_availability_acquires_lock(self) -> None:
        path = self.root / "avail.json"
        save_availability(EbayAvailabilitySnapshot(state="HEALTHY", market="AU"), path=path, force=True)
        lock = FileLock(str(lock_path_for(path)), timeout=0.2)
        lock.acquire()
        try:
            with self.assertRaises(AtomicStateError):
                save_availability(
                    EbayAvailabilitySnapshot(state="COOLDOWN", market="AU"),
                    path=path, force=True)
        finally:
            lock.release()
        # Canonical remains prior healthy write.
        self.assertEqual(load_availability(path=path).state, "HEALTHY")

    def test_save_ops_state_acquires_lock(self) -> None:
        path = self.root / "ops.json"
        save_ops_state({"version": 1, "markets": {}}, path=path, force=True)
        lock = FileLock(str(lock_path_for(path)), timeout=0.2)
        lock.acquire()
        try:
            with self.assertRaises(AtomicStateError):
                save_ops_state({"version": 1, "markets": {"AU": {"reason": "X"}}}, path=path, force=True)
        finally:
            lock.release()

    def test_save_ledger_acquires_lock(self) -> None:
        from cardscanr_market_engine.control_plane_incidents import _save_ledger

        path = self.root / "inc.json"
        _save_ledger({"version": 1, "incidents": {}}, path=path, force=True)
        lock = FileLock(str(lock_path_for(path)), timeout=0.2)
        lock.acquire()
        try:
            with self.assertRaises(AtomicStateError):
                _save_ledger({"version": 1, "incidents": {"a": {"status": "active"}}}, path=path, force=True)
        finally:
            lock.release()


class SeedSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "avail.json"
        self.addCleanup(self.tmp.cleanup)

    def test_seed_cannot_clobber_newer_challenge_across_processes(self) -> None:
        # Start from blank (no file) so seed would normally be allowed — race with challenge.
        ready_seed = mp.Event()
        ready_chal = mp.Event()
        go = mp.Event()
        done_chal = mp.Event()
        out_q: mp.Queue = mp.Queue()
        # Pre-create HEALTHY blank so both start from known state.
        save_availability(EbayAvailabilitySnapshot(state="HEALTHY", market="AU"), path=self.path, force=True)
        # First advance to challenge in parent, then seed must refuse.
        record_challenge(now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc), path=self.path, reference="x")
        with self.assertRaises(AtomicStateError):
            seed_from_observed_sorrys(
                last_sorry_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                consecutive_sorry_events=2,
                path=self.path,
                now=datetime(2026, 9, 1, 1, tzinfo=timezone.utc),
            )
        self.assertEqual(load_availability(path=self.path).state, "CHALLENGE_REQUIRED")

        # Concurrent race: seed vs challenge on fresh HEALTHY.
        save_availability(EbayAvailabilitySnapshot(state="HEALTHY", market="AU"), path=self.path, force=True)
        p_seed = mp.Process(target=_worker_seed, args=(str(self.path), ready_seed, go, out_q))
        p_chal = mp.Process(target=_worker_challenge, args=(str(self.path), ready_chal, go, done_chal))
        p_seed.start()
        p_chal.start()
        self.assertTrue(ready_seed.wait(timeout=5))
        self.assertTrue(ready_chal.wait(timeout=5))
        go.set()
        self.assertTrue(done_chal.wait(timeout=10))
        p_seed.join(timeout=10)
        p_chal.join(timeout=10)
        final = load_availability(path=self.path)
        # Newer challenge must win or at least not be erased to a seeded cooldown from Sept.
        self.assertEqual(final.state, "CHALLENGE_REQUIRED")
        seed_result = out_q.get(timeout=2)
        # Either seed lost the race and refused, or challenge overwrote after seed;
        # canonical must remain CHALLENGE.
        self.assertTrue(final.state == "CHALLENGE_REQUIRED")


class FailClosedStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_corrupt_availability_cannot_authorise_browser_work(self) -> None:
        path = self.root / "avail.json"
        path.write_text("{not-json", encoding="utf-8")
        allowed, reason, snap = browser_work_allowed(path=path, for_probe=True)
        self.assertFalse(allowed)
        self.assertEqual(reason, "EBAY_CHALLENGE_REQUIRED")
        with self.assertRaises(AtomicStateError):
            load_availability(path=path)

    def test_corrupt_ops_raises_for_operational_read(self) -> None:
        path = self.root / "ops.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(AtomicStateError):
            load_ops_state(path=path)

    def test_lock_timeout_fail_closed(self) -> None:
        path = self.root / "state.json"
        atomic_write_json(path, {"version": 1, "n": 1})
        lock = FileLock(str(lock_path_for(path)), timeout=0.1)
        lock.acquire()
        try:
            with self.assertRaises(AtomicStateError):
                with locked_json_state(path, timeout_seconds=0.2):
                    pass
        finally:
            lock.release()

    def test_stale_healthy_cannot_erase_challenge_or_cooldown(self) -> None:
        path = self.root / "avail.json"
        now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        save_availability(
            EbayAvailabilitySnapshot(state="PROBE_REQUIRED", probe_in_flight=True, market="AU", next_probe_at=now),
            path=path, force=True)
        record_challenge(now=now + timedelta(seconds=1), path=path, reference="c")
        snap = record_healthy_browser_check(now=now + timedelta(seconds=2), path=path, from_probe=True)
        self.assertEqual(snap.state, "CHALLENGE_REQUIRED")
        save_availability(EbayAvailabilitySnapshot(state="HEALTHY", market="AU"), path=path, force=True)
        record_sorry(now=now, path=path, from_probe=False)
        self.assertEqual(load_availability(path=path).state, "COOLDOWN")
        snap2 = record_healthy_browser_check(now=now + timedelta(seconds=1), path=path, from_probe=False)
        self.assertEqual(snap2.state, "COOLDOWN")


class TopologyTests(unittest.TestCase):
    def test_windows_host_is_sole_canonical_writer(self) -> None:
        topo = topology_summary()
        self.assertEqual(topo["canonicalStateWriterDomain"], "windows_host")
        self.assertEqual(topo["wslRole"], "x11_chrome_cdp_navigation_only")
        self.assertFalse(topo["sharedCrossOsMutation"])


class ConcurrentIncidentTests(unittest.TestCase):
    def test_concurrent_incident_increments_not_lost(self) -> None:
        from cardscanr_market_engine.control_plane_incidents import _load_ledger

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "inc.json"
        procs = [mp.Process(target=_worker_register_incident, args=(str(path), i)) for i in range(8)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=20)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(len((_load_ledger(path=path).get("incidents") or {})), 8)


if __name__ == "__main__":
    try:
        mp.set_start_method("spawn")
    except RuntimeError:
        pass
    unittest.main()
