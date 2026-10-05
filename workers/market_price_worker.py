#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

import requests
import urllib3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.canary_control_plane import parse_operation_mode
from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.continuous_au_status import append_daily_metrics, write_continuous_status
from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.continuous_worker_policy import (
    HARD_STOP_OUTCOMES,
    classify_continuous_gate,
    maybe_resume_transient_halt,
)
from cardscanr_market_engine.international.fallback_runner import InternationalMarketPriceJobRunner
from cardscanr_market_engine.navigation_runtime_context import bind_persisted_nav_context_path
from cardscanr_market_engine.owned_daily_enablement import (
    apply_continuous_au_env,
    owned_daily_full_enable,
    write_owned_daily_flag,
)
from cardscanr_market_engine.owned_daily_outcomes import (
    CHALLENGE_REQUIRED,
    TEMPORARY_EBAY_SERVER_FAILURE,
    TRANSIENT_EBAY_FAILURE_OUTCOMES,
)
from cardscanr_market_engine.owned_daily_pacing import OwnedDailyPacingController
from cardscanr_market_engine.providers import create_market_comps_provider
from cardscanr_market_engine.scheduler import parse_market_allowlist
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient

MIN_TRANSIENT_BACKOFF_SECONDS = 10
MAX_TRANSIENT_BACKOFF_SECONDS = 60
MAX_REPORT_ERROR_CHARS = 1000


def _ebay_pacing_enabled(config: MarketEngineConfig) -> bool:
    return str(getattr(config, "provider_name", "") or "").strip().lower() == "ebay_browser"


def _outcome_from_result(row: dict[str, Any]) -> str | None:
    return (
        str(row.get("ownedDailyOutcome") or row.get("outcomeClass") or "").strip() or None
    )


def utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _http_status_code(exc: BaseException) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int):
        return response_status
    return None


def is_transient_worker_error(exc: BaseException) -> bool:
    transient_types = (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        urllib3.exceptions.ProtocolError,
        urllib3.exceptions.ReadTimeoutError,
        urllib3.exceptions.ConnectTimeoutError,
        urllib3.exceptions.MaxRetryError,
        urllib3.exceptions.NewConnectionError,
        ConnectionResetError,
    )
    if isinstance(exc, transient_types):
        return True
    status_code = _http_status_code(exc)
    if status_code is not None and 500 <= status_code <= 599:
        return True
    for nested in (*getattr(exc, "args", ()), getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if isinstance(nested, transient_types):
            return True
    return False


def sanitized_error_message(exc: BaseException) -> str:
    message = str(exc)
    replacements = [
        (r"(?i)(apikey=)[^&\s'\",;]+", r"\1***REDACTED***"),
        (r"(?i)(Authorization:\s*Bearer\s+)[^\s'\",;]+", r"\1***REDACTED***"),
        (r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***REDACTED***"),
        (r"(?i)((?:service_role_key|supabase_service_role_key)\s*[:=]\s*)[^\s'\",;]+", r"\1***REDACTED***"),
    ]
    for pattern, replacement in replacements:
        message = re.sub(pattern, replacement, message)
    if len(message) > MAX_REPORT_ERROR_CHARS:
        message = message[:MAX_REPORT_ERROR_CHARS] + "...<truncated>"
    return message


def next_transient_backoff_seconds(current_backoff_seconds: int, *, poll_seconds: int) -> int:
    if current_backoff_seconds <= 0:
        return min(max(poll_seconds, MIN_TRANSIENT_BACKOFF_SECONDS), MAX_TRANSIENT_BACKOFF_SECONDS)
    return min(current_backoff_seconds * 2, MAX_TRANSIENT_BACKOFF_SECONDS)


def build_cycle_summary(
    *,
    config: MarketEngineConfig,
    cycle: int,
    started_at: str,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "startedAtUtc": started_at,
        "finishedAtUtc": utc_iso(),
        "cycle": cycle,
        "workerId": config.worker_id,
        "provider": config.provider_name,
        "jobCount": len(results),
        "results": results,
    }


def build_transient_error_summary(
    *,
    config: MarketEngineConfig,
    cycle: int,
    started_at: str,
    exc: BaseException,
    backoff_seconds: int,
) -> dict[str, Any]:
    return {
        "startedAtUtc": started_at,
        "finishedAtUtc": utc_iso(),
        "cycle": cycle,
        "workerId": config.worker_id,
        "provider": config.provider_name,
        "status": "transient_error",
        "jobCount": 0,
        "results": [],
        "error_type": exc.__class__.__name__,
        "error_message": sanitized_error_message(exc),
        "backoff_seconds": backoff_seconds,
        "retry_after_seconds": backoff_seconds,
    }


def run_worker_loop(
    *,
    args: argparse.Namespace,
    config: MarketEngineConfig,
    runner: InternationalMarketPriceJobRunner,
    poll_seconds: int,
    max_jobs: int,
    sleep_func: Any = time.sleep,
    logger: Any = print,
    pacing: OwnedDailyPacingController | None = None,
) -> int:
    cycle = 0
    transient_backoff_seconds = 0
    pacing_controller = pacing
    if pacing_controller is None and _ebay_pacing_enabled(config):
        pacing_controller = OwnedDailyPacingController()
    budget = ContinuousSafetyBudget.from_env()
    last_success = None
    last_transient = None
    healthy_times: list[str] = []

    while True:
        cycle += 1
        started_at = utc_iso()
        cycle_t0 = time.monotonic()
        continuous = owned_daily_full_enable()
        operation_mode = parse_operation_mode()
        allowed_markets = parse_market_allowlist(os.getenv("MARKET_WORKER_ALLOWED_MARKETS", ""))
        gate_market = allowed_markets[0] if len(allowed_markets) == 1 else "AU"
        gate_info: dict[str, Any] = {}
        worker_state = "SELECTING"
        hard = None
        budget_reason = None
        # Probe / canary modes must classify the target market even when the
        # continuous flag is false so PROBE_REQUIRED can begin_probe.
        if continuous or operation_mode in {"PROBE", "CANARY"}:
            gate_info = classify_continuous_gate(market=gate_market)
            if continuous and pacing_controller is not None:
                maybe_resume_transient_halt(pacing_controller, gate_info)
            if continuous:
                budget_reason = budget.budget_exhausted()
                transient_stop = budget.transient_hard_stop()
                hard = gate_info.get("hardStop") or transient_stop
                if pacing_controller is not None and pacing_controller.state.halt_reason in HARD_STOP_OUTCOMES:
                    hard = pacing_controller.state.halt_reason
            else:
                hard = gate_info.get("hardStop")
            worker_state = str(gate_info.get("workerState") or "SELECTING")
            if budget_reason:
                worker_state = "BUDGET_WAIT"
            if hard:
                worker_state = "HARD_STOP"
                if continuous:
                    write_owned_daily_flag(False)
                    budget.last_hard_stop = str(hard)
                    budget.persist()
            if continuous:
                write_continuous_status(
                    {
                        "enabled": continuous and not hard,
                        "market": gate_market,
                        "workerState": worker_state,
                        "submissions1h": budget.submissions_1h(),
                        "submissions24h": budget.submissions_24h(),
                        "healthy1h": len(healthy_times),
                        "healthy24h": len(healthy_times),
                        "transientFailures1h": budget.transients_1h(),
                        "consecutiveTransientFailures": budget.consecutive_transient,
                        "lastSuccessfulJobAt": last_success,
                        "lastTransientFailureAt": last_transient,
                        "cooldownUntil": gate_info.get("cooldownUntil"),
                        "nextProbeAt": gate_info.get("nextProbeAt"),
                        "lastHardStopReason": hard or budget.last_hard_stop,
                        "activeChallenges": gate_info.get("activeChallenges") or 0,
                    }
                )
            if hard:
                logger(
                    "[market-engine] "
                    f"cycle={cycle} status=HARD_STOP reason={hard} market={gate_market} "
                    f"mode={operation_mode} awaiting owner review"
                )
                if args.once:
                    return 2
                sleep_func(max(poll_seconds, 60))
                continue
            if budget_reason:
                logger(
                    "[market-engine] "
                    f"cycle={cycle} status=BUDGET_WAIT reason={budget_reason}"
                )
                if args.once:
                    return 0
                sleep_func(max(poll_seconds, 60))
                continue
            if worker_state == "COOLDOWN":
                logger(
                    "[market-engine] "
                    f"cycle={cycle} status=COOLDOWN market={gate_market} "
                    f"until={gate_info.get('cooldownUntil')} "
                    f"nextProbeAt={gate_info.get('nextProbeAt')} no provider"
                )
                if args.once:
                    return 0
                sleep_func(max(poll_seconds, 30))
                continue

        if pacing_controller is not None and pacing_controller.state.browser_halted:
            halt = pacing_controller.state.halt_reason
            if halt in HARD_STOP_OUTCOMES:
                logger(
                    "[market-engine] "
                    f"cycle={cycle} status=browser_halted reason={halt} "
                    "awaiting human eBay session restoration"
                )
                if args.once:
                    return 0
                if args.max_cycles > 0 and cycle >= args.max_cycles:
                    return 0
                sleep_func(max(poll_seconds, pacing_controller.config.max_inter_job_delay_seconds))
                continue
            logger(
                "[market-engine] "
                f"cycle={cycle} status=transient_halt reason={halt} waiting control plane"
            )
            if args.once:
                return 0
            sleep_func(max(poll_seconds, 30))
            continue

        runner._ebay_probe_mode = (  # noqa: SLF001
            worker_state == "PROBE_REQUIRED"
            or operation_mode == "PROBE"
            or str(gate_info.get("availabilityState") or "").upper() == "PROBE_REQUIRED"
        )
        try:
            results = runner.run_once(max_jobs=max_jobs)
        except Exception as exc:
            if not is_transient_worker_error(exc):
                raise
            transient_backoff_seconds = next_transient_backoff_seconds(
                transient_backoff_seconds,
                poll_seconds=poll_seconds,
            )
            summary = build_transient_error_summary(
                config=config,
                cycle=cycle,
                started_at=started_at,
                exc=exc,
                backoff_seconds=transient_backoff_seconds,
            )
            write_json(config.latest_report_path, summary)
            append_jsonl(config.runs_report_path, summary)
            logger(
                "[market-engine] "
                f"cycle={cycle} status=transient_error errorType={summary['error_type']} "
                f"retryAfter={transient_backoff_seconds}s report={config.latest_report_path}"
            )

            if args.once:
                return 0
            if args.max_cycles > 0 and cycle >= args.max_cycles:
                return 0
            sleep_func(transient_backoff_seconds)
            continue

        transient_backoff_seconds = 0
        if pacing_controller is not None and results:
            elapsed = time.monotonic() - cycle_t0
            per_job = elapsed / max(1, len(results))
            for row in results:
                pacing_controller.record_check_duration(per_job)
                outcome = _outcome_from_result(row)
                pacing_controller.observe_outcome(
                    outcome,
                    last_good_retained=bool(row.get("lastGoodRetained")),
                )
                if outcome in HARD_STOP_OUTCOMES:
                    break
                if continuous and (
                    outcome == TEMPORARY_EBAY_SERVER_FAILURE
                    or outcome in TRANSIENT_EBAY_FAILURE_OUTCOMES
                ):
                    last_transient = utc_iso()
                    stop_reason = budget.record_transient()
                    budget.persist()
                    if stop_reason:
                        write_owned_daily_flag(False)
                        write_continuous_status(
                            {
                                "enabled": False,
                                "workerState": "HARD_STOP",
                                "lastHardStopReason": stop_reason,
                                "consecutiveTransientFailures": budget.consecutive_transient,
                            }
                        )
                        logger(
                            "[market-engine] "
                            f"cycle={cycle} status=HARD_STOP reason={stop_reason}"
                        )
                        if args.once:
                            return 2
                        break
                elif outcome in {"UPDATED_FROM_EBAY", "UNCHANGED_FROM_EBAY", "CHECKED_NO_NEW_EXACT_EVIDENCE"}:
                    if continuous:
                        budget.record_healthy()
                    last_success = utc_iso()
                    healthy_times.append(last_success)
                    consumed = bool(row.get("searchSubmissionStarted")) or str(row.get("status") or "") == "completed"
                    if consumed:
                        budget.record_submission()
                    budget.persist()
        summary = build_cycle_summary(
            config=config,
            cycle=cycle,
            started_at=started_at,
            results=results,
        )
        if pacing_controller is not None:
            summary["ownedDailyPacing"] = pacing_controller.state.snapshot()
        write_json(config.latest_report_path, summary)
        append_jsonl(config.runs_report_path, summary)
        append_daily_metrics(
            {
                "cycle": cycle,
                "jobCount": len(results),
                "workerState": worker_state,
                "submissions1h": budget.submissions_1h(),
                "submissions24h": budget.submissions_24h(),
            }
        )
        try:
            if hasattr(runner.client, "upsert_pipeline_heartbeat"):
                completed = sum(1 for row in results if str(row.get("status") or "") == "completed")
                failed = sum(1 for row in results if str(row.get("status") or "") == "failed")
                runner.client.upsert_pipeline_heartbeat(
                    component="worker",
                    worker_id=config.worker_id,
                    state="processing" if results else "idle",
                    meta={
                        "startedAtUtc": started_at,
                        "finishedAtUtc": summary.get("finishedAtUtc"),
                        "jobCount": len(results),
                        "jobsCompleted": completed,
                        "jobsFailed": failed,
                        "provider": config.provider_name,
                        "cycle": cycle,
                    },
                )
            if hasattr(runner.client, "recover_abandoned_refresh_jobs") and cycle % 5 == 1:
                runner.client.recover_abandoned_refresh_jobs(
                    stale_after_minutes=int(os.getenv("MARKET_WORKER_STALE_LOCK_MINUTES", "90")),
                    max_jobs=10,
                )
        except Exception as heartbeat_exc:  # pragma: no cover - ops telemetry must not stop worker
            logger(f"[market-engine] heartbeat_warning={heartbeat_exc.__class__.__name__}")
        logger(f"[market-engine] cycle={cycle} jobCount={len(results)} report={config.latest_report_path}")

        if args.once:
            return 0
        if args.max_cycles > 0 and cycle >= args.max_cycles:
            return 0
        if pacing_controller is not None and results:
            delay = pacing_controller.next_delay_seconds(more_jobs_pending=True)
            logger(f"[market-engine] paced_cooldown={delay}s outcome={pacing_controller.state.last_outcome}")
            sleep_func(delay if delay > 0 else poll_seconds)
        else:
            sleep_func(poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the CardScanR market price worker.")
    parser.add_argument("--once", action="store_true", help="Process one poll cycle and exit.")
    parser.add_argument("--max-cycles", type=int, default=0, help="Optional cycle limit for loop mode.")
    parser.add_argument("--max-jobs", type=int, default=0, help="Override MARKET_WORKER_MAX_JOBS_PER_RUN.")
    parser.add_argument("--poll-seconds", type=int, default=0, help="Override MARKET_WORKER_POLL_SECONDS.")
    return parser.parse_args()


def main() -> int:
    bind_persisted_nav_context_path()
    args = parse_args()
    if owned_daily_full_enable():
        apply_continuous_au_env()
    config = MarketEngineConfig.from_env(require_supabase=True)
    if config.provider_name == "ebay_browser" and os.getenv("CONFIRM_LIVE_EBAY_WORKER", "").strip().lower() != "true":
        raise ValueError(
            "MARKET_LOOKUP_PROVIDER=ebay_browser requires CONFIRM_LIVE_EBAY_WORKER=true for the bulk worker. "
            "Use the one-card live write smoke for controlled validation."
        )
    if config.worker_concurrency != 1:
        print("[market-engine] MARKET_WORKER_CONCURRENCY>1 is not used for local browser provider; continuing sequentially.")

    client = SupabaseMarketEngineClient(
        supabase_url=config.supabase_url,
        service_role_key=config.supabase_service_role_key,
    )
    runner = InternationalMarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider(config.provider_name),
        config=config,
    )
    poll_seconds = args.poll_seconds if args.poll_seconds > 0 else config.poll_seconds
    max_jobs = args.max_jobs if args.max_jobs > 0 else config.max_jobs_per_run
    return run_worker_loop(
        args=args,
        config=config,
        runner=runner,
        poll_seconds=poll_seconds,
        max_jobs=max_jobs,
    )


if __name__ == "__main__":
    raise SystemExit(main())
