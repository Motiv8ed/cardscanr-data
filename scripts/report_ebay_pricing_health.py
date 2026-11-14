#!/usr/bin/env python3
"""CardScanR eBay pricing operational health report.

Answers: is pricing working right now? Per-marketplace AUTH/CHALLENGE/HEALTHY
status, stuck-queue detection, worker/scheduler liveness.

Worker fields (backward compatible; stricter semantics after #33 follow-ups):
- worker.healthy: true only when a single worker PID is present, local status/ledger
  evidence is fresh, the DB pipeline heartbeat is fresh and not in a gated state, and
  worker.gateState is not blocking (HARD_STOP, BUDGET_WAIT, COOLDOWN, etc.).
- worker.runtimeEvidence: fresh|stale|missing for status/ledger files with ages.
- pricingWorkingNow: false whenever worker.healthy is false or overall is DOWN.
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
from cardscanr_market_engine.owned_daily_enablement import BUDGET_LEDGER_PATH, STATUS_PATH
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
RUNTIME_EVIDENCE_STALE_MINUTES = int(os.getenv("MARKET_HEALTH_RUNTIME_STALE_MINUTES", "10"))
RUNTIME_EVIDENCE_FUTURE_TOLERANCE_MINUTES = float(
    os.getenv("MARKET_HEALTH_RUNTIME_FUTURE_TOLERANCE_MINUTES", "2")
)
DB_HEARTBEAT_STALE_MINUTES = int(os.getenv("MARKET_HEALTH_DB_HEARTBEAT_STALE_MINUTES", "10"))

# Worker local status / ledger gates that mean the process is alive but not claiming pricing jobs.
# worker.healthy=false for these; pricingWorkingNow stays false until the gate clears.
_WORKER_GATE_STATES_BLOCKING = frozenset(
    {
        "HARD_STOP",
        "BUDGET_WAIT",
        "COOLDOWN",
        "TRANSIENT_HALT",
        "BROWSER_HALTED",
        "PROBE_REQUIRED",
        "NOT_RUNNING",
    }
)
# Supabase pipeline heartbeat states that imply the worker is not actively pricing.
_DB_HEARTBEAT_GATED_STATES = frozenset(
    {
        "hard_stop",
        "budget_wait",
        "cooldown",
        "transient_halt",
        "browser_halted",
    }
)


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


def _normalize_gate_state(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text.upper().replace("-", "_")


def _worker_gate_blocks_pricing(gate_state: str | None) -> bool:
    normalized = _normalize_gate_state(gate_state)
    return normalized in _WORKER_GATE_STATES_BLOCKING if normalized else False


def _runtime_file_evidence(
    payload: dict | None,
    *,
    path: Path,
    now: datetime,
) -> tuple[str, float | None]:
    """Return freshness label (fresh|stale|missing) and age in minutes when known."""
    if payload is None:
        if not path.exists():
            return "missing", None
        return "missing", None
    updated = parse_utc(payload.get("updatedAtUtc"))
    if updated is None:
        return "stale", None
    age_minutes = (now - updated).total_seconds() / 60.0
    if age_minutes < -RUNTIME_EVIDENCE_FUTURE_TOLERANCE_MINUTES:
        return "stale", round(age_minutes, 1)
    if age_minutes > RUNTIME_EVIDENCE_STALE_MINUTES:
        return "stale", round(age_minutes, 1)
    return "fresh", round(age_minutes, 1)


def _worker_runtime_gate(
    status: dict | None,
    ledger: dict | None,
    *,
    status_evidence: str | None = None,
    db_heartbeat_fresh: bool = False,
) -> tuple[str | None, str | None]:
    """Fail-safe latch / gate state from local runtime status files (no Supabase).

    Ledger lastHardStop is authoritative while set. When lastHardStop is cleared
    (owner recovery via record_healthy), do not report HARD_STOP even if status
    still carries a stale workerState.
    """
    gate_state = None
    gate_reason = None
    ledger_stop = None
    ledger_present = ledger is not None
    if ledger is not None:
        ledger_stop = str(ledger.get("lastHardStop") or "").strip() or None

    if status:
        gate_state = str(status.get("workerState") or "").strip() or None
        if _normalize_gate_state(gate_state) == "HARD_STOP":
            gate_reason = str(status.get("lastHardStopReason") or "").strip() or None

    if ledger_stop:
        gate_state = "HARD_STOP"
        gate_reason = ledger_stop
    elif ledger_present and _normalize_gate_state(gate_state) == "HARD_STOP":
        # Cleared latch in ledger supersedes HARD_STOP on status only with fresh evidence.
        gate_state = None
        gate_reason = None
        fresh_runtime = status_evidence == "fresh"
        if status:
            gate_state = str(status.get("workerState") or "").strip() or None
            if _normalize_gate_state(gate_state) == "HARD_STOP":
                if fresh_runtime or db_heartbeat_fresh:
                    gate_state = "SELECTING"
                else:
                    gate_state = None
                    gate_reason = "STALE_RUNTIME_AFTER_LATCH_CLEAR"

    if _normalize_gate_state(gate_state) == "BUDGET_WAIT" and not gate_reason:
        gate_reason = "BUDGET_WAIT"
    elif _normalize_gate_state(gate_state) == "COOLDOWN":
        if not gate_reason and status:
            gate_reason = str(status.get("cooldownUntil") or "COOLDOWN").strip() or "COOLDOWN"
    elif _normalize_gate_state(gate_state) == "HARD_STOP" and not gate_reason:
        gate_reason = "UNKNOWN_HARD_STOP"
    return gate_state, gate_reason


def _heartbeat_sort_key(row: dict) -> datetime:
    parsed = parse_utc(row.get("last_heartbeat_at"))
    if parsed is not None:
        return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def _heartbeat_row_worker_id(row: dict) -> str:
    return str(row.get("worker_id") or "").strip()


def _fetch_worker_db_heartbeat(
    client: SupabaseMarketEngineClient,
    *,
    worker_id: str | None = None,
) -> dict | None:
    """Return the worker pipeline heartbeat row, deterministically when possible."""
    configured = str(worker_id if worker_id is not None else os.getenv("MARKET_WORKER_ID", "")).strip()
    params: dict[str, str] = {
        "select": "component,worker_id,state,last_heartbeat_at,meta",
        "component": "eq.worker",
        "order": "last_heartbeat_at.desc",
        "limit": "50",
    }
    if configured:
        params["worker_id"] = f"eq.{configured}"

    try:
        rows = client._table_get(  # noqa: SLF001
            "market_price_pipeline_heartbeats",
            params=params,
        )
    except Exception:
        return None
    if not isinstance(rows, list) or not rows:
        return None

    candidates = [row for row in rows if isinstance(row, dict)]
    if not candidates:
        return None
    candidates.sort(key=_heartbeat_sort_key, reverse=True)
    if configured:
        for row in candidates:
            if _heartbeat_row_worker_id(row) == configured:
                return row
        return None

    distinct_worker_ids = {
        _heartbeat_row_worker_id(row) for row in candidates if _heartbeat_row_worker_id(row)
    }
    has_blank_worker_id = any(not _heartbeat_row_worker_id(row) for row in candidates)
    if len(distinct_worker_ids) > 1:
        return None
    if has_blank_worker_id:
        return None
    if len(distinct_worker_ids) == 1:
        only_id = next(iter(distinct_worker_ids))
        for row in candidates:
            if _heartbeat_row_worker_id(row) == only_id:
                return row
        return None
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

    status_payload = _read_json(STATUS_PATH)
    ledger_payload = _read_json(BUDGET_LEDGER_PATH)
    status_evidence, status_age_min = _runtime_file_evidence(status_payload, path=STATUS_PATH, now=current)
    ledger_evidence, ledger_age_min = _runtime_file_evidence(ledger_payload, path=BUDGET_LEDGER_PATH, now=current)

    worker_ok = len(worker_pids) == 1
    scheduler_ok = len(scheduler_pids) == 1 and scheduler_recent

    db_heartbeat_row = _fetch_worker_db_heartbeat(client)
    db_hb_state = str((db_heartbeat_row or {}).get("state") or "").strip() or None
    db_hb_at = parse_utc((db_heartbeat_row or {}).get("last_heartbeat_at"))
    db_hb_age_min = None
    db_hb_stale = False
    db_hb_fresh = False
    if db_hb_at is not None:
        db_hb_age_min = round((current - db_hb_at).total_seconds() / 60.0, 1)
        if db_hb_age_min < -RUNTIME_EVIDENCE_FUTURE_TOLERANCE_MINUTES:
            db_hb_stale = True
        elif db_hb_age_min > DB_HEARTBEAT_STALE_MINUTES:
            db_hb_stale = True
        else:
            db_hb_fresh = True
    elif len(worker_pids) >= 1:
        db_hb_stale = True

    gate_state, gate_reason = _worker_runtime_gate(
        status_payload,
        ledger_payload,
        status_evidence=status_evidence,
        db_heartbeat_fresh=db_hb_fresh,
    )
    worker_note = None
    if len(worker_pids) >= 1:
        if status_evidence != "fresh":
            worker_ok = False
            worker_note = (
                worker_note
                or f"Worker PID present but continuous_au_worker_status is {status_evidence}"
                + (f" (age {status_age_min}m)" if status_age_min is not None else "")
                + "; not healthy for pricing."
            )
        if ledger_evidence != "fresh":
            worker_ok = False
            worker_note = (
                worker_note
                or f"Worker PID present but continuous_au_safety_ledger is {ledger_evidence}"
                + (f" (age {ledger_age_min}m)" if ledger_age_min is not None else "")
                + "; not healthy for pricing."
            )

    if len(worker_pids) >= 1:
        if db_hb_stale:
            worker_ok = False
            worker_note = worker_note or (
                "Worker PID present but pipeline DB heartbeat is stale or missing; not healthy for pricing."
            )
        elif db_hb_state and db_hb_state.lower() in _DB_HEARTBEAT_GATED_STATES:
            worker_ok = False
            worker_note = worker_note or (
                f"Worker process alive but DB heartbeat state={db_hb_state} (gated); not claiming pricing jobs."
            )

    gate_blocks = _worker_gate_blocks_pricing(gate_state)
    if gate_blocks:
        worker_ok = False
        normalized = _normalize_gate_state(gate_state)
        if normalized == "HARD_STOP" and gate_reason:
            worker_note = (
                worker_note
                or (
                    f"Worker process may still be running but pricing is HARD_STOP latched "
                    f"({gate_reason}); awaiting owner review — not healthy for pricing."
                )
            )
        elif normalized == "BUDGET_WAIT":
            worker_note = (
                worker_note
                or (
                    f"Worker is in BUDGET_WAIT ({gate_reason or 'safety budget'}); "
                    "not claiming jobs — not healthy for pricing."
                )
            )
        elif normalized == "COOLDOWN":
            worker_note = (
                worker_note
                or "Worker is in COOLDOWN; not claiming provider jobs — not healthy for pricing."
            )
        else:
            worker_note = (
                worker_note
                or f"Worker gate state {gate_state} blocks pricing claims — not healthy for pricing."
            )

    if not worker_ok or not scheduler_ok or stuck or not healthy_markets or gate_blocks:
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
            "gateState": gate_state,
            "gateReason": gate_reason,
            "runtimeEvidence": {
                "status": status_evidence,
                "statusAgeMinutes": status_age_min,
                "ledger": ledger_evidence,
                "ledgerAgeMinutes": ledger_age_min,
                "staleThresholdMinutes": RUNTIME_EVIDENCE_STALE_MINUTES,
            },
            "dbHeartbeat": {
                "state": db_hb_state,
                "lastHeartbeatAt": (db_heartbeat_row or {}).get("last_heartbeat_at"),
                "ageMinutes": db_hb_age_min,
                "stale": db_hb_stale,
                "staleThresholdMinutes": DB_HEARTBEAT_STALE_MINUTES,
            },
            **({"note": worker_note} if worker_note else {}),
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
