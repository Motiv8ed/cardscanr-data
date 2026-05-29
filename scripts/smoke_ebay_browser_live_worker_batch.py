#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.providers import create_market_comps_provider
from cardscanr_market_engine.providers.errors import sanitize_provider_diagnostics
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from scripts.smoke_ebay_browser_live_write import _identity, _summarize_bundle

MARKET_CURRENCIES = {"AU": "AUD", "US": "USD", "GB": "GBP", "CA": "CAD"}
LATEST_REPORT = ROOT / "reports" / "ebay_browser_live_worker_batch_latest.json"
RUNS_REPORT = ROOT / "reports" / "ebay_browser_live_worker_batch_runs.jsonl"


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_market_list(markets: str | list[str] | tuple[str, ...]) -> list[dict[str, str]]:
    raw_values: list[str]
    if isinstance(markets, (list, tuple)):
        raw_values = [str(value) for value in markets]
    else:
        raw_values = str(markets).replace(" ", ",").split(",")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_values:
        market = raw.strip().upper()
        if not market:
            continue
        if market not in MARKET_CURRENCIES:
            raise ValueError(f"Unsupported market '{market}'. Supported: AU, US, GB, CA")
        if market not in seen:
            rows.append({"market": market, "currency": MARKET_CURRENCIES[market]})
            seen.add(market)
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a controlled live eBay browser worker batch.")
    parser.add_argument("--markets", default="AU")
    parser.add_argument("--max-jobs", type=int, default=1)
    parser.add_argument("--pause-between-jobs-seconds", type=int, default=20)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--card-name", default="Charizard ex")
    parser.add_argument("--collector-number", default="125/197")
    parser.add_argument("--set-name", default="Obsidian Flames")
    parser.add_argument("--set-code", default="sv03")
    parser.add_argument("--condition", default="raw")
    parser.add_argument("--variant", default="raw")
    return parser.parse_args()


def _require_flags() -> None:
    required = {
        "MARKET_LOOKUP_PROVIDER": "ebay_browser",
        "ENABLE_EBAY_REAL_LOOKUP": "true",
        "CONFIRM_LIVE_EBAY_WRITE": "true",
        "CONFIRM_LIVE_EBAY_WORKER": "true",
    }
    for name, expected in required.items():
        if os.getenv(name, "").strip().lower() != expected:
            raise RuntimeError(f"{name}={expected} is required")


def default_plan(args: argparse.Namespace) -> list[dict[str, str]]:
    return parse_market_list(getattr(args, "markets", "AU") or "AU")


def _request_args(args: argparse.Namespace, *, market: str, currency: str) -> argparse.Namespace:
    return argparse.Namespace(
        market=market,
        currency=currency,
        card_name=args.card_name,
        collector_number=args.collector_number,
        set_name=args.set_name,
        set_code=args.set_code,
        condition=args.condition,
        variant=args.variant,
    )


def _process_job(
    *,
    client: SupabaseMarketEngineClient,
    config: MarketEngineConfig,
    job_id: str,
    expected_price_key_id: str,
    market: str,
) -> dict[str, Any]:
    existing_job = client.get_refresh_job(job_id=job_id)
    if existing_job is None:
        raise RuntimeError(f"Refresh job not found: {job_id}")
    if existing_job.price_key_id != expected_price_key_id:
        raise RuntimeError(f"Refusing to process job {job_id}: price_key_id does not match requested key")
    if existing_job.status != "queued":
        return {"status": "skipped", "reason": f"job_status_{existing_job.status}", "jobId": job_id}
    job = client.claim_specific_refresh_job(job_id=job_id, worker_id=config.worker_id)
    if job is None:
        raise RuntimeError(f"Refresh job {job_id} could not be claimed safely")
    if job.price_key_id != expected_price_key_id:
        raise RuntimeError(f"Refusing claimed job {job_id}: price_key_id does not match requested key")
    os.environ["EBAY_BROWSER_DEBUG_ARTIFACT_DIR"] = str(
        ROOT / "reports" / "ebay_browser_debug" / "live_worker_batch" / "latest" / market.lower()
    )
    runner = MarketPriceJobRunner(
        client=client,
        provider=create_market_comps_provider("ebay_browser"),
        config=config,
    )
    return runner.run_job(job)


def run_batch(args: argparse.Namespace) -> dict[str, Any]:
    _require_flags()
    started = utc_iso()
    max_jobs = max(1, min(int(getattr(args, "max_jobs", 1) or 1), 20))
    force_refresh = bool(getattr(args, "force_refresh", False))
    pause_seconds = max(0, int(getattr(args, "pause_between_jobs_seconds", 20) or 0))
    plan = default_plan(args)
    config = MarketEngineConfig.from_env(require_supabase=True)
    client = SupabaseMarketEngineClient(
        supabase_url=config.supabase_url,
        service_role_key=config.supabase_service_role_key,
        timeout_seconds=60,
    )
    market_results: list[dict[str, Any]] = []
    processed_count = 0
    skipped_cache_fresh = 0
    errors: list[dict[str, Any]] = []
    for row in plan:
        market = row["market"]
        currency = row["currency"]
        request_args = _request_args(args, market=market, currency=currency)
        identity = _identity(request_args)
        refresh: dict[str, Any] = {}
        worker_result: dict[str, Any] | None = None
        bundle: dict[str, Any] | None = None
        error: dict[str, Any] | None = None
        try:
            refresh = client.request_market_price_refresh(
                **identity,
                reason="live_ebay_worker_batch",
                force_refresh=force_refresh,
            )
            action = str(refresh.get("action"))
            job_id = str(refresh.get("job_id") or "")
            if action == "cache_fresh":
                skipped_cache_fresh += 1
            elif action in {"job_enqueued", "active_job_exists"} and job_id:
                if processed_count < max_jobs:
                    worker_result = _process_job(
                        client=client,
                        config=config,
                        job_id=job_id,
                        expected_price_key_id=str(refresh.get("price_key_id")),
                        market=market,
                    )
                    if worker_result.get("status") == "completed":
                        processed_count += 1
                        if pause_seconds and processed_count < max_jobs:
                            time.sleep(pause_seconds)
                else:
                    worker_result = {"status": "skipped", "reason": "max_jobs_reached", "jobId": job_id}
            else:
                worker_result = {"status": "skipped", "reason": f"unhandled_action_{action}", "jobId": job_id}
            bundle = client.get_market_price_bundle(fingerprint=identity["fingerprint"], evidence_limit=100)
        except Exception as exc:
            error = {"market": market, "error": str(exc), "error_type": type(exc).__name__}
            errors.append(error)
        summary = _summarize_bundle(bundle)
        market_results.append(
            sanitize_provider_diagnostics(
                {
                    "market": market,
                    "currency": currency,
                    "identity": identity,
                    "request_market_price_refresh": refresh,
                    "job_id": refresh.get("job_id"),
                    "worker_result": worker_result,
                    "cache_fresh": refresh.get("action") == "cache_fresh",
                    "processed": bool(worker_result and worker_result.get("status") == "completed"),
                    "debug_artifact_dir": str(
                        ROOT / "reports" / "ebay_browser_debug" / "live_worker_batch" / "latest" / market.lower()
                    ),
                    "error": error,
                    **summary,
                }
            )
        )
    report = sanitize_provider_diagnostics(
        {
            "status": "success" if not errors else "partial",
            "startedAtUtc": started,
            "finishedAtUtc": utc_iso(),
            "requested_markets": [row["market"] for row in plan],
            "max_jobs": max_jobs,
            "force_refresh_requested": force_refresh,
            "processed_job_count": processed_count,
            "cache_fresh_skipped_count": skipped_cache_fresh,
            "jobs_processed": [
                item.get("job_id") for item in market_results if item.get("processed") and item.get("job_id")
            ],
            "markets": market_results,
            "errors": errors,
        }
    )
    return report


def main() -> int:
    args = parse_args()
    try:
        report = run_batch(args)
    except Exception as exc:
        report = sanitize_provider_diagnostics(
            {
                "status": "failed",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "finishedAtUtc": utc_iso(),
            }
        )
        write_json(LATEST_REPORT, report)
        append_jsonl(RUNS_REPORT, report)
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        return 1
    write_json(LATEST_REPORT, report)
    append_jsonl(RUNS_REPORT, report)
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if report.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
