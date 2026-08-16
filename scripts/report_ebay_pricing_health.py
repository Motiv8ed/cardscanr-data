#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import REPORTS_DIR, supabase_secret_key_from_env
from cardscanr_market_engine.smoke_utils import write_json
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _count_python_workers(needle: str) -> list[int]:
    # Best-effort Windows process scan without exposing command secrets.
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
        pids: list[int] = []
        for line in (completed.stdout or "").splitlines():
            text = line.strip()
            if text.isdigit():
                pids.append(int(text))
        return pids
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


def main() -> int:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = supabase_secret_key_from_env()
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SECRET_KEY are required")

    client = SupabaseMarketEngineClient(supabase_url=url, service_role_key=key)
    now = utc_now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")

    jobs = client._table_get(  # noqa: SLF001 - operational health only
        "market_price_refresh_jobs",
        params={
            "select": "id,status,reason,error_message,requested_at,started_at,completed_at,price_key_id,created_snapshot_id",
            "order": "requested_at.desc",
            "limit": "500",
        },
    )
    if not isinstance(jobs, list):
        jobs = []

    queued = [j for j in jobs if j.get("status") == "queued"]
    running = [j for j in jobs if j.get("status") == "running"]
    completed_today = [
        j
        for j in jobs
        if j.get("status") == "completed" and str(j.get("completed_at") or "") >= day_start
    ]
    failed_today = [
        j for j in jobs if j.get("status") == "failed" and str(j.get("completed_at") or "") >= day_start
    ]
    auth_or_challenge = [
        j
        for j in failed_today
        if "authentication" in str(j.get("error_message") or "").lower()
        or "challenge" in str(j.get("error_message") or "").lower()
        or "MARKETPLACE_CHALLENGE" in str(j.get("error_message") or "")
    ]
    no_resultish = [
        j
        for j in completed_today
        if not j.get("created_snapshot_id")
    ]

    cache_rows = client._table_get(  # noqa: SLF001
        "market_price_cache",
        params={
            "select": "price_key_id,recommended_price,last_updated_at,stale_after,marketplace,market_price_keys!inner(market_country,currency,fingerprint)",
            "limit": "500",
        },
    )
    if not isinstance(cache_rows, list):
        cache_rows = []

    stale = 0
    missing_price = 0
    by_market: dict[str, dict[str, int]] = {}
    for row in cache_rows:
        key = row.get("market_price_keys") or {}
        if isinstance(key, list):
            key = key[0] if key else {}
        market = str(key.get("market_country") or "?").upper()
        bucket = by_market.setdefault(market, {"cache": 0, "stale": 0, "missing_price": 0})
        bucket["cache"] += 1
        stale_after = str(row.get("stale_after") or "")
        if stale_after and stale_after <= now.isoformat().replace("+00:00", "Z"):
            stale += 1
            bucket["stale"] += 1
        if row.get("recommended_price") is None:
            missing_price += 1
            bucket["missing_price"] += 1

    last_success = None
    for row in sorted(cache_rows, key=lambda item: str(item.get("last_updated_at") or ""), reverse=True):
        if row.get("last_updated_at"):
            last_success = row.get("last_updated_at")
            break

    worker_pids = _count_python_workers("workers/market_price_worker.py")
    scheduler_pids = _count_python_workers("workers/market_price_scheduler.py")
    scheduler_latest = _read_json(REPORTS_DIR / "market_price_scheduler_latest.json")
    worker_latest = _read_json(REPORTS_DIR / "market_price_worker_latest.json")

    oldest_queued = None
    if queued:
        oldest_queued = min(str(j.get("requested_at") or "") for j in queued)

    report = {
        "status": "success",
        "checkedAtUtc": iso(now),
        "pricingWorkingNow": bool(worker_pids) and bool(scheduler_pids or (scheduler_latest and scheduler_latest.get("status") == "success")),
        "worker": {
            "healthy": bool(worker_pids),
            "pids": worker_pids,
            "latestCycle": worker_latest,
        },
        "scheduler": {
            "healthy": bool(scheduler_pids),
            "pids": scheduler_pids,
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
            "oldestQueuedAt": oldest_queued or None,
            "lastJobClaimedAt": max((str(j.get("started_at") or "") for j in jobs if j.get("started_at")), default=None),
            "successfulToday": len(completed_today),
            "failedToday": len(failed_today),
            "authOrChallengeDeferredToday": len(auth_or_challenge),
            "noResultHeuristicToday": len(no_resultish),
        },
        "lastSuccessfulPriceRefreshAt": last_success,
        "markets": {
            "AU": {"status": "live_recurring", **by_market.get("AU", {"cache": 0, "stale": 0, "missing_price": 0})},
            "US": {"status": "live_recurring", **by_market.get("US", {"cache": 0, "stale": 0, "missing_price": 0})},
            "GB": {
                "status": "MARKETPLACE_CHALLENGE_UNRESOLVED_NOT_AUTH_ATTEMPTED",
                **by_market.get("GB", {"cache": 0, "stale": 0, "missing_price": 0}),
            },
            "CA": {
                "status": "MARKETPLACE_CHALLENGE_UNRESOLVED_NOT_AUTH_ATTEMPTED",
                **by_market.get("CA", {"cache": 0, "stale": 0, "missing_price": 0}),
            },
        },
        "cache": {
            "rowsSampled": len(cache_rows),
            "stale": stale,
            "missingPrice": missing_price,
        },
    }

    out = REPORTS_DIR / "ebay_pricing_health_latest.json"
    write_json(out, report)
    print(json.dumps(report, indent=2))
    print(f"[health] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
