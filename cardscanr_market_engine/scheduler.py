from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any

from .config import REPORTS_DIR
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


def _extract_cache_marketplace(value: Any) -> Any:
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
    latest_report_path: Path
    runs_report_path: Path

    @classmethod
    def from_env(cls, *, require_supabase: bool = True) -> "MarketSchedulerConfig":
        supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        supabase_service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        if require_supabase:
            if not supabase_url:
                raise ValueError("SUPABASE_URL is required")
            if not supabase_service_role_key:
                raise ValueError("SUPABASE_SERVICE_ROLE_KEY is required")
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

    def _candidate_sort_key(self, item: dict[str, Any]) -> tuple[int, float, str]:
        seen = _parse_utc(item.get("last_seen_at"))
        seen_ts = seen.timestamp() if seen else float("-inf")
        return (0 if item.get("candidate_type") == "missing_cache" else 1, -seen_ts, str(item.get("id")))

    def evaluate_candidate(self, candidate: dict[str, Any], *, now: datetime) -> SchedulerDecision:
        has_cache = bool(candidate.get("has_cache"))
        stale_after = _parse_utc(candidate.get("stale_after"))
        is_stale = bool(stale_after and stale_after <= now)
        popularity_score = max(0, int(candidate.get("popularity_score") or 0))
        inventory_count = max(0, int(candidate.get("inventory_count") or 0))
        last_seen_at = _parse_utc(candidate.get("last_seen_at"))
        current_market_price = _float_or_zero(candidate.get("current_market_price"))
        recommended_price = _float_or_zero(candidate.get("recommended_price"))
        value_signal = max(current_market_price, recommended_price)
        high_value = value_signal >= 100
        popular = popularity_score >= 15 or inventory_count >= 8
        recently_seen = self._is_recent(last_seen_at, now=now)
        very_old = self._is_old(last_seen_at, now=now)
        score = 0
        if not has_cache:
            score += 1000
        if is_stale:
            score += 450
        score += min(popularity_score * 5, 250)
        score += min(inventory_count * 4, 180)
        score += min(int(value_signal), 300)
        if recently_seen:
            score += 125
        if high_value:
            score += 75

        details = {
            "has_cache": has_cache,
            "stale_after": utc_iso(stale_after) if stale_after else None,
            "is_stale": is_stale,
            "current_market_price": current_market_price,
            "recommended_price": recommended_price,
            "value_signal": value_signal,
            "popularity_score": popularity_score,
            "inventory_count": inventory_count,
            "last_seen_at": utc_iso(last_seen_at) if last_seen_at else None,
            "recently_seen": recently_seen,
            "very_old": very_old,
            "score": score,
        }

        if not has_cache:
            reason = "missing_cache_recent" if recently_seen else "missing_cache"
            return SchedulerDecision(should_enqueue=True, priority=50, reason=reason, score=score, details=details)

        if not is_stale:
            return SchedulerDecision(
                should_enqueue=False,
                priority=None,
                reason="fresh_cache",
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
                    "current_market_price": None,
                    "recommended_price": None,
                    "candidate_type": "missing_cache",
                }
        if self.config.include_stale_cache:
            for row in self.client.list_stale_cache_keys(
                stale_before_iso=utc_iso(now),
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
                    "marketplace": row.get("marketplace"),
                    "popularity_score": row.get("popularity_score") or 0,
                    "inventory_count": row.get("inventory_count") or 0,
                    "last_seen_at": row.get("last_seen_at"),
                    "has_cache": True,
                    "stale_after": row.get("stale_after"),
                    "current_market_price": row.get("current_market_price"),
                    "recommended_price": row.get("recommended_price"),
                    "candidate_type": "stale_cache",
                }
        candidates = list(rows.values())
        candidates.sort(
            key=self._candidate_sort_key,
        )
        return candidates[: self.config.max_keys_per_run]

    def run_once(self) -> dict[str, Any]:
        now = self.now_func()
        started_at = utc_iso(now)
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
        dry_run_candidates = 0
        enqueued_jobs: list[dict[str, Any]] = []
        top_reason_counts: dict[str, int] = {}

        for item in decisions:
            decision: SchedulerDecision = item["decision"]
            reason_key = decision.reason
            top_reason_counts[reason_key] = top_reason_counts.get(reason_key, 0) + 1
            if not decision.should_enqueue:
                skipped_fresh += 1
                continue
            if item["active_job"] is not None:
                skipped_active += 1
                continue
            if enqueues_done >= self.config.max_enqueues_per_run:
                skipped_limits += 1
                continue

            if self.config.dry_run:
                dry_run_candidates += 1
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
                    }
                )
                continue

            job_row = self.client.enqueue_refresh_job(
                price_key_id=item["price_key_id"],
                reason=f"scheduler:{decision.reason}",
                priority=int(decision.priority or 100),
                dedupe_key=f"scheduler:{started_at}:{item['price_key_id']}",
            )
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
                    "status": str(job_row.get("status", "unknown")),
                    "job_id": str(job_row.get("id", "")),
                    "score": decision.score,
                }
            )

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
            },
            "summary": {
                "candidatesScanned": len(raw_candidates),
                "jobsEnqueued": enqueues_done,
                "jobsSkippedAlreadyActive": skipped_active,
                "jobsSkippedByLimit": skipped_limits,
                "jobsSkippedFresh": skipped_fresh,
                "jobsDryRunOnly": dry_run_candidates,
            },
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
        return report

    def run_and_write_reports(self) -> dict[str, Any]:
        report = self.run_once()
        clean_report = sanitize_scheduler_report(report)
        write_json(self.config.latest_report_path, clean_report)
        append_jsonl(self.config.runs_report_path, clean_report)
        return clean_report
