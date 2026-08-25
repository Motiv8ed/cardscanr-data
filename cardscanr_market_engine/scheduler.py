from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any

from .config import REPORTS_DIR, supabase_secret_key_from_env
from .marketplace_ops_state import get_active_cooldown
from .refresh_policy import RefreshCooldownConfig, calculate_refresh_policy
from .smoke_utils import append_jsonl, sanitize_for_report, write_json


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _parse_non_negative_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def parse_market_allowlist(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Parse a comma-separated market allowlist. Empty means all markets."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = str(value).split(",")
    markets: list[str] = []
    for raw in raw_items:
        market = str(raw).strip().upper()
        if market and market not in markets:
            markets.append(market)
    return markets


def _parse_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _float_or_zero(value: Any) -> float:
    if value is None or value is False:
        return 0.0
    try:
        return float(value)
    except Exception:
        return 0.0


def _extract_cache_marketplace(value: Any) -> str | None:
    if isinstance(value, list):
        if not value:
            return None
        first = value[0]
        if isinstance(first, dict):
            return first.get("marketplace")
        return None
    if isinstance(value, dict):
        return value.get("marketplace")
    return None


@dataclass(frozen=True)
class MarketSchedulerConfig:
    supabase_url: str
    supabase_service_role_key: str
    max_keys_per_run: int
    max_enqueues_per_run: int
    include_missing_cache: bool
    include_stale_cache: bool
    min_popularity_score: int
    min_inventory_count: int
    dry_run: bool
    poll_seconds: int
    allowed_markets: list[str]
    latest_report_path: Path
    runs_report_path: Path

    @classmethod
    def from_env(cls, *, require_supabase: bool = True) -> "MarketSchedulerConfig":
        supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        supabase_service_role_key = supabase_secret_key_from_env()
        if require_supabase:
            if not supabase_url:
                raise ValueError("SUPABASE_URL is required")
            if not supabase_service_role_key:
                raise ValueError("SUPABASE_SECRET_KEY is required")
        return cls(
            supabase_url=supabase_url,
            supabase_service_role_key=supabase_service_role_key,
            max_keys_per_run=_parse_positive_int("MARKET_SCHEDULER_MAX_KEYS_PER_RUN", 100),
            max_enqueues_per_run=_parse_positive_int("MARKET_SCHEDULER_MAX_ENQUEUES_PER_RUN", 50),
            include_missing_cache=_parse_bool("MARKET_SCHEDULER_INCLUDE_MISSING_CACHE", True),
            include_stale_cache=_parse_bool("MARKET_SCHEDULER_INCLUDE_STALE_CACHE", True),
            min_popularity_score=_parse_non_negative_int("MARKET_SCHEDULER_MIN_POPULARITY_SCORE", 0),
            min_inventory_count=_parse_non_negative_int("MARKET_SCHEDULER_MIN_INVENTORY_COUNT", 0),
            dry_run=_parse_bool("MARKET_SCHEDULER_DRY_RUN", False),
            poll_seconds=_parse_positive_int("MARKET_SCHEDULER_POLL_SECONDS", 300),
            allowed_markets=parse_market_allowlist(os.getenv("MARKET_SCHEDULER_ALLOWED_MARKETS", "")),
            latest_report_path=REPORTS_DIR / "market_price_scheduler_latest.json",
            runs_report_path=REPORTS_DIR / "market_price_scheduler_runs.jsonl",
        )


@dataclass(frozen=True)
class SchedulerDecision:
    should_enqueue: bool
    priority: int | None
    reason: str
    score: int
    details: dict[str, Any]


def sanitize_scheduler_report(payload: dict[str, Any]) -> dict[str, Any]:
    clean = sanitize_for_report(payload)
    if not isinstance(clean, dict):
        return {"status": "failed", "error": "invalid_report_shape"}
    return clean


class MarketPriceRefreshScheduler:
    def __init__(
        self,
        *,
        client: Any,
        config: MarketSchedulerConfig,
        now_func: Any = utc_now,
    ) -> None:
        self.client = client
        self.config = config
        self.now_func = now_func

    def _is_recent(self, last_seen_at: datetime | None, *, now: datetime) -> bool:
        return bool(last_seen_at and last_seen_at >= (now - timedelta(days=14)))

    def _is_old(self, last_seen_at: datetime | None, *, now: datetime) -> bool:
        return bool(last_seen_at and last_seen_at <= (now - timedelta(days=60)))

    def _candidate_sort_key(self, item: dict[str, Any]) -> tuple[int, float, float, str]:
        seen = _parse_utc(item.get("last_seen_at"))
        seen_ts = seen.timestamp() if seen else float("-inf")
        due = _parse_utc(item.get("next_refresh_due_at")) or _parse_utc(item.get("stale_after"))
        due_ts = due.timestamp() if due else float("-inf")
        type_rank = {
            "missing_cache": 0,
            "missing_price": 0,
            "failed_cache": 1,
            "stale_cache": 2,
        }.get(str(item.get("candidate_type") or ""), 3)
        return (type_rank, due_ts, -seen_ts, str(item.get("id")))

    def evaluate_candidate(self, candidate: dict[str, Any], *, now: datetime) -> SchedulerDecision:
        market_country = str(candidate.get("market_country") or "").strip().upper()
        if self.config.allowed_markets and market_country and market_country not in self.config.allowed_markets:
            return SchedulerDecision(
                should_enqueue=False,
                priority=None,
                reason="market_not_allowed",
                score=0,
                details={
                    "market_country": market_country or None,
                    "allowed_markets": list(self.config.allowed_markets),
                },
            )
        cooldown = get_active_cooldown(market_country, now=now) if market_country else None
        if cooldown is not None:
            return SchedulerDecision(
                should_enqueue=False,
                priority=None,
                reason="marketplace_cooldown",
                score=0,
                details={
                    "market_country": market_country or None,
                    "cooldown_reason": cooldown.reason,
                    "cooldown_until": utc_iso(cooldown.until),
                    "last_error": cooldown.last_error,
                },
            )
        has_cache = bool(candidate.get("has_cache"))
        stale_after = _parse_utc(candidate.get("stale_after"))
        next_refresh_due_at = _parse_utc(candidate.get("next_refresh_due_at"))
        due_at = next_refresh_due_at or stale_after
        is_due = bool(due_at and due_at <= now)
        is_stale = bool(stale_after and stale_after <= now)
        popularity_score = max(0, int(candidate.get("popularity_score") or 0))
        inventory_count = max(0, int(candidate.get("inventory_count") or 0))
        last_seen_at = _parse_utc(candidate.get("last_seen_at"))
        current_market_price_raw = candidate.get("current_market_price")
        current_market_price = _float_or_zero(current_market_price_raw)
        has_usable_price = current_market_price_raw is not None and current_market_price_raw is not False
        recommended_price = _float_or_zero(candidate.get("recommended_price"))
        value_signal = max(current_market_price, recommended_price)
        high_value = value_signal >= 100
        popular = popularity_score >= 15 or inventory_count >= 8
        recently_seen = self._is_recent(last_seen_at, now=now)
        very_old = self._is_old(last_seen_at, now=now)
        last_updated_at = _parse_utc(candidate.get("last_updated_at"))
        refresh_status = str(candidate.get("refresh_status") or "").strip().lower()
        score = 0
        if not has_cache or not has_usable_price:
            score += 1000
        if refresh_status == "failed":
            score += 700
        if is_due or is_stale:
            score += 450
        score += min(popularity_score * 5, 250)
        score += min(inventory_count * 4, 180)
        score += min(int(value_signal), 300)
        if recently_seen:
            score += 125
        if high_value:
            score += 75
        if due_at is not None:
            # Prefer oldest overdue first within the same priority band.
            overdue_hours = max(0.0, (now - due_at).total_seconds() / 3600.0)
            score += min(int(overdue_hours), 200)

        details = {
            "has_cache": has_cache,
            "has_usable_price": has_usable_price,
            "stale_after": utc_iso(stale_after) if stale_after else None,
            "next_refresh_due_at": utc_iso(next_refresh_due_at) if next_refresh_due_at else None,
            "is_due": is_due,
            "is_stale": is_stale,
            "last_updated_at": utc_iso(last_updated_at) if last_updated_at else None,
            "current_market_price": current_market_price if has_usable_price else None,
            "recommended_price": recommended_price,
            "value_signal": value_signal,
            "popularity_score": popularity_score,
            "inventory_count": inventory_count,
            "last_seen_at": utc_iso(last_seen_at) if last_seen_at else None,
            "recently_seen": recently_seen,
            "very_old": very_old,
            "refresh_status": refresh_status or None,
            "score": score,
            "market_country": market_country or None,
            "allowed_markets": list(self.config.allowed_markets),
        }

        # Priority 1: no usable current price / no cache row.
        if not has_cache:
            reason = "missing_cache_recent" if recently_seen else "missing_cache"
            return SchedulerDecision(should_enqueue=True, priority=50, reason=reason, score=score, details=details)
        if not has_usable_price:
            reason = "missing_price_recent" if recently_seen else "missing_price"
            return SchedulerDecision(should_enqueue=True, priority=50, reason=reason, score=score, details=details)

        policy = calculate_refresh_policy(
            cache_row={
                "last_updated_at": candidate.get("last_updated_at"),
                "current_market_price": candidate.get("current_market_price"),
                "recommended_price": candidate.get("recommended_price"),
            },
            price_key_row={
                "popularity_score": popularity_score,
                "inventory_count": inventory_count,
            },
            active_job=None,
            now=now,
            request_reason="scheduler",
            force=False,
            config=RefreshCooldownConfig.from_env(),
        )
        details.update(
            {
                "cooldown_hours": policy.cooldown_hours,
                "cooldown_until": utc_iso(policy.cooldown_until) if policy.cooldown_until else None,
                "cooldown_reason": policy.reason,
                "cache_is_fresh": policy.cache_is_fresh,
            }
        )

        # Never auto-refresh while next due remains in the future.
        if due_at is not None and due_at > now and refresh_status != "failed":
            return SchedulerDecision(
                should_enqueue=False,
                priority=None,
                reason="not_due_yet",
                score=score,
                details=details,
            )

        if refresh_status == "failed":
            if policy.is_in_cooldown:
                return SchedulerDecision(
                    should_enqueue=False,
                    priority=None,
                    reason="failed_in_cooldown",
                    score=score,
                    details=details,
                )
            return SchedulerDecision(
                should_enqueue=True,
                priority=60,
                reason="failed_retryable",
                score=score,
                details=details,
            )

        if not policy.can_refresh:
            return SchedulerDecision(
                should_enqueue=False,
                priority=None,
                reason="fresh_cache",
                score=score,
                details=details,
            )

        if not is_due and not is_stale:
            return SchedulerDecision(
                should_enqueue=False,
                priority=None,
                reason="not_due_yet",
                score=score,
                details=details,
            )

        if high_value:
            return SchedulerDecision(
                should_enqueue=True,
                priority=80,
                reason="stale_high_value_cache",
                score=score,
                details=details,
            )
        if popular:
            return SchedulerDecision(
                should_enqueue=True,
                priority=90,
                reason="stale_popular_cache",
                score=score,
                details=details,
            )
        if very_old:
            return SchedulerDecision(
                should_enqueue=True,
                priority=100,
                reason="stale_old_background",
                score=score,
                details=details,
            )
        return SchedulerDecision(
            should_enqueue=True,
            priority=100,
            reason="stale_background_refresh",
            score=score,
            details=details,
        )

    def _load_candidates(self, *, now: datetime) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        fetch_limit = max(self.config.max_keys_per_run, 1)
        if self.config.include_missing_cache:
            for row in self.client.list_missing_cache_keys(
                limit=fetch_limit,
                min_popularity_score=self.config.min_popularity_score,
                min_inventory_count=self.config.min_inventory_count,
            ):
                key_id = str(row.get("id", "")).strip()
                if not key_id:
                    continue
                rows[key_id] = {
                    "id": key_id,
                    "fingerprint": row.get("fingerprint"),
                    "market_country": row.get("market_country"),
                    "currency": row.get("currency"),
                    "marketplace": _extract_cache_marketplace(row.get("market_price_cache")),
                    "popularity_score": row.get("popularity_score") or 0,
                    "inventory_count": row.get("inventory_count") or 0,
                    "last_seen_at": row.get("last_seen_at"),
                    "has_cache": False,
                    "stale_after": None,
                    "next_refresh_due_at": None,
                    "current_market_price": None,
                    "recommended_price": None,
                    "refresh_status": None,
                    "candidate_type": "missing_cache",
                }
        if self.config.include_stale_cache:
            if hasattr(self.client, "list_cache_refresh_candidates"):
                cache_rows = self.client.list_cache_refresh_candidates(
                    limit=fetch_limit,
                    min_popularity_score=self.config.min_popularity_score,
                    min_inventory_count=self.config.min_inventory_count,
                    due_before_iso=utc_iso(now),
                )
            else:
                cache_rows = self.client.list_stale_cache_keys(
                    stale_before_iso=utc_iso(now),
                    limit=fetch_limit,
                    min_popularity_score=self.config.min_popularity_score,
                    min_inventory_count=self.config.min_inventory_count,
                )
            for row in cache_rows:
                key_id = str(row.get("id", "")).strip()
                if not key_id:
                    continue
                has_price = row.get("current_market_price") is not None
                refresh_status = str(row.get("refresh_status") or "").strip().lower()
                if not has_price:
                    candidate_type = "missing_price"
                elif refresh_status == "failed":
                    candidate_type = "failed_cache"
                else:
                    candidate_type = "stale_cache"
                # Prefer missing-cache classification if already present.
                if key_id in rows and rows[key_id].get("candidate_type") == "missing_cache":
                    continue
                rows[key_id] = {
                    "id": key_id,
                    "fingerprint": row.get("fingerprint"),
                    "market_country": row.get("market_country"),
                    "currency": row.get("currency"),
                    "marketplace": row.get("marketplace"),
                    "popularity_score": row.get("popularity_score") or 0,
                    "inventory_count": row.get("inventory_count") or 0,
                    "last_seen_at": row.get("last_seen_at"),
                    "has_cache": True,
                    "stale_after": row.get("stale_after"),
                    "next_refresh_due_at": row.get("next_refresh_due_at"),
                    "current_market_price": row.get("current_market_price"),
                    "recommended_price": row.get("recommended_price"),
                    "last_updated_at": row.get("last_updated_at"),
                    "refresh_status": row.get("refresh_status"),
                    "candidate_type": candidate_type,
                }
        candidates = list(rows.values())
        candidates.sort(key=self._candidate_sort_key)
        return candidates[: self.config.max_keys_per_run]

    def run_once(self) -> dict[str, Any]:
        now = self.now_func()
        started_at = utc_iso(now)
        recovered_jobs: list[dict[str, Any]] = []
        if hasattr(self.client, "recover_abandoned_refresh_jobs") and not self.config.dry_run:
            try:
                recovered_jobs = self.client.recover_abandoned_refresh_jobs(
                    stale_after_minutes=int(os.getenv("MARKET_SCHEDULER_STALE_LOCK_MINUTES", "90")),
                    max_jobs=25,
                )
            except Exception as exc:  # pragma: no cover - defensive ops path
                recovered_jobs = [{"error": str(exc)[:300]}]

        raw_candidates = self._load_candidates(now=now)
        active_jobs = self.client.get_active_jobs_for_keys(price_key_ids=[str(item["id"]) for item in raw_candidates])
        decisions: list[dict[str, Any]] = []
        for candidate in raw_candidates:
            decision = self.evaluate_candidate(candidate, now=now)
            candidate_id = str(candidate["id"])
            active_job = active_jobs.get(candidate_id)
            decisions.append(
                {
                    "price_key_id": candidate_id,
                    "fingerprint": candidate.get("fingerprint"),
                    "market_country": candidate.get("market_country"),
                    "currency": candidate.get("currency"),
                    "marketplace": candidate.get("marketplace"),
                    "candidate_type": candidate.get("candidate_type"),
                    "decision": decision,
                    "active_job": active_job,
                    "last_seen_at": candidate.get("last_seen_at"),
                }
            )
        decisions.sort(
            key=lambda item: (
                item["decision"].priority if item["decision"].priority is not None else 999,
                -item["decision"].score,
                _parse_utc(item.get("last_seen_at")) or datetime.min.replace(tzinfo=timezone.utc),
                item["price_key_id"],
            ),
            reverse=False,
        )

        enqueues_done = 0
        skipped_active = 0
        skipped_limits = 0
        skipped_fresh = 0
        skipped_market = 0
        skipped_deduped = 0
        dry_run_candidates = 0
        enqueued_jobs: list[dict[str, Any]] = []
        top_reason_counts: dict[str, int] = {}

        for item in decisions:
            decision: SchedulerDecision = item["decision"]
            reason_key = decision.reason
            top_reason_counts[reason_key] = top_reason_counts.get(reason_key, 0) + 1
            if not decision.should_enqueue:
                if decision.reason == "market_not_allowed":
                    skipped_market += 1
                elif decision.reason == "marketplace_cooldown":
                    skipped_market += 1
                else:
                    skipped_fresh += 1
                continue
            if item["active_job"] is not None:
                skipped_active += 1
                continue
            if enqueues_done >= self.config.max_enqueues_per_run:
                skipped_limits += 1
                continue

            # Stable automatic dedupe key: one active automatic job per key.
            dedupe_key = f"scheduler:auto:{item['price_key_id']}"

            if self.config.dry_run:
                dry_run_candidates += 1
                enqueues_done += 1
                enqueued_jobs.append(
                    {
                        "price_key_id": item["price_key_id"],
                        "fingerprint": item["fingerprint"],
                        "market_country": item.get("market_country"),
                        "currency": item.get("currency"),
                        "marketplace": item.get("marketplace"),
                        "priority": decision.priority,
                        "reason": decision.reason,
                        "status": "dry_run_only",
                        "score": decision.score,
                        "dedupe_key": dedupe_key,
                    }
                )
                continue

            job_row = self.client.enqueue_refresh_job(
                price_key_id=item["price_key_id"],
                reason=f"scheduler:{decision.reason}",
                priority=int(decision.priority or 100),
                dedupe_key=dedupe_key,
            )
            # If the unique active-job index returned an existing job, count as deduped.
            existing_status = str(job_row.get("status", "unknown"))
            if existing_status in {"queued", "running"} and str(job_row.get("dedupe_key") or "") != dedupe_key:
                skipped_deduped += 1
            enqueues_done += 1
            enqueued_jobs.append(
                {
                    "price_key_id": item["price_key_id"],
                    "fingerprint": item["fingerprint"],
                    "market_country": item.get("market_country"),
                    "currency": item.get("currency"),
                    "marketplace": item.get("marketplace"),
                    "priority": decision.priority,
                    "reason": decision.reason,
                    "status": existing_status,
                    "job_id": str(job_row.get("id", "")),
                    "score": decision.score,
                    "dedupe_key": dedupe_key,
                }
            )

        eligible_count = sum(1 for item in decisions if item["decision"].should_enqueue)
        report = {
            "status": "success",
            "startedAtUtc": started_at,
            "finishedAtUtc": utc_iso(self.now_func()),
            "dryRun": self.config.dry_run,
            "limits": {
                "maxKeysPerRun": self.config.max_keys_per_run,
                "maxEnqueuesPerRun": self.config.max_enqueues_per_run,
                "includeMissingCache": self.config.include_missing_cache,
                "includeStaleCache": self.config.include_stale_cache,
                "minPopularityScore": self.config.min_popularity_score,
                "minInventoryCount": self.config.min_inventory_count,
                "allowedMarkets": list(self.config.allowed_markets),
            },
            "summary": {
                "candidatesScanned": len(raw_candidates),
                "keysEligible": eligible_count,
                "jobsEnqueued": 0 if self.config.dry_run else enqueues_done,
                "jobsSkippedAlreadyActive": skipped_active,
                "jobsSkippedByLimit": skipped_limits,
                "jobsSkippedFresh": skipped_fresh,
                "jobsSkippedMarket": skipped_market,
                "jobsSkippedDeduped": skipped_deduped,
                "jobsDryRunOnly": dry_run_candidates,
                "abandonedJobsRecovered": len(
                    [row for row in recovered_jobs if isinstance(row, dict) and "error" not in row]
                ),
            },
            "recoveredAbandonedJobs": [
                {
                    "id": row.get("id"),
                    "price_key_id": row.get("price_key_id"),
                    "status": row.get("status"),
                    "error_message": row.get("error_message"),
                }
                for row in recovered_jobs
                if isinstance(row, dict)
            ][:25],
            "topPriorityReasons": [
                {"reason": reason, "count": count}
                for reason, count in sorted(top_reason_counts.items(), key=lambda item: (-item[1], item[0]))
            ][:10],
            "enqueuedJobs": enqueued_jobs,
            "candidateDecisions": [
                {
                    "price_key_id": item["price_key_id"],
                    "fingerprint": item["fingerprint"],
                    "market_country": item.get("market_country"),
                    "currency": item.get("currency"),
                    "marketplace": item.get("marketplace"),
                    "candidate_type": item["candidate_type"],
                    "has_active_job": item["active_job"] is not None,
                    "decision": {
                        "should_enqueue": item["decision"].should_enqueue,
                        "priority": item["decision"].priority,
                        "reason": item["decision"].reason,
                        "score": item["decision"].score,
                        "details": item["decision"].details,
                    },
                }
                for item in decisions[: self.config.max_keys_per_run]
            ],
        }
        if hasattr(self.client, "upsert_pipeline_heartbeat"):
            try:
                self.client.upsert_pipeline_heartbeat(
                    component="scheduler",
                    worker_id=os.getenv("MARKET_SCHEDULER_WORKER_ID", "market-price-scheduler"),
                    state="idle" if enqueues_done == 0 else "enqueued",
                    meta={
                        "startedAtUtc": started_at,
                        "finishedAtUtc": report["finishedAtUtc"],
                        "candidatesScanned": len(raw_candidates),
                        "keysEligible": eligible_count,
                        "jobsEnqueued": report["summary"]["jobsEnqueued"],
                        "jobsSkippedDeduped": skipped_deduped,
                        "abandonedJobsRecovered": report["summary"]["abandonedJobsRecovered"],
                        "dryRun": self.config.dry_run,
                    },
                )
            except Exception:
                pass
        return report

    def run_and_write_reports(self) -> dict[str, Any]:
        report = self.run_once()
        clean_report = sanitize_scheduler_report(report)
        write_json(self.config.latest_report_path, clean_report)
        append_jsonl(self.config.runs_report_path, clean_report)
        return clean_report
