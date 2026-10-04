#!/usr/bin/env python3
"""Enable normal continuous AU owned_daily (no proof-batch cap)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.continuous_au_status import write_continuous_status
from cardscanr_market_engine.continuous_worker_policy import classify_continuous_gate
from cardscanr_market_engine.demand_aware_policy import DemandAwarePolicy
from cardscanr_market_engine.owned_daily_enablement import (
    FLAG_PATH,
    apply_continuous_au_env,
    write_owned_daily_flag,
)
from cardscanr_market_engine.owned_daily_scheduler import (
    OwnedDailySchedulerConfig,
    OwnedPrintingRefreshScheduler,
)
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient

REQUIRED_COMMITS = (
    "86973b7cd6dfddc0643492aac98b315be54c0ea8",
    "d95f5482cb8b8f2880003776e4a38ade30b5bc12",
    "cfdce5a68870d475d06ef4e251c3bb6e1de053f2",
    "8330e65e8d06e0aef66a6d98901d111a2cc82ca1",
    "5a3b5c3c",
)
LOG_DIR = ROOT / "reports" / "runtime"
SCHED_LOG = LOG_DIR / "owned_daily_continuous_scheduler.log"
WORKER_LOG = LOG_DIR / "owned_daily_continuous_worker.log"


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def deployment_ok() -> dict:
    head = _git("rev-parse", "HEAD")
    present = {}
    for commit in REQUIRED_COMMITS:
        rc = subprocess.call(["git", "merge-base", "--is-ancestor", commit, "HEAD"], cwd=ROOT)
        present[commit] = rc == 0
    dirty = _git("status", "--porcelain", "--", "cardscanr_market_engine", "workers")
    return {
        "ok": all(present.values()) and not dirty,
        "HEAD": head,
        "requiredCommits": present,
        "workingTree": dirty or "(clean)",
    }


def policy_audit() -> dict:
    cfg = DemandAwarePolicy.from_env()
    checks = {
        "hotTtl": cfg.hot_verified_ttl_hours == 12,
        "normalTtl": cfg.normal_verified_ttl_hours == 24,
        "highDemand": cfg.high_min_requests_24h == 3,
        "lanes": (
            abs(cfg.lane_demand_share - 0.5) < 1e-9
            and abs(cfg.lane_stale_share - 0.3) < 1e-9
            and abs(cfg.lane_coverage_share - 0.2) < 1e-9
        ),
        "concurrencyOne": cfg.canary_concurrency == 1,
    }
    return {"ok": all(checks.values()), "checks": checks, "policy": cfg.to_dict()}


def competing_workers() -> list[dict]:
    out: list[dict] = []
    try:
        raw = subprocess.check_output(
            ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return out
    for line in raw.splitlines():
        if "owned_daily_price_scheduler" in line or "market_price_worker" in line:
            out.append({"cmd": line.strip()[:240]})
    return out


def main() -> int:
    apply_continuous_au_env()
    deploy = deployment_ok()
    policy = policy_audit()
    gate = classify_continuous_gate(market="AU")
    precheck = {
        "deployment": deploy,
        "policyOk": bool(policy.get("ok")),
        "stateIntegrity": bool(gate.get("stateIntegrityOk")),
        "activeChallenges": int(gate.get("activeChallenges") or 0),
        "availability": gate.get("availabilityState"),
        "PRE_SUBMIT_ONLY": os.environ.get("PRE_SUBMIT_ONLY"),
        "competing": competing_workers(),
    }
    (LOG_DIR / "continuous_au_precheck.json").parent.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "continuous_au_precheck.json").write_text(
        json.dumps(precheck, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps({"PRECHECK": precheck}, indent=2, default=str), flush=True)
    if not deploy["ok"]:
        print("BLOCKED: deployment/worktree", flush=True)
        return 2
    if not policy.get("ok"):
        print("BLOCKED: policy", flush=True)
        return 2
    if not gate.get("stateIntegrityOk") or int(gate.get("activeChallenges") or 0) > 0:
        print("BLOCKED: hard marketplace condition", flush=True)
        return 2
    if precheck["competing"]:
        print("BLOCKED: competing pricing worker/scheduler already running", flush=True)
        return 2

    write_owned_daily_flag(True)
    apply_continuous_au_env()

    cfg = OwnedDailySchedulerConfig.from_env()
    client = SupabaseMarketEngineClient(
        supabase_url=cfg.supabase_url,
        service_role_key=cfg.supabase_service_role_key,
    )
    first = OwnedPrintingRefreshScheduler(client=client, config=cfg).run_and_write_reports()
    summary = first.get("summary") or {}
    enqueued = first.get("enqueuedJobs") or first.get("jobs") or []
    if not enqueued and isinstance(first.get("enqueued"), list):
        enqueued = first.get("enqueued")
    # latest report has enqueued jobs
    latest = {}
    try:
        latest = json.loads(cfg.latest_report_path.read_text(encoding="utf-8"))
        enqueued = latest.get("enqueuedJobs") or latest.get("jobsEnqueuedDetail") or enqueued
        summary = latest.get("summary") or summary
    except Exception:
        pass

    env = os.environ.copy()
    env.update(apply_continuous_au_env())
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    sched_out = open(SCHED_LOG, "a", encoding="utf-8")
    work_out = open(WORKER_LOG, "a", encoding="utf-8")
    sched = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "workers" / "owned_daily_price_scheduler.py"), "--no-sync-keys"],
        cwd=str(ROOT),
        env=env,
        stdout=sched_out,
        stderr=subprocess.STDOUT,
    )
    worker = subprocess.Popen(
        [sys.executable, "-u", str(ROOT / "workers" / "market_price_worker.py"), "--max-jobs", "1"],
        cwd=str(ROOT),
        env=env,
        stdout=work_out,
        stderr=subprocess.STDOUT,
    )
    time.sleep(3)
    sched_alive = sched.poll() is None
    worker_alive = worker.poll() is None
    write_continuous_status(
        {
            "enabled": True,
            "market": "AU",
            "workerState": gate.get("workerState") or "WAITING",
            "cooldownUntil": gate.get("cooldownUntil"),
            "nextProbeAt": gate.get("nextProbeAt"),
            "activeChallenges": gate.get("activeChallenges") or 0,
            "schedulerPid": sched.pid,
            "workerPid": worker.pid,
        }
    )
    result = {
        "enabled": True,
        "schedulerPid": sched.pid,
        "workerPid": worker.pid,
        "schedulerRunning": sched_alive,
        "workerRunning": worker_alive,
        "flag": FLAG_PATH.read_text(encoding="utf-8").strip(),
        "firstCycle": {
            "targets": summary.get("targetsScanned"),
            "due": summary.get("keysEligible"),
            "enqueued": summary.get("jobsEnqueued"),
            "freshSkipped": summary.get("jobsSkippedFresh") or summary.get("keysSkippedFresh"),
        },
        "gate": gate,
        "HEAD": deploy["HEAD"],
    }
    (LOG_DIR / "continuous_au_enable.json").write_text(
        json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps({"ENABLED": result}, indent=2, default=str), flush=True)
    return 0 if sched_alive and worker_alive else 1


if __name__ == "__main__":
    raise SystemExit(main())
