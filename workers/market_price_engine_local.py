#!/usr/bin/env python3
"""
Phase 4B — Local Market Price Engine Runner

Coordinates the scheduler + worker in a configurable multi-cycle loop so that
a developer's PC can behave as a temporary backend engine without a running
server.

Usage (dry-run, no DB writes):
    python workers/market_price_engine_local.py --dry-run

One cycle:
    python workers/market_price_engine_local.py --cycles 1

Repeated cycles:
    python workers/market_price_engine_local.py --cycles 5 --poll-seconds 30

CLI options:
    --cycles              Number of scheduler+worker cycles to run (default: 1)
    --poll-seconds        Sleep between cycles (default: 0)
    --scheduler-max-keys  Override MARKET_SCHEDULER_MAX_KEYS_PER_RUN (default: 100)
    --scheduler-max-enqueues  Override MARKET_SCHEDULER_MAX_ENQUEUES_PER_RUN (default: 50)
    --worker-max-jobs     Override MARKET_WORKER_MAX_JOBS_PER_RUN (default: 50)
    --dry-run             Skip all DB writes; report plan only
    --reports-dir         Directory for output reports (default: reports)

Mock safety:
    MARKET_LOOKUP_PROVIDER must be "mock" or unset (defaults to mock).
    Live providers are blocked in this phase.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.scheduler import MarketPriceRefreshScheduler, MarketSchedulerConfig
from cardscanr_market_engine.smoke_utils import append_jsonl, sanitize_for_report, write_json


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _env_present(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


def _redact_env_summary() -> dict[str, Any]:
    """Return a sanitized env/config summary — never exposes secret values."""
    return {
        "supabase_url_present": _env_present("SUPABASE_URL"),
        "supabase_service_role_key_present": _env_present("SUPABASE_SERVICE_ROLE_KEY"),
        "market_lookup_provider": os.getenv("MARKET_LOOKUP_PROVIDER", "mock").strip().lower() or "mock",
        "market_worker_id": os.getenv("MARKET_WORKER_ID", "market-price-worker"),
    }


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Phase 4B — Local Market Price Engine Runner (mock-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--cycles", type=int, default=1, help="Number of scheduler+worker cycles (default: 1).")
    parser.add_argument("--poll-seconds", type=int, default=0, help="Sleep between cycles in seconds (default: 0).")
    parser.add_argument("--scheduler-max-keys", type=int, default=100, help="Max candidate keys per scheduler run.")
    parser.add_argument("--scheduler-max-enqueues", type=int, default=50, help="Max enqueues per scheduler run.")
    parser.add_argument("--worker-max-jobs", type=int, default=50, help="Max jobs per worker run.")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run: no DB writes, no jobs processed.")
    parser.add_argument("--reports-dir", type=str, default="reports", help="Directory for output reports.")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Mock-safety guard
# ---------------------------------------------------------------------------

def _assert_mock_safe() -> None:
    provider = os.getenv("MARKET_LOOKUP_PROVIDER", "mock").strip().lower() or "mock"
    if provider != "mock":
        raise ValueError(
            f"Phase 4B supports MARKET_LOOKUP_PROVIDER=mock only. "
            f"Current value: '{provider}'. "
            "Unset the variable or set it to 'mock' to proceed."
        )


# ---------------------------------------------------------------------------
# Scheduler adapter
# ---------------------------------------------------------------------------

def _build_scheduler_config(
    *,
    max_keys: int,
    max_enqueues: int,
    dry_run: bool,
    reports_dir: Path,
) -> MarketSchedulerConfig:
    supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    supabase_service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    return MarketSchedulerConfig(
        supabase_url=supabase_url,
        supabase_service_role_key=supabase_service_role_key,
        max_keys_per_run=max(1, max_keys),
        max_enqueues_per_run=max(1, max_enqueues),
        include_missing_cache=True,
        include_stale_cache=True,
        min_popularity_score=0,
        min_inventory_count=0,
        dry_run=dry_run,
        poll_seconds=300,
        latest_report_path=reports_dir / "market_price_scheduler_latest.json",
        runs_report_path=reports_dir / "market_price_scheduler_runs.jsonl",
    )


def _run_scheduler_once(
    *,
    scheduler: MarketPriceRefreshScheduler,
    dry_run: bool,
) -> dict[str, Any]:
    """Run one scheduler cycle. Returns a sanitized summary."""
    report = scheduler.run_and_write_reports()
    summary = report.get("summary", {})
    return {
        "status": report.get("status", "success"),
        "startedAtUtc": report.get("startedAtUtc"),
        "finishedAtUtc": report.get("finishedAtUtc"),
        "dryRun": dry_run,
        "candidatesScanned": summary.get("candidatesScanned", 0),
        "jobsEnqueued": summary.get("jobsEnqueued", 0),
        "jobsDryRunOnly": summary.get("jobsDryRunOnly", 0),
        "jobsSkippedAlreadyActive": summary.get("jobsSkippedAlreadyActive", 0),
        "jobsSkippedByLimit": summary.get("jobsSkippedByLimit", 0),
        "jobsSkippedFresh": summary.get("jobsSkippedFresh", 0),
    }


# ---------------------------------------------------------------------------
# Worker adapter
# ---------------------------------------------------------------------------

def _build_worker_runner(
    *,
    client: Any,
    max_jobs: int,
) -> Any:
    """Build a MarketPriceJobRunner for the mock provider."""
    from cardscanr_market_engine.config import MarketEngineConfig
    from cardscanr_market_engine.job_runner import MarketPriceJobRunner
    from cardscanr_market_engine.providers import MockMarketCompsProvider

    config = MarketEngineConfig.from_env(require_supabase=False)
    return MarketPriceJobRunner(
        client=client,
        provider=MockMarketCompsProvider(),
        config=config,
    ), config, max_jobs


def _run_worker_once(
    *,
    runner: Any,
    max_jobs: int,
    dry_run: bool,
) -> dict[str, Any]:
    """Run one worker cycle. In dry-run mode, skip actual job processing."""
    started_at = _utc_iso()
    if dry_run:
        return {
            "status": "dry_run_skipped",
            "startedAtUtc": started_at,
            "finishedAtUtc": _utc_iso(),
            "dryRun": True,
            "jobCount": 0,
            "jobsCompleted": 0,
            "jobsFailed": 0,
        }

    results = runner.run_once(max_jobs=max_jobs)
    jobs_completed = sum(1 for r in results if r.get("status") == "completed")
    jobs_failed = sum(1 for r in results if r.get("status") == "failed")
    return {
        "status": "success",
        "startedAtUtc": started_at,
        "finishedAtUtc": _utc_iso(),
        "dryRun": False,
        "jobCount": len(results),
        "jobsCompleted": jobs_completed,
        "jobsFailed": jobs_failed,
    }


# ---------------------------------------------------------------------------
# Supabase client helper
# ---------------------------------------------------------------------------

def _build_supabase_client() -> Any:
    from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
    supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    return SupabaseMarketEngineClient(
        supabase_url=supabase_url,
        service_role_key=service_role_key,
    )


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def _write_engine_reports(
    report: dict[str, Any],
    reports_dir: Path,
) -> None:
    clean = sanitize_for_report(report)
    write_json(reports_dir / "market_price_engine_local_latest.json", clean)
    append_jsonl(reports_dir / "market_price_engine_local_runs.jsonl", clean)


def _read_market_pricing_worker_summary(reports_dir: Path) -> dict[str, Any]:
    """Read the latest market_pricing_worker report and return a safe provider summary.

    Looks first in the given reports_dir, then falls back to ROOT/reports.
    Returns an empty dict if no report is found.
    """
    import json as _json

    candidates = [
        reports_dir / "market_pricing_worker_latest.json",
        ROOT / "reports" / "market_pricing_worker_latest.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                data = _json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                prov = data.get("providerSummary")
                summary = data.get("summary", {})
                return {
                    "available": True,
                    "providerRequested": data.get("providerRequested") or data.get("provider"),
                    "providerResolved": data.get("providerResolved"),
                    "providerEnabled": data.get("providerEnabled", True),
                    "liveEbayDisabled": not data.get("liveEbayEnabled", False),
                    "evidenceAccepted": (prov or summary).get("evidenceAccepted", 0) if isinstance(prov or summary, dict) else 0,
                    "evidenceRejected": (prov or summary).get("evidenceRejected", 0) if isinstance(prov or summary, dict) else 0,
                    "aggregatesBuilt": (prov or summary).get("aggregatesBuilt", 0) if isinstance(prov or summary, dict) else 0,
                    "workerMode": data.get("mode"),
                    "workerStatus": data.get("status"),
                    "generatedAtUtc": data.get("generatedAtUtc"),
                }
            except Exception:
                continue
    return {"available": False}


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_local_engine(
    *,
    cycles: int = 1,
    poll_seconds: int = 0,
    scheduler_max_keys: int = 100,
    scheduler_max_enqueues: int = 50,
    worker_max_jobs: int = 50,
    dry_run: bool = False,
    reports_dir: Path,
    scheduler_factory: Any = None,
    worker_factory: Any = None,
) -> dict[str, Any]:
    """
    Orchestrate scheduler + worker for a given number of cycles.

    scheduler_factory / worker_factory can be injected for unit tests.
    Each factory signature:
        scheduler_factory(config) -> scheduler_object
        worker_factory() -> (runner, config, max_jobs)
    """
    started_at = _utc_iso()
    errors: list[str] = []
    scheduler_summaries: list[dict[str, Any]] = []
    worker_summaries: list[dict[str, Any]] = []
    cycles_completed = 0

    supabase_present = _env_present("SUPABASE_URL") and _env_present("SUPABASE_SERVICE_ROLE_KEY")

    # Build scheduler config + instance
    scheduler_config = _build_scheduler_config(
        max_keys=scheduler_max_keys,
        max_enqueues=scheduler_max_enqueues,
        dry_run=dry_run,
        reports_dir=reports_dir,
    )

    if scheduler_factory is not None:
        scheduler = scheduler_factory(scheduler_config)
    else:
        client = _build_supabase_client()
        scheduler = MarketPriceRefreshScheduler(client=client, config=scheduler_config)

    # Build worker runner
    if worker_factory is not None:
        worker_runner, _worker_config, effective_max_jobs = worker_factory()
    else:
        worker_client = _build_supabase_client()
        worker_runner, _worker_config, effective_max_jobs = _build_worker_runner(
            client=worker_client,
            max_jobs=worker_max_jobs,
        )

    for cycle_num in range(1, cycles + 1):
        print(f"[market-engine-local] cycle={cycle_num}/{cycles} starting")

        # --- Scheduler ---
        try:
            sched_summary = _run_scheduler_once(scheduler=scheduler, dry_run=dry_run)
            scheduler_summaries.append({"cycle": cycle_num, **sched_summary})
            print(
                f"[market-engine-local] scheduler cycle={cycle_num} "
                f"candidates={sched_summary.get('candidatesScanned', 0)} "
                f"enqueued={sched_summary.get('jobsEnqueued', 0)} "
                f"dryRunOnly={sched_summary.get('jobsDryRunOnly', 0)}"
            )
        except Exception as exc:
            msg = f"cycle={cycle_num} scheduler error: {exc}"
            print(f"[market-engine-local] ERROR {msg}")
            errors.append(msg)
            scheduler_summaries.append({"cycle": cycle_num, "status": "error", "error": str(exc)})
            break

        # --- Worker ---
        try:
            worker_summary = _run_worker_once(
                runner=worker_runner,
                max_jobs=effective_max_jobs,
                dry_run=dry_run,
            )
            worker_summaries.append({"cycle": cycle_num, **worker_summary})
            print(
                f"[market-engine-local] worker cycle={cycle_num} "
                f"jobs={worker_summary.get('jobCount', 0)} "
                f"completed={worker_summary.get('jobsCompleted', 0)} "
                f"failed={worker_summary.get('jobsFailed', 0)}"
            )
        except Exception as exc:
            msg = f"cycle={cycle_num} worker error: {exc}"
            print(f"[market-engine-local] ERROR {msg}")
            errors.append(msg)
            worker_summaries.append({"cycle": cycle_num, "status": "error", "error": str(exc)})
            break

        cycles_completed += 1

        if cycle_num < cycles and poll_seconds > 0:
            print(f"[market-engine-local] sleeping {poll_seconds}s before next cycle...")
            time.sleep(poll_seconds)

    completed_at = _utc_iso()
    total_enqueued = sum(s.get("jobsEnqueued", 0) for s in scheduler_summaries)
    total_processed = sum(s.get("jobCount", 0) for s in worker_summaries)
    total_completed = sum(s.get("jobsCompleted", 0) for s in worker_summaries)
    total_failed = sum(s.get("jobsFailed", 0) for s in worker_summaries)

    report = {
        "started_at": started_at,
        "completed_at": completed_at,
        "cycles_requested": cycles,
        "cycles_completed": cycles_completed,
        "dry_run": dry_run,
        "total_jobs_enqueued": total_enqueued,
        "total_jobs_processed": total_processed,
        "total_jobs_completed": total_completed,
        "total_jobs_failed": total_failed,
        "errors": errors,
        "scheduler_summaries": scheduler_summaries,
        "worker_summaries": worker_summaries,
        "env_summary": _redact_env_summary(),
        "supabase_env_present": supabase_present,
        "config": {
            "scheduler_max_keys": scheduler_max_keys,
            "scheduler_max_enqueues": scheduler_max_enqueues,
            "worker_max_jobs": effective_max_jobs,
            "poll_seconds": poll_seconds,
        },
        "market_pricing_worker_summary": _read_market_pricing_worker_summary(reports_dir),
    }

    _write_engine_reports(report, reports_dir)
    return report


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        _assert_mock_safe()
    except ValueError as exc:
        print(f"[market-engine-local] BLOCKED: {exc}", file=sys.stderr)
        return 1

    reports_dir = ROOT / args.reports_dir

    supabase_missing = not _env_present("SUPABASE_URL") or not _env_present("SUPABASE_SERVICE_ROLE_KEY")
    if supabase_missing and not args.dry_run:
        print(
            "[market-engine-local] WARNING: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is missing. "
            "Live Supabase calls will fail. Use --dry-run or set the required env vars.",
            file=sys.stderr,
        )

    try:
        report = run_local_engine(
            cycles=max(1, args.cycles),
            poll_seconds=max(0, args.poll_seconds),
            scheduler_max_keys=max(1, args.scheduler_max_keys),
            scheduler_max_enqueues=max(1, args.scheduler_max_enqueues),
            worker_max_jobs=max(1, args.worker_max_jobs),
            dry_run=args.dry_run,
            reports_dir=reports_dir,
        )
    except Exception as exc:
        print(f"[market-engine-local] FATAL: {exc}", file=sys.stderr)
        return 2

    cycles_completed = report.get("cycles_completed", 0)
    errors = report.get("errors", [])
    print(
        f"[market-engine-local] done: cycles_completed={cycles_completed}/{args.cycles} "
        f"enqueued={report.get('total_jobs_enqueued', 0)} "
        f"processed={report.get('total_jobs_processed', 0)} "
        f"completed={report.get('total_jobs_completed', 0)} "
        f"failed={report.get('total_jobs_failed', 0)} "
        f"errors={len(errors)}"
    )
    print(f"[market-engine-local] reports: {reports_dir / 'market_price_engine_local_latest.json'}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
