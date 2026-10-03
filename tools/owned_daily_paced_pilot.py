#!/usr/bin/env python3
"""Enqueue + drain an owned-daily paced pilot (sequential eBay market checks).

Uses normal owned-daily priority order. Does not change pricing identity logic.
OWNED_DAILY_FULL_ENABLE must remain false for capped probes/pilots.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.gaming_resource_pause import GamingResourcePauseController
from cardscanr_market_engine.owned_daily_outcomes import HEALTHY_CHECK_OUTCOMES, summarize_outcome_counts
from cardscanr_market_engine.owned_daily_pacing import OwnedDailyPacingController
from cardscanr_market_engine.owned_daily_scheduler import (
    OwnedDailySchedulerConfig,
    OwnedPrintingRefreshScheduler,
)
from cardscanr_market_engine.providers.factory import create_market_comps_provider
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


OUT_DIR = ROOT / "reports" / "artifacts" / "owned_daily_session"
SCHEDULER_LATEST = ROOT / "reports" / "owned_price_scheduler_latest.json"


def _merge_pacing_into_scheduler_report(pacing_payload: dict[str, Any]) -> None:
    if not SCHEDULER_LATEST.exists():
        return
    try:
        report = json.loads(SCHEDULER_LATEST.read_text(encoding="utf-8"))
    except Exception:
        return
    report["pacing"] = pacing_payload
    # Flatten key observability fields requested for the latest scheduler report.
    for key in (
        "browser_mode",
        "inter_job_delay_seconds",
        "backoff_state",
        "checks_completed_today",
        "estimates_updated_today",
        "estimates_unchanged_today",
        "no_new_evidence_today",
        "browser_failures_today",
        "challenges_today",
        "last_good_retained_today",
        "average_check_seconds",
        "average_total_seconds_per_job",
        "effective_checks_per_hour",
        "safe_daily_capacity",
        "expected_daily_workload",
        "utilisation",
        "capacity_status",
    ):
        if key in pacing_payload:
            report[key] = pacing_payload[key]
    SCHEDULER_LATEST.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def enqueue_owned_daily(*, max_enqueue: int) -> dict[str, Any]:
    os.environ["OWNED_DAILY_MAX_ENQUEUE"] = str(max_enqueue)
    os.environ.setdefault("OWNED_DAILY_FULL_ENABLE", "false")
    client = SupabaseMarketEngineClient(
        supabase_url=os.environ["SUPABASE_URL"].rstrip("/"),
        service_role_key=supabase_secret_key_from_env(),
    )
    sched = OwnedPrintingRefreshScheduler(
        client=client,
        config=OwnedDailySchedulerConfig.from_env(),
    )
    return sched.run_and_write_reports()


def drain_paced(*, max_jobs: int, worker_id: str, out_name: str) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = SupabaseMarketEngineClient(
        supabase_url=os.environ["SUPABASE_URL"].rstrip("/"),
        service_role_key=supabase_secret_key_from_env(),
    )
    runner = MarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider("ebay_browser"),
        config=MarketEngineConfig.from_env(),
    )
    pacing = OwnedDailyPacingController()
    gaming = GamingResourcePauseController()
    results: list[dict[str, Any]] = []
    t0 = time.time()
    while len(results) < max_jobs:
        if pacing.state.browser_halted:
            print(f"[paced-pilot] HALT {pacing.state.halt_reason}", flush=True)
            break
        if gaming.should_block_new_jobs():
            print(
                f"[paced-pilot] GAMING_PAUSE reason={gaming.block_reason()} "
                f"status={gaming.status().get('pricingWorkerState')}",
                flush=True,
            )
            # Preserve queue: do not claim; wait and re-check.
            time.sleep(min(5, int(os.getenv("CARDSCANR_GAMING_POLL_INTERVAL_SECONDS", "20") or 20)))
            continue
        claimed = client.claim_jobs(worker_id=worker_id, max_jobs=1)
        if not claimed:
            print("[paced-pilot] no more queued jobs", flush=True)
            break
        job = claimed[0]
        reason = str(getattr(job, "reason", "") or "")
        print(f"[paced-pilot] [{len(results)+1}/{max_jobs}] {job.id} {reason}", flush=True)
        if not reason.lower().startswith("owned_daily:"):
            if hasattr(client, "cancel_job"):
                client.cancel_job(job_id=job.id, reason="paced_pilot_skip_non_owned_daily")
            continue
        before = client.get_cache_row(price_key_id=job.price_key_id) or {}
        job_t0 = time.monotonic()
        gaming.mark_job_started(str(job.price_key_id))
        try:
            # If Fortnite appears mid-job, finish this card (safe boundary) then pause.
            r = runner.run_job(job)
        finally:
            gaming.mark_job_finished()
        check_sec = time.monotonic() - job_t0
        after = client.get_cache_row(price_key_id=job.price_key_id) or {}
        outcome = str(r.get("ownedDailyOutcome") or r.get("outcomeClass") or "").strip()
        is_noop = outcome.endswith("_noop") or outcome in {"already_fresh_noop", "owned_daily_fresh_noop"}
        if not is_noop:
            pacing.record_check_duration(check_sec)
        pacing.observe_outcome(outcome, last_good_retained=bool(r.get("lastGoodRetained")))
        results.append(
            {
                "jobId": job.id,
                "priceKeyId": job.price_key_id,
                "reason": reason,
                "status": r.get("status"),
                "ownedDailyOutcome": outcome or None,
                "error": r.get("error"),
                "includedCount": r.get("includedCount"),
                "recommendedPrice": r.get("recommendedPrice"),
                "beforePrice": before.get("current_market_price"),
                "afterPrice": after.get("current_market_price"),
                "beforeProvider": before.get("provider"),
                "afterProvider": after.get("provider"),
                "snapshotId": r.get("snapshotId"),
                "lastGoodRetained": r.get("lastGoodRetained"),
                "checkSeconds": round(check_sec, 1),
                "imposedCooldownSeconds": None,
            }
        )
        more = len(results) < max_jobs
        delay = pacing.next_delay_seconds(more_jobs_pending=more and not pacing.state.browser_halted)
        results[-1]["imposedCooldownSeconds"] = delay
        print(
            f"[paced-pilot] outcome={outcome} check={check_sec:.0f}s cooldown={delay}s "
            f"failStreak={pacing.state.consecutive_browser_failures}",
            flush=True,
        )
        if delay > 0 and more and not pacing.state.browser_halted:
            time.sleep(delay)

    metrics = summarize_outcome_counts(results)
    healthy = sum(1 for row in results if row.get("ownedDailyOutcome") in HEALTHY_CHECK_OUTCOMES)
    browser = metrics["OWNED_PRICE_BROWSER_FAILURES"]
    ebay_primary_overwrites = 0
    for row in results:
        try:
            bp = float(row.get("beforePrice") or 0)
        except Exception:
            continue
        if row.get("beforeProvider") == "ebay_browser" and row.get("afterProvider") != "ebay_browser" and bp > 0:
            ebay_primary_overwrites += 1

    expected_due = 142
    try:
        sched = json.loads(SCHEDULER_LATEST.read_text(encoding="utf-8"))
        expected_due = int((sched.get("metrics") or {}).get("OWNED_PRICE_DUE") or expected_due)
    except Exception:
        sched = {}

    browser_mode = os.getenv("EBAY_BROWSER_MODE") or (
        "headed" if os.getenv("EBAY_BROWSER_HEADLESS", "").lower() == "false" else "headless"
    )
    pacing_payload = pacing.observability_payload(
        browser_mode=browser_mode,
        expected_daily_workload=expected_due,
    )
    _merge_pacing_into_scheduler_report(pacing_payload)

    pilot = {
        "elapsedSec": round(time.time() - t0, 1),
        "queued": max_jobs,
        "jobsDrained": len(results),
        "schedulerSummary": (sched or {}).get("summary"),
        "schedulerMetrics": (sched or {}).get("metrics"),
        "outcomeMetrics": metrics,
        "checksCompleted": metrics["OWNED_PRICE_CHECKS_COMPLETED"],
        "updated": metrics["OWNED_PRICE_ESTIMATES_UPDATED"],
        "unchanged": metrics["OWNED_PRICE_ESTIMATES_UNCHANGED"],
        "noNewExactEvidence": metrics["OWNED_PRICE_NO_NEW_EVIDENCE"],
        "browserFailures": browser,
        "challenges": metrics["OWNED_PRICE_CHALLENGES"],
        "noPriceEver": metrics["NO_PRICE_EVER_FOUND"],
        "snapshots": sum(1 for row in results if row.get("snapshotId")),
        "staleRetained": metrics["OWNED_PRICE_LAST_GOOD_RETAINED"],
        "ebayPrimaryOverwrites": ebay_primary_overwrites,
        "operationalSuccessRate": round(healthy / len(results), 3) if results else None,
        "priceUpdateRate": round(metrics["OWNED_PRICE_ESTIMATES_UPDATED"] / len(results), 3) if results else None,
        "healthy": healthy,
        "pacing": pacing_payload,
        "averageCheckSeconds": pacing_payload.get("average_check_seconds"),
        "averageCooldownSeconds": pacing.state.snapshot().get("average_cooldown_seconds"),
        "averageTotalSecondsPerJob": pacing_payload.get("average_total_seconds_per_job"),
        "effectiveChecksPerHour": pacing_payload.get("effective_checks_per_hour"),
        "results": results,
    }
    out_path = OUT_DIR / out_name
    out_path.write_text(json.dumps(pilot, indent=2, default=str, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: pilot[k] for k in pilot if k != "results"}, indent=2, default=str), flush=True)
    print(f"[paced-pilot] wrote {out_path}", flush=True)
    return pilot


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Owned-daily paced pilot")
    p.add_argument("--max-jobs", type=int, default=10)
    p.add_argument("--enqueue", action="store_true", help="Run owned-daily enqueue before drain")
    p.add_argument("--drain-only", action="store_true")
    p.add_argument("--worker-id", default="owned-daily-paced-pilot")
    p.add_argument("--out-name", default="paced_probe10_outcomes.json")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.enqueue and not args.drain_only:
        print(f"[paced-pilot] enqueue max={args.max_jobs}", flush=True)
        report = enqueue_owned_daily(max_enqueue=args.max_jobs)
        print(
            json.dumps(
                {
                    "jobsEnqueued": (report.get("summary") or {}).get("jobsEnqueued"),
                    "due": (report.get("metrics") or {}).get("OWNED_PRICE_DUE"),
                    "fullEnable": report.get("fullEnable"),
                },
                indent=2,
            ),
            flush=True,
        )
    drain_paced(max_jobs=args.max_jobs, worker_id=args.worker_id, out_name=args.out_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
