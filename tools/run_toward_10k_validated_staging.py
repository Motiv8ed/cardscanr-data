#!/usr/bin/env python3
"""Drive conservative staging acquire until 10k validated unique printings or stop conditions."""

from __future__ import annotations

import json
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
BATCH = 1500


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def count_validated() -> int:
    if not STAGING.exists():
        return 0
    n = 0
    for asset in STAGING.rglob("asset.json"):
        if "_quarantine" in asset.parts:
            continue
        if not (asset.parent / "display.webp").exists():
            continue
        n += 1
    return n


def main() -> int:
    # Conservative wall: enough for ~10k at ~2.5 req/s with headroom, but not indefinite.
    max_wall = int(__import__("os").environ.get("CARDSCANR_ACQ_MAX_WALL_SEC", str(90 * 60)))
    started = time.time()
    history = []
    REPORT.mkdir(parents=True, exist_ok=True)
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
            "5",
            "--min-interval",
            "0.22",
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
    }
    (REPORT / "toward_10k_run.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
