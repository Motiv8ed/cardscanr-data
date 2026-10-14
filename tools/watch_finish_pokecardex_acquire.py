#!/usr/bin/env python3
"""Wait for the existing PokéCardex acquire PID to exit, scrub false 404s, resume once.

Does not start a second downloader while the lock/PID is alive.
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
STAGING = Path(r"D:\CardScanR_Archive\image_staging\pokecardex_zh_hans_20261014")
LOCK = STAGING / "acquire.pid"
PROGRESS = STAGING / "progress.jsonl"
STATUS = ROOT / "reports" / "global_image_acquisition" / "pokecardex" / "watch_finish_status.json"
EXPECTED_PID = 52952


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pid_alive(pid: int) -> bool:
    """True if process exists. Windows: os.kill(pid, 0) is unreliable (WinError 87)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def progress_counts() -> dict[str, int]:
    from collections import Counter

    c: Counter[str] = Counter()
    if PROGRESS.exists():
        for line in PROGRESS.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                c[json.loads(line).get("status")] += 1
            except json.JSONDecodeError:
                continue
    return dict(c)


def write_status(**extra: object) -> None:
    payload = {
        "at": utc_now(),
        "progress": progress_counts(),
        "webp": sum(1 for _ in STAGING.rglob("display.webp")) if STAGING.exists() else 0,
        **extra,
    }
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload), flush=True)


def main() -> int:
    write_status(phase="waiting_for_pid", expectedPid=EXPECTED_PID)
    # Wait until expected PID dead AND lock free or stale
    while True:
        lock_pid = 0
        if LOCK.exists():
            try:
                lock_pid = int(LOCK.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                lock_pid = 0
        alive_expected = pid_alive(EXPECTED_PID)
        alive_lock = pid_alive(lock_pid) if lock_pid else False
        write_status(
            phase="waiting_for_pid",
            expectedPid=EXPECTED_PID,
            expectedAlive=alive_expected,
            lockPid=lock_pid,
            lockAlive=alive_lock,
        )
        if not alive_expected and not alive_lock:
            break
        time.sleep(45)

    # Clear stale lock if present
    if LOCK.exists() and not pid_alive(int(LOCK.read_text(encoding="utf-8").strip() or "0")):
        LOCK.unlink()

    write_status(phase="scrubbing_cs1ac_false_404")
    scrub = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "scrub_pokecardex_false_404.py"), "--sets", "CS1AC"],
        cwd=str(ROOT),
        check=False,
    )
    if scrub.returncode != 0:
        write_status(phase="scrub_failed", returncode=scrub.returncode)
        return scrub.returncode

    write_status(phase="resuming_acquire")
    resume = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "pokecardex_zh_hans_acquire.py"),
            "--execute",
            "--standard-only",
            "--min-interval",
            "0.45",
            "--concurrency",
            "2",
        ],
        cwd=str(ROOT),
        check=False,
    )
    write_status(phase="resume_finished", returncode=resume.returncode)

    # Post steps
    for script in (
        "crosswalk_pokecardex_tcgdex.py",
        "reconcile_pokecardex_zh_hans.py",
        "validate_pokecardex_zh_hans_images.py",
        "pack_pokecardex_evidence_zip.py",
    ):
        subprocess.run([sys.executable, str(ROOT / "tools" / script)], cwd=str(ROOT), check=False)
    write_status(phase="complete")
    return resume.returncode


if __name__ == "__main__":
    raise SystemExit(main())
