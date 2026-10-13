#!/usr/bin/env python3
"""Drive conservative staging acquire until 10k validated unique printings or stop conditions.

Coordinates a single parent driver + one child acquire process. Child holds
staging acquire.pid; this driver holds toward_10k.pid so competing drivers
cannot spawn duplicate downloaders / rate limiters.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
STAGING = Path(r"D:\CardScanR_Archive\image_staging\global_acq_20261013")
REPORT = ROOT / "reports" / "global_image_acquisition"
TARGET = 10_000
BATCH = 2500
# Keep rates conservative; never raise to chase the milestone.
DEFAULT_CONCURRENCY = "6"
DEFAULT_MIN_INTERVAL = "0.15"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def count_validated() -> int:
    if not STAGING.exists():
        return 0
    n = 0
    for lang in STAGING.iterdir():
        if not lang.is_dir() or lang.name.startswith("_"):
            continue
        n += sum(1 for _ in lang.rglob("display.webp"))
    return n


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_driver_lock() -> Path:
    STAGING.mkdir(parents=True, exist_ok=True)
    lock_path = STAGING / "toward_10k.pid"
    if lock_path.exists():
        try:
            old = int(lock_path.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            old = 0
        if old and _pid_alive(old) and old != os.getpid():
            raise SystemExit(f"another toward_10k driver holds {lock_path} (pid={old})")
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    return lock_path


def main() -> int:
    # Conservative wall: enough for ~10k at gated rates with headroom, but not indefinite.
    max_wall = int(os.environ.get("CARDSCANR_ACQ_MAX_WALL_SEC", str(90 * 60)))
    concurrency = os.environ.get("CARDSCANR_ACQ_CONCURRENCY", DEFAULT_CONCURRENCY)
    min_interval = os.environ.get("CARDSCANR_ACQ_MIN_INTERVAL", DEFAULT_MIN_INTERVAL)
    started = time.time()
    history = []
    REPORT.mkdir(parents=True, exist_ok=True)
    lock_path = acquire_driver_lock()
    try:
        return _run_batches(
            max_wall=max_wall,
            started=started,
            history=history,
            concurrency=concurrency,
            min_interval=min_interval,
        )
    finally:
        if lock_path.exists() and lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock_path.unlink()


def _run_batches(
    *,
    max_wall: int,
    started: float,
    history: list,
    concurrency: str,
    min_interval: str,
) -> int:
    while True:
        current = count_validated()
        history.append({"at": utc_now(), "validated": current})
        print(f"validated={current} target={TARGET}", flush=True)
        if current >= TARGET:
            break
        if time.time() - started >= max_wall:
            print("stop: wall_clock_budget", flush=True)
            break
        need = TARGET - current
        limit = min(BATCH, max(need + 50, 100))
        cmd = [
            sys.executable,
            "-u",
            str(TOOLS / "global_image_staging_acquire.py"),
            "--execute",
            "--exclude-languages",
            "en,ja",
            "--concurrency",
            concurrency,
            "--min-interval",
            min_interval,
            "--limit",
            str(limit),
        ]
        print("+", " ".join(cmd), flush=True)
        code = subprocess.call(cmd, cwd=str(ROOT))
        if code != 0:
            print(f"acquire_exit={code}", flush=True)
            # Continue unless lock conflict repeatedly
            time.sleep(5)
        # Light pause between batches
        time.sleep(2)
        if count_validated() == current:
            # No progress — likely queue exhausted or paused
            print("stop: no_progress_batch", flush=True)
            break
    final = count_validated()
    payload = {
        "generatedAtUtc": utc_now(),
        "target": TARGET,
        "finalValidated": final,
        "reachedTarget": final >= TARGET,
        "history": history,
        "elapsedSeconds": round(time.time() - started, 1),
        "concurrency": concurrency,
        "minInterval": min_interval,
        "driverPid": os.getpid(),
    }
    (REPORT / "toward_10k_run.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
