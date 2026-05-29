#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.models import MarketPriceKey
from cardscanr_market_engine.providers.errors import sanitize_provider_diagnostics
from cardscanr_market_engine.scheduler import _parse_utc, utc_iso
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient

LATEST_REPORT = ROOT / "reports" / "ebay_browser_live_scheduler_latest.json"
RUNS_REPORT = ROOT / "reports" / "ebay_browser_live_scheduler_runs.jsonl"


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_market_allowlist(value: str | list[str] | tuple[str, ...]) -> list[str]:
    raw_values = value if isinstance(value, (list, tuple)) else str(value).replace(" ", ",").split(",")
    markets: list[str] = []
    for raw in raw_values:
        market = str(raw).strip().upper()
        if market and market not in markets:
            markets.append(market)
    return markets or ["AU"]


@dataclass(frozen=True)
class LiveSchedulerConfig:
    enabled: bool
    confirmed: bool
    markets: list[str]
    max_enqueues_per_run: int
    max_keys_scanned_per_run: int
    min_cooldown_hours: int
    allow_force_refresh: bool
    dry_run: bool
    daily_enqueue_cap: int

    @classmethod
    def from_env(cls) -> "LiveSchedulerConfig":
        return cls(
            enabled=_parse_bool_env("ENABLE_LIVE_EBAY_SCHEDULER", False),
            confirmed=_parse_bool_env("CONFIRM_LIVE_EBAY_SCHEDULER", False),
            markets=parse_market_allowlist(os.getenv("LIVE_EBAY_SCHEDULER_MARKETS", "AU")),
            max_enqueues_per_run=max(1, _parse_int_env("LIVE_EBAY_SCHEDULER_MAX_ENQUEUES_PER_RUN", 2)),
            max_keys_scanned_per_run=max(1, _parse_int_env("LIVE_EBAY_SCHEDULER_MAX_KEYS_SCANNED_PER_RUN", 25)),
            min_cooldown_hours=max(0, _parse_int_env("LIVE_EBAY_SCHEDULER_MIN_COOLDOWN_HOURS", 6)),
            allow_force_refresh=_parse_bool_env("LIVE_EBAY_SCHEDULER_ALLOW_FORCE_REFRESH", False),
            dry_run=_parse_bool_env("LIVE_EBAY_SCHEDULER_DRY_RUN", True),
            daily_enqueue_cap=max(0, _parse_int_env("LIVE_EBAY_SCHEDULER_DAILY_ENQUEUE_CAP", 20)),
        )

    def with_overrides(
        self,
        *,
        markets: list[str] | None = None,
        max_enqueues: int | None = None,
        dry_run: bool | None = None,
    ) -> "LiveSchedulerConfig":
        return LiveSchedulerConfig(
            enabled=self.enabled,
            confirmed=self.confirmed,
            markets=markets or self.markets,
            max_enqueues_per_run=max(1, max_enqueues if max_enqueues is not None else self.max_enqueues_per_run),
            max_keys_scanned_per_run=self.max_keys_scanned_per_run,
            min_cooldown_hours=self.min_cooldown_hours,
            allow_force_refresh=self.allow_force_refresh,
            dry_run=self.dry_run if dry_run is None else dry_run,
            daily_enqueue_cap=self.daily_enqueue_cap,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run or enqueue a guarded live eBay scheduler batch.")
    parser.add_argument("--markets", default=None)
    parser.add_argument("--max-enqueues", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", default=None)
    parser.add_argument("--real-enqueue", action="store_true", default=False)
    return parser.parse_args()


def _require_live_provider_flags(config: MarketEngineConfig) -> list[str]:
    missing: list[str] = []
    if config.provider_name != "ebay_browser":
        missing.append("MARKET_LOOKUP_PROVIDER=ebay_browser")
    if os.getenv("ENABLE_EBAY_REAL_LOOKUP", "").strip().lower() != "true":
        missing.append("ENABLE_EBAY_REAL_LOOKUP=true")
    return missing


def _require_real_enqueue_flags(*, engine_config: MarketEngineConfig, live_config: LiveSchedulerConfig) -> list[str]:
    missing = _require_live_provider_flags(engine_config)
    if not live_config.enabled:
        missing.append("ENABLE_LIVE_EBAY_SCHEDULER=true")
    if not live_config.confirmed:
        missing.append("CONFIRM_LIVE_EBAY_SCHEDULER=true")
    if live_config.allow_force_refresh:
        missing.append("LIVE_EBAY_SCHEDULER_ALLOW_FORCE_REFRESH=false")
    return missing


def _identity_from_key(key: MarketPriceKey) -> dict[str, str]:
    return {
        "game": key.game,
        "card_name": key.card_name,
        "normalized_card_name": key.normalized_card_name,
        "set_name": key.set_name,
        "set_code": key.set_code,
        "collector_number": key.collector_number,
        "language": key.language,
        "variant": key.variant,
        "condition": key.condition,
        "market_country": key.market_country,
        "currency": key.currency,
        "fingerprint": key.fingerprint,
    }


def _load_candidates(client: Any, *, limit: int) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in client.list_missing_cache_keys(limit=limit, min_popularity_score=0, min_inventory_count=0):
        key_id = str(row.get("id", "")).strip()
        if key_id:
            rows[key_id] = {**row, "candidate_type": "missing_cache", "has_cache": False}
    if hasattr(client, "list_cache_refresh_candidates"):
        cache_rows = client.list_cache_refresh_candidates(limit=limit, min_popularity_score=0, min_inventory_count=0)
    else:
        cache_rows = client.list_stale_cache_keys(stale_before_iso=utc_iso(utc_now()), limit=limit)
    for row in cache_rows:
        key_id = str(row.get("id", "")).strip()
        if key_id:
            rows[key_id] = {**row, "candidate_type": "stale_cache", "has_cache": True}
    return list(rows.values())[:limit]


def _daily_count(client: Any) -> tuple[int, str]:
    if hasattr(client, "count_live_scheduler_jobs_today"):
        return int(client.count_live_scheduler_jobs_today()), "database"
    return 0, "not_available_report_only"


def evaluate_candidate(
    candidate: dict[str, Any],
    *,
    allowed_markets: list[str],
    active_job: dict[str, Any] | None,
    now: datetime,
) -> tuple[bool, str]:
    market = str(candidate.get("market_country") or "").upper()
    if market not in allowed_markets:
        return False, "market_not_allowed"
    if active_job:
        return False, "active_job_exists"
    if not candidate.get("has_cache"):
        return True, "missing_cache"
    stale_after = _parse_utc(candidate.get("stale_after"))
    if stale_after and stale_after <= now:
        return True, "stale_cache"
    return False, "not_stale"


def run_live_scheduler(args: argparse.Namespace, *, client: Any | None = None, now_func: Any = utc_now) -> dict[str, Any]:
    started = now_func()
    engine_config = MarketEngineConfig.from_env(require_supabase=True)
    base_live_config = LiveSchedulerConfig.from_env()
    cli_markets = parse_market_allowlist(args.markets) if getattr(args, "markets", None) else None
    dry_run = True if getattr(args, "dry_run", None) else None
    if getattr(args, "real_enqueue", False):
        dry_run = False
    live_config = base_live_config.with_overrides(markets=cli_markets, max_enqueues=getattr(args, "max_enqueues", None), dry_run=dry_run)
    if not live_config.dry_run:
        missing = _require_real_enqueue_flags(engine_config=engine_config, live_config=live_config)
        if missing:
            raise RuntimeError("Real live scheduler enqueue refused; missing/invalid flags: " + ", ".join(missing))
    provider_flag_warnings = _require_live_provider_flags(engine_config)
    client = client or SupabaseMarketEngineClient(
        supabase_url=engine_config.supabase_url,
        service_role_key=engine_config.supabase_service_role_key,
        timeout_seconds=60,
    )
    daily_count, daily_count_source = _daily_count(client)
    candidates = _load_candidates(client, limit=live_config.max_keys_scanned_per_run)
    active_jobs = client.get_active_jobs_for_keys(price_key_ids=[str(item.get("id")) for item in candidates])
    decisions: list[dict[str, Any]] = []
    enqueues = 0
    skipped: dict[str, int] = {}
    for candidate in candidates:
        key_id = str(candidate.get("id", "")).strip()
        active_job = active_jobs.get(key_id)
        should_enqueue, reason = evaluate_candidate(candidate, allowed_markets=live_config.markets, active_job=active_job, now=started)
        if daily_count + enqueues >= live_config.daily_enqueue_cap:
            should_enqueue = False
            reason = "daily_cap_reached"
        if enqueues >= live_config.max_enqueues_per_run and should_enqueue:
            should_enqueue = False
            reason = "max_enqueues_reached"
        entry: dict[str, Any] = {
            "price_key_id": key_id,
            "fingerprint": candidate.get("fingerprint"),
            "market_country": str(candidate.get("market_country") or "").upper(),
            "currency": str(candidate.get("currency") or "").upper(),
            "candidate_type": candidate.get("candidate_type"),
            "decision": "would_enqueue" if should_enqueue and live_config.dry_run else "enqueue" if should_enqueue else "skip",
            "reason": reason,
            "active_job": active_job,
        }
        if should_enqueue:
            key = client.get_price_key(key_id)
            identity = _identity_from_key(key)
            if live_config.dry_run:
                entry["request_market_price_refresh"] = {"action": "dry_run_only", "force_refresh": False}
            else:
                refresh = client.request_market_price_refresh(
                    **identity,
                    reason="live_ebay_scheduler",
                    force_refresh=False,
                )
                entry["request_market_price_refresh"] = refresh
                if refresh.get("action") == "job_enqueued":
                    enqueues += 1
                elif refresh.get("action") == "cache_fresh":
                    entry["reason"] = "cache_fresh"
                    skipped["cache_fresh"] = skipped.get("cache_fresh", 0) + 1
                    decisions.append(entry)
                    continue
            if live_config.dry_run:
                enqueues += 1
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
        decisions.append(entry)
    return sanitize_provider_diagnostics(
        {
            "status": "success",
            "dryRun": live_config.dry_run,
            "startedAtUtc": utc_iso(started),
            "finishedAtUtc": utc_iso(now_func()),
            "liveSchedulerEnabled": live_config.enabled,
            "liveSchedulerConfirmed": live_config.confirmed,
            "providerFlagWarnings": provider_flag_warnings,
            "limits": {
                "markets": live_config.markets,
                "maxEnqueuesPerRun": live_config.max_enqueues_per_run,
                "maxKeysScannedPerRun": live_config.max_keys_scanned_per_run,
                "minCooldownHours": live_config.min_cooldown_hours,
                "allowForceRefresh": live_config.allow_force_refresh,
                "dailyEnqueueCap": live_config.daily_enqueue_cap,
            },
            "dailyCap": {"usedToday": daily_count, "source": daily_count_source},
            "summary": {
                "candidatesScanned": len(candidates),
                "jobsEnqueued": 0 if live_config.dry_run else enqueues,
                "jobsWouldEnqueue": enqueues if live_config.dry_run else 0,
                "skipped": skipped,
            },
            "candidateDecisions": decisions,
        }
    )


def main() -> int:
    args = parse_args()
    try:
        report = run_live_scheduler(args)
    except Exception as exc:
        report = sanitize_provider_diagnostics(
            {"status": "failed", "error": str(exc), "error_type": type(exc).__name__, "finishedAtUtc": utc_iso(utc_now())}
        )
        write_json(LATEST_REPORT, report)
        append_jsonl(RUNS_REPORT, report)
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        return 1
    write_json(LATEST_REPORT, report)
    append_jsonl(RUNS_REPORT, report)
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
