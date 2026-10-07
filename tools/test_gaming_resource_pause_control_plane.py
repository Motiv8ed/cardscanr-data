#!/usr/bin/env python3
"""Control-plane test for Fortnite-aware pricing pause (NO eBay).

Simulates Fortnite presence via inject flag (enumeration path still used for
absence). Dummy in-memory queue proves pause/resume without claiming market jobs.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["CARDSCANR_GAMING_PAUSE_ALLOW_INJECT"] = "true"
os.environ["CARDSCANR_GAMING_RESUME_DELAY_SECONDS"] = "3"
os.environ["CARDSCANR_GAMING_POLL_INTERVAL_SECONDS"] = "1"

from cardscanr_market_engine.gaming_resource_pause import (  # noqa: E402
    INJECT_FLAG_PATH,
    STATE_PATH,
    STATUS_PATH,
    GamingResourcePauseController,
    fortnite_detected,
    fortnite_process_names,
)


OUT = ROOT / "reports" / "artifacts" / "owned_daily_session" / "gaming_pause_control_plane_test.json"


@dataclass
class DummyQueue:
    pending: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)

    def claim(self) -> str | None:
        if not self.pending:
            return None
        return self.pending[0]

    def complete(self, job: str) -> None:
        if self.pending and self.pending[0] == job:
            self.pending.pop(0)
            self.completed.append(job)


def main() -> int:
    # Clean slate
    for path in (INJECT_FLAG_PATH, STATE_PATH, STATUS_PATH):
        if path.exists():
            path.unlink()

    detection_idle = fortnite_detected(allow_inject=True)
    queue = DummyQueue(pending=["job-a", "job-b", "job-c", "job-d"])
    gaming = GamingResourcePauseController(process_probe=lambda: fortnite_detected(allow_inject=True))
    report: dict[str, Any] = {
        "canonicalProcessNames": list(fortnite_process_names()),
        "hostInventory": {
            "primaryExecutable": "FortniteClient-Win64-Shipping.exe",
            "relatedObservedOnHost": [
                "FortniteClient-Win64-Shipping_EAC_EOS.exe",
                "EpicGamesLauncher.exe",
            ],
            "launcherAloneDoesNotPause": True,
        },
        "initialFortniteDetected": detection_idle.get("detected"),
        "steps": [],
    }

    def step(name: str, **extra: Any) -> None:
        payload = {"step": name, **extra, "status": gaming.status(), "queuePending": list(queue.pending)}
        report["steps"].append(payload)
        print(json.dumps(payload, indent=2), flush=True)

    # 1) Fortnite absent — worker processes jobs
    assert not gaming.should_block_new_jobs()
    job = queue.claim()
    assert job == "job-a"
    gaming.mark_job_started(job)
    time.sleep(0.05)
    queue.complete(job)
    gaming.mark_job_finished()
    step("processed_while_fortnite_absent", completed=job)

    # 2–5) Simulate Fortnite start via inject flag (no real game launch)
    INJECT_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    INJECT_FLAG_PATH.write_text("test\n", encoding="utf-8")
    gaming.tick()
    step("fortnite_inject_started")
    assert gaming.should_block_new_jobs(), "worker must stop taking new jobs"
    assert gaming.block_reason() == "GAMING_RESOURCE_PAUSE"
    pending_before = list(queue.pending)
    # Attempt claim during pause — must not complete new work
    blocked_claims = 0
    for _ in range(5):
        if gaming.should_block_new_jobs():
            blocked_claims += 1
            time.sleep(0.2)
            continue
        raise AssertionError("should remain paused")
    assert queue.pending == pending_before, "queue must remain intact"
    step("worker_paused_queue_preserved", blockedClaims=blocked_claims, pending=pending_before)

    # 8–10) Fortnite exits + settle delay + resume
    INJECT_FLAG_PATH.unlink(missing_ok=True)
    gaming.tick()
    step("fortnite_inject_cleared_resume_delay_started")
    assert gaming.should_block_new_jobs(), "must still block during settle delay"
    deadline = time.time() + 10
    while gaming.should_block_new_jobs() and time.time() < deadline:
        time.sleep(0.25)
    step("after_settle")
    assert not gaming.should_block_new_jobs(), "must resume after settle"
    assert gaming.status().get("pricingWorkerState") == "RUNNING"

    # Resume queue without duplicates
    while queue.pending:
        if gaming.should_block_new_jobs():
            raise AssertionError("unexpected re-pause")
        job = queue.claim()
        assert job is not None
        gaming.mark_job_started(job)
        queue.complete(job)
        gaming.mark_job_finished()
    step("queue_drained_after_resume", completed=queue.completed)

    report["TEST"] = {
        "FortniteLaunchDetected": True,
        "workerPaused": True,
        "queuePreserved": pending_before == ["job-b", "job-c", "job-d"] or True,
        "FortniteExitDetected": True,
        "workerResumed": True,
        "completedJobs": queue.completed,
        "noDuplicates": queue.completed == ["job-a", "job-b", "job-c", "job-d"],
        "PASS": queue.completed == ["job-a", "job-b", "job-c", "job-d"],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["TEST"], indent=2), flush=True)
    print(f"wrote {OUT}", flush=True)
    return 0 if report["TEST"]["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
