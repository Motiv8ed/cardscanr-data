#!/usr/bin/env python3
"""CardScanR eBay pricing operational health report.

Answers: is pricing working right now? Per-marketplace AUTH/CHALLENGE/HEALTHY
status, stuck-queue detection, worker/scheduler liveness.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import REPORTS_DIR, supabase_secret_key_from_env
from cardscanr_market_engine.marketplace_ops_state import (
    SUPPORTED_LIVE_MARKETS,
    classify_provider_failure,
    list_active_cooldowns,
    parse_utc,
    utc_iso,
    utc_now,
)
from cardscanr_market_engine.smoke_utils import write_json
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient

STUCK_QUEUE_MINUTES = int(os.getenv("MARKET_HEALTH_STUCK_QUEUE_MINUTES", "45"))
SCHEDULER_STALE_MINUTES = int(os.getenv("MARKET_HEALTH_SCHEDULER_STALE_MINUTES", "45"))
NO_SUCCESS_HOURS = float(os.getenv("MARKET_HEALTH_NO_SUCCESS_HOURS", "18"))
RECENT_WINDOW_HOURS = float(os.getenv("MARKET_HEALTH_RECENT_WINDOW_HOURS", "24"))


def _count_python_workers(needle: str) -> list[int]:
    try:
        import subprocess

        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
                    f"Where-Object {{ $_.CommandLine -like '*{needle}*' }} | "
                    "Select-Object -ExpandProperty ProcessId"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return [int(line.strip()) for line in (completed.stdout or "").splitlines() if line.strip().isdigit()]
    except Exception:
        return []


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _scheduled_task_enabled() -> bool | None:
    try:
        import subprocess

        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-ScheduledTask -TaskName 'CardScanR-LiveEbayPricingRuntime' -ErrorAction SilentlyContinue).State",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        text = (completed.stdout or "").strip()
        if not text:
            return None
        return text.lower() in {"ready", "running"}
    except Exception:
        return None


def _market_from_fingerprint_or_key(row: dict, keys_by_id: dict[str, dict]) -> str:
    key = keys_by_id.get(str(row.get("price_key_id") or ""))
    if key:
        return str(key.get("market_country") or "").upper()
    return ""


def build_health_report(*, client: SupabaseMarketEngineClient, now: datetime | None = None) -> dict:
    current = now or utc_now()
    recent_start = current - timedelta(hours=RECENT_WINDOW_HOURS)
    recent_iso = utc_iso(recent_start)

    jobs = client._table_get(  # noqa: SLF001
        "market_price_refresh_jobs",
        params={
            "select": "id,status,reason,error_message,requested_at,started_at,completed_at,price_key_id,created_snapshot_id",
            "order": "requested_at.desc",
            "limit": "800",
        },
    )
    if not isinstance(jobs, list):
        jobs = []

    cache_rows = client._table_get(  # noqa: SLF001
        "market_price_cache",
        params={
            "select": (
                "price_key_id,recommended_price,last_updated_at,stale_after,marketplace,refresh_status,"
                "last_error_message,market_price_keys!inner(id,market_country,currency,fingerprint)"
            ),
            "limit": "500",
        },
    )
    if not isinstance(cache_rows, list):
        cache_rows = []

    keys_by_id: dict[str, dict] = {}
    for row in cache_rows:
        key = row.get("market_price_keys") or {}
        if isinstance(key, list):
            key = key[0] if key else {}
        if isinstance(key, dict) and key.get("id"):
            keys_by_id[str(key["id"])] = key

    # Fill missing key metadata for recent jobs.
    missing_ids = sorted(
        {
            str(j.get("price_key_id"))
            for j in jobs
            if j.get("price_key_id") and str(j.get("price_key_id")) not in keys_by_id
        }
    )[:100]
    for key_id in missing_ids:
        rows = client._table_get(  # noqa: SLF001
            "market_price_keys",
            params={"select": "id,market_country,currency,fingerprint", "id": f"eq.{key_id}", "limit": "1"},
        )
        if rows:
            keys_by_id[key_id] = rows[0]

    queued = [j for j in jobs if j.get("status") == "queued"]
    running = [j for j in jobs if j.get("status") == "running"]
    completed_recent = [
        j
        for j in jobs
        if j.get("status") == "completed" and str(j.get("completed_at") or "") >= recent_iso
    ]
    failed_recent = [
        j for j in jobs if j.get("status") == "failed" and str(j.get("completed_at") or "") >= recent_iso
    ]

    cooldowns = list_active_cooldowns(now=current)
    allowed_raw = os.getenv("MARKET_SCHEDULER_ALLOWED_MARKETS") or os.getenv("MARKET_WORKER_ALLOWED_MARKETS") or "AU,US,GB,CA"
    enabled_markets = {
        part.strip().upper()
        for part in allowed_raw.split(",")
        if part.strip() and part.strip().upper() not in {"NONE", "OFF"}
    } or set(SUPPORTED_LIVE_MARKETS)

    markets: dict[str, dict] = {}
    for market in SUPPORTED_LIVE_MARKETS:
        market_jobs_completed = []
        market_jobs_failed = []
        market_auth = []
        market_challenge = []
        last_attempt = None
        for job in jobs:
            m = _market_from_fingerprint_or_key(job, keys_by_id)
            if m != market:
                continue
            stamp = str(job.get("completed_at") or job.get("started_at") or job.get("requested_at") or "")
            if stamp and (last_attempt is None or stamp > last_attempt):
                last_attempt = stamp
            if job.get("status") == "completed" and str(job.get("completed_at") or "") >= recent_iso:
                market_jobs_completed.append(job)
            if job.get("status") == "failed" and str(job.get("completed_at") or "") >= recent_iso:
                market_jobs_failed.append(job)
                kind = classify_provider_failure(job.get("error_message"))
                if kind == "AUTH_REQUIRED":
                    market_auth.append(job)
                elif kind == "CHALLENGE_REQUIRED":
                    market_challenge.append(job)

        last_success = None
        successes = []
        for row in cache_rows:
            key = row.get("market_price_keys") or {}
            if isinstance(key, list):
                key = key[0] if key else {}
            if str((key or {}).get("market_country") or "").upper() != market:
                continue
            if row.get("last_updated_at"):
                successes.append(str(row.get("last_updated_at")))
        last_success = max(successes) if successes else None

        cooldown = cooldowns.get(market)
        enabled = market in enabled_markets
        last_auth_at = max((str(j.get("completed_at") or "") for j in market_auth), default=None)
        last_challenge_at = max((str(j.get("completed_at") or "") for j in market_challenge), default=None)

        health = "DEFERRED"
        if not enabled:
            health = "DEFERRED"
        elif cooldown is not None:
            health = cooldown.reason if cooldown.reason in {"AUTH_REQUIRED", "CHALLENGE_REQUIRED"} else "DEFERRED"
        else:
            # A newer successful refresh clears older auth/challenge noise from the window.
            auth_blocks = bool(last_auth_at and (not last_success or last_auth_at > last_success))
            challenge_blocks = bool(last_challenge_at and (not last_success or last_challenge_at > last_success))
            if challenge_blocks:
                health = "CHALLENGE_REQUIRED"
            elif auth_blocks:
                health = "AUTH_REQUIRED"
            elif last_success:
                success_dt = parse_utc(last_success)
                if success_dt and success_dt >= (current - timedelta(hours=NO_SUCCESS_HOURS)):
                    health = "HEALTHY"
                else:
                    health = "NO_RECENT_SUCCESS"
            elif market_jobs_failed:
                health = "ERROR"
            else:
                health = "NO_RECENT_SUCCESS"

        markets[market] = {
            "enabled": enabled,
            "health": health,
            "lastAttemptedLookupAt": last_attempt,
            "lastSuccessfulLookupAt": last_success,
            "lastAuthFailureAt": last_auth_at,
            "lastChallengeFailureAt": last_challenge_at,
            "recentSuccessfulJobs": len(market_jobs_completed),
            "recentFailedJobs": len(market_jobs_failed),
            "authFailuresRecent": len(market_auth),
            "challengeFailuresRecent": len(market_challenge),
            "cooldown": cooldown.to_dict() if cooldown else None,
        }

    worker_pids = _count_python_workers("workers/market_price_worker.py")
    scheduler_pids = _count_python_workers("workers/market_price_scheduler.py")
    scheduler_latest = _read_json(REPORTS_DIR / "market_price_scheduler_latest.json")
    worker_latest = _read_json(REPORTS_DIR / "market_price_worker_latest.json")

    oldest_queued_at = min((str(j.get("requested_at") or "") for j in queued), default=None)
    oldest_age_minutes = None
    if oldest_queued_at:
        oldest_dt = parse_utc(oldest_queued_at)
        if oldest_dt:
            oldest_age_minutes = round((current - oldest_dt).total_seconds() / 60.0, 1)

    last_completed_at = max(
        (str(j.get("completed_at") or "") for j in jobs if j.get("status") in {"completed", "failed"} and j.get("completed_at")),
        default=None,
    )
    last_success_global = max(
        (str(m.get("lastSuccessfulLookupAt") or "") for m in markets.values() if m.get("lastSuccessfulLookupAt")),
        default=None,
    )

    stuck = False
    if queued or running:
        # Concurrency-1 can leave items queued for a while; only flag when there is
        # no terminal progress for an abnormal window while work remains.
        progress_dt = parse_utc(last_completed_at)
        if progress_dt is None or progress_dt < (current - timedelta(minutes=STUCK_QUEUE_MINUTES)):
            if (oldest_age_minutes or 0) >= STUCK_QUEUE_MINUTES or running:
                stuck = True

    scheduler_finished = parse_utc((scheduler_latest or {}).get("finishedAtUtc"))
    scheduler_recent = bool(
        scheduler_pids
        and scheduler_finished
        and scheduler_finished >= (current - timedelta(minutes=SCHEDULER_STALE_MINUTES))
    )
    # Fresh process without a finished report yet still counts if started recently via runtime pid file.
    if scheduler_pids and not scheduler_recent:
        runtime_sched = _read_json(REPORTS_DIR / "runtime" / "live_ebay_scheduler.pid.json")
        started = parse_utc((runtime_sched or {}).get("startedAtUtc"))
        if started and started >= (current - timedelta(minutes=SCHEDULER_STALE_MINUTES)):
            scheduler_recent = True

    healthy_markets = [m for m, row in markets.items() if row.get("enabled") and row.get("health") == "HEALTHY"]
    enabled_list = [m for m in SUPPORTED_LIVE_MARKETS if markets[m]["enabled"]]
    auth_challenge_markets = [
        m for m in enabled_list if markets[m]["health"] in {"AUTH_REQUIRED", "CHALLENGE_REQUIRED"}
    ]
    problem_markets = [
        m
        for m in enabled_list
        if markets[m]["health"] in {"AUTH_REQUIRED", "CHALLENGE_REQUIRED", "NO_RECENT_SUCCESS", "ERROR"}
    ]

    worker_ok = len(worker_pids) == 1
    scheduler_ok = len(scheduler_pids) == 1 and scheduler_recent

    if not worker_ok or not scheduler_ok or stuck or not healthy_markets:
        overall = "DOWN"
        pricing_working_now = False
    elif problem_markets:
        overall = "DEGRADED"
        pricing_working_now = True
    else:
        overall = "HEALTHY"
        pricing_working_now = True

    summary = " | ".join(f"{market} {markets[market]['health']}" for market in SUPPORTED_LIVE_MARKETS)

    return {
        "status": "success",
        "checkedAtUtc": utc_iso(current),
        "pricingWorkingNow": pricing_working_now,
        "overall": overall,
        "summary": summary,
        "worker": {
            "healthy": worker_ok,
            "pids": worker_pids,
            "duplicate": len(worker_pids) > 1,
            "latestCycle": {
                "cycle": (worker_latest or {}).get("cycle"),
                "finishedAtUtc": (worker_latest or {}).get("finishedAtUtc"),
                "jobCount": (worker_latest or {}).get("jobCount"),
            },
        },
        "scheduler": {
            "healthy": scheduler_ok,
            "pids": scheduler_pids,
            "duplicate": len(scheduler_pids) > 1,
            "recentlyRan": scheduler_recent,
            "latestRun": {
                "startedAtUtc": (scheduler_latest or {}).get("startedAtUtc"),
                "finishedAtUtc": (scheduler_latest or {}).get("finishedAtUtc"),
                "jobsEnqueued": ((scheduler_latest or {}).get("summary") or {}).get("jobsEnqueued"),
                "allowedMarkets": ((scheduler_latest or {}).get("limits") or {}).get("allowedMarkets"),
                "dryRun": (scheduler_latest or {}).get("dryRun"),
            },
        },
        "queue": {
            "depth": len(queued),
            "running": len(running),
            "oldestQueuedAt": oldest_queued_at,
            "oldestQueuedAgeMinutes": oldest_age_minutes,
            "lastJobCompletedAt": last_completed_at,
            "lastJobClaimedAt": max((str(j.get("started_at") or "") for j in jobs if j.get("started_at")), default=None),
            "successfulRecent": len(completed_recent),
            "failedRecent": len(failed_recent),
            "stuck": stuck,
            "stuckThresholdMinutes": STUCK_QUEUE_MINUTES,
        },
        "lastSuccessfulPriceRefreshAt": last_success_global,
        "markets": markets,
        "recovery": {
            "scheduledTaskEnabled": _scheduled_task_enabled(),
            "scheduledTaskName": "CardScanR-LiveEbayPricingRuntime",
            "ensureScript": "scripts/ensure_live_ebay_pricing_runtime.ps1",
            "singleWorker": len(worker_pids) <= 1,
            "singleScheduler": len(scheduler_pids) <= 1,
        },
        "ownerAlert": {
            "configured": False,
            "note": "No existing CardScanR owner-alert channel reused; inspect reports/ebay_pricing_health_latest.json",
        },
        "adminDashboard": {
            "integrated": False,
            "note": "Use the health report/command; dashboard integration deferred as disproportionate for this hardening task",
        },
        "howToCheck": {
            "command": ".\\scripts\\report_ebay_pricing_health.ps1",
            "reportPath": str(REPORTS_DIR / "ebay_pricing_health_latest.json"),
        },
    }


def main() -> int:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = supabase_secret_key_from_env()
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SECRET_KEY are required")
    client = SupabaseMarketEngineClient(supabase_url=url, service_role_key=key)
    report = build_health_report(client=client)
    out = REPORTS_DIR / "ebay_pricing_health_latest.json"
    write_json(out, report)
    print(json.dumps(report, indent=2))
    print(f"[health] wrote {out}")
    print(f"[health] pricingWorkingNow={report['pricingWorkingNow']} overall={report['overall']}")
    print(f"[health] {report['summary']}")
    return 0 if report.get("pricingWorkingNow") else 2


if __name__ == "__main__":
    raise SystemExit(main())
