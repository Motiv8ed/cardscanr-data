"""Daily owned-printing × home-market price refresh scheduler.

Eligibility is based on last successful refresh age (≥24h), not calendar day.
Failed refreshes never count as success. Catalogue-only keys are out of scope.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Any

from .config import REPORTS_DIR, supabase_secret_key_from_env
from .marketplace_ops_state import get_active_cooldown
from .ebay_availability import (
    EBAY_AVAILABILITY_COOLDOWN,
    browser_work_allowed,
)
from .owned_daily_enablement import owned_daily_full_enable
from .owned_daily_pacing import OwnedDailyPacingConfig
from .queue_capacity import QueueWatermarks, enqueue_budget
from .scheduler import (
    SchedulerDecision,
    _parse_bool,
    _parse_non_negative_int,
    _parse_positive_int,
    _parse_utc,
    parse_market_allowlist,
    sanitize_scheduler_report,
    utc_iso,
    utc_now,
)
from .demand_aware_policy import DEFAULT_DEMAND_AWARE_POLICY
from .demand_aware_scheduler import (
    DemandIndex,
    ExplainableSchedulerRow,
    evaluate_demand_aware_target,
    events_from_job_rows,
    select_fair_lane_mix,
)
from .owned_daily_source_policy import classify_owned_daily_band
from .smoke_utils import append_jsonl, write_json

PRIORITY_BY_BAND: dict[str, int] = {
    "P0_NEVER_PRICED": 20,
    # Same enqueue priority as never-priced: reference-only still needs first eBay verify.
    "P0_NEEDS_VERIFIED_LOCAL": 20,
    "P1_STALE_GT_24H": 40,
    "P2_FAILED_RETRY": 50,
    "P3_APPROACHING_DUE": 90,
}

BAND_RANK: dict[str, int] = {
    "P0_NEVER_PRICED": 0,
    "P0_NEEDS_VERIFIED_LOCAL": 0,
    "P1_STALE_GT_24H": 1,
    "P2_FAILED_RETRY": 2,
    "P3_APPROACHING_DUE": 3,
}


def _float_or_none(value: Any) -> float | None:
    if value is None or value is False:
        return None
    try:
        return float(value)
    except Exception:
        return None


@dataclass(frozen=True)
class OwnedDailySchedulerConfig:
    supabase_url: str
    supabase_service_role_key: str
    max_enqueues_per_run: int
    queue_low_watermark: int
    queue_high_watermark: int
    dry_run: bool
    sync_keys_before_run: bool
    allowed_markets: list[str]
    rolling_window_hours: int
    success_fresh_hours: int
    latest_report_path: Path
    runs_report_path: Path
    enable_full_daily: bool

    @classmethod
    def from_env(cls, *, require_supabase: bool = True) -> "OwnedDailySchedulerConfig":
        supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        supabase_service_role_key = supabase_secret_key_from_env()
        if require_supabase:
            if not supabase_url:
                raise ValueError("SUPABASE_URL is required")
            if not supabase_service_role_key:
                raise ValueError("SUPABASE_SECRET_KEY is required")
        pilot_cap = _parse_positive_int("OWNED_DAILY_MAX_ENQUEUE", 25)
        full_enable = owned_daily_full_enable()
        if full_enable:
            # Continuous AU: concurrency 1 — enqueue at most one due card per cycle.
            full_cap = _parse_non_negative_int("OWNED_DAILY_FULL_MAX_ENQUEUE", 1)
            max_enqueues = full_cap if full_cap > 0 else 1
        else:
            max_enqueues = pilot_cap
        return cls(
            supabase_url=supabase_url,
            supabase_service_role_key=supabase_service_role_key,
            max_enqueues_per_run=max_enqueues,
            queue_low_watermark=_parse_non_negative_int("OWNED_DAILY_QUEUE_LOW_WATERMARK", 0),
            queue_high_watermark=_parse_non_negative_int("OWNED_DAILY_QUEUE_HIGH_WATERMARK", 0),
            dry_run=_parse_bool("OWNED_DAILY_DRY_RUN", False),
            sync_keys_before_run=_parse_bool("OWNED_DAILY_SYNC_KEYS", True),
            allowed_markets=parse_market_allowlist(os.getenv("OWNED_DAILY_ALLOWED_MARKETS", "AU,US,GB,CA")),
            rolling_window_hours=_parse_positive_int("OWNED_DAILY_ROLLING_WINDOW_HOURS", 24),
            success_fresh_hours=_parse_positive_int("OWNED_DAILY_SUCCESS_FRESH_HOURS", 24),
            latest_report_path=REPORTS_DIR / "owned_price_scheduler_latest.json",
            runs_report_path=REPORTS_DIR / "owned_price_scheduler_runs.jsonl",
            enable_full_daily=full_enable,
        )


class OwnedPrintingRefreshScheduler:
    """Enqueue one refresh per owned printing × home market when due."""

    def __init__(
        self,
        *,
        client: Any,
        config: OwnedDailySchedulerConfig,
        now_func: Any = utc_now,
    ) -> None:
        self.client = client
        self.config = config
        self.now_func = now_func
        self._demand_index = DemandIndex([])

    def evaluate_target(self, target: dict[str, Any], *, now: datetime) -> SchedulerDecision:
        band = str(target.get("owned_priority_band") or "").strip().upper()
        due = bool(target.get("due_for_owned_daily"))
        market = str(target.get("market_country") or "").strip().upper()
        price = _float_or_none(target.get("current_market_price"))
        last_updated = _parse_utc(target.get("last_updated_at"))
        refresh_status = str(target.get("refresh_status") or "").strip().lower()
        next_due = _parse_utc(target.get("next_refresh_due_at"))
        owner_count = max(0, int(target.get("owner_count") or 0))
        total_qty = max(0, int(target.get("total_owned_quantity") or 0))

        details: dict[str, Any] = {
            "owned_priority_band": band or None,
            "due_for_owned_daily": due,
            "market_country": market or None,
            "currency": target.get("currency"),
            "owner_count": owner_count,
            "total_owned_quantity": total_qty,
            "current_market_price": price,
            "last_updated_at": utc_iso(last_updated) if last_updated else None,
            "refresh_status": refresh_status or None,
            "next_refresh_due_at": utc_iso(next_due) if next_due else None,
            "success_fresh_hours": self.config.success_fresh_hours,
            "sample_resolution_source": target.get("sample_resolution_source"),
        }

        if owner_count <= 0:
            return SchedulerDecision(
                should_enqueue=False,
                priority=None,
                reason="no_owners",
                score=0,
                details=details,
            )

        if self.config.allowed_markets and market and market not in self.config.allowed_markets:
            return SchedulerDecision(
                should_enqueue=False,
                priority=None,
                reason="market_not_allowed",
                score=0,
                details=details,
            )

        cooldown = get_active_cooldown(market, now=now) if market else None
        if cooldown is not None:
            return SchedulerDecision(
                should_enqueue=False,
                priority=None,
                reason="marketplace_cooldown",
                score=0,
                details={
                    **details,
                    "cooldown_reason": cooldown.reason,
                    "cooldown_until": utc_iso(cooldown.until),
                },
            )

        # Worker-wide eBay availability circuit: do not consume due cards during COOLDOWN.
        # PROBE_REQUIRED: allow a single recovery-slot evaluation (worker/job_runner
        # uses for_probe); do not HOLD the entire queue permanently.
        allowed_browser, avail_reason, avail_snap = browser_work_allowed(now=now, for_probe=False)
        probe_ok, _, _ = browser_work_allowed(now=now, for_probe=True)
        probe_slot = (not allowed_browser) and probe_ok and str(avail_snap.state or "") == "PROBE_REQUIRED"
        if not allowed_browser and not probe_slot:
            return SchedulerDecision(
                should_enqueue=False,
                priority=None,
                reason=EBAY_AVAILABILITY_COOLDOWN
                if avail_reason == EBAY_AVAILABILITY_COOLDOWN
                else avail_reason,
                score=0,
                details={
                    **details,
                    "ebay_availability_state": avail_snap.state,
                    "next_probe_at": utc_iso(avail_snap.next_probe_at) if avail_snap.next_probe_at else None,
                    "consecutive_sorry_events": avail_snap.consecutive_sorry_events,
                    "owned_priority_band": "EBAY_AVAILABILITY_HOLD",
                },
            )
        if probe_slot:
            details["probeSlot"] = True
            details["ebay_availability_state"] = avail_snap.state

        # Source-aware + demand-aware owned_daily eBay policy.
        # Do NOT use cache stale_after alone — that is selected-price TTL, often
        # a short reference window, and caused RPC P1 vs Python FRESH_SKIP splits.
        overlay = {
            **target,
            "current_market_price": price,
            "last_updated_at": utc_iso(last_updated) if last_updated else target.get("last_updated_at"),
            "refresh_status": refresh_status,
            "next_refresh_due_at": utc_iso(next_due) if next_due else target.get("next_refresh_due_at"),
        }
        demand_row = evaluate_demand_aware_target(
            overlay,
            now=now,
            demand_index=self._demand_index,
            owner_count=owner_count,
        )
        band, due, reason_suffix, freshness_view, source_extras = classify_owned_daily_band(
            overlay,
            now=now,
            success_fresh_hours=demand_row.freshness_threshold_hours,
        )
        details.update(source_extras)
        details["freshness_view"] = freshness_view.to_dict()
        details["rpc_owned_priority_band"] = str(target.get("owned_priority_band") or "") or None
        details["rpc_due_for_owned_daily"] = target.get("due_for_owned_daily")
        details["demandAware"] = demand_row.to_public_dict()
        details["success_fresh_hours"] = demand_row.freshness_threshold_hours
        details["_demand_row"] = demand_row

        if not due or band == "FRESH_SKIP" or not demand_row.due:
            skip_reason = reason_suffix if str(reason_suffix).startswith("fresh_lt") else "not_due"
            if str(demand_row.reason_code).startswith("FRESH_SKIP"):
                skip_reason = "fresh_lt_24h" if demand_row.freshness_threshold_hours >= 24 else f"fresh_lt_{demand_row.freshness_threshold_hours}h"
            return SchedulerDecision(
                should_enqueue=False,
                priority=None,
                reason=skip_reason,
                score=0,
                details={**details, "owned_priority_band": "FRESH_SKIP"},
            )

        priority = PRIORITY_BY_BAND.get(band, 100)
        # Anti-starvation score: older eBay-success / more owners first within band.
        age_hours = 0.0
        if freshness_view.ebay_success_freshness_at is not None:
            age_hours = max(
                0.0,
                (now - freshness_view.ebay_success_freshness_at).total_seconds() / 3600.0,
            )
        elif band in {"P0_NEVER_PRICED", "P0_NEEDS_VERIFIED_LOCAL"}:
            age_hours = 10_000.0
        elif last_updated is not None:
            age_hours = max(0.0, (now - last_updated).total_seconds() / 3600.0)
        score = (
            (3 - BAND_RANK.get(band, 3)) * 10_000
            + min(owner_count * 100, 2_000)
            + min(total_qty * 10, 1_000)
            + min(int(age_hours), 5_000)
        )
        # Fair rolling cursor: rotate by fingerprint hash + hour window so
        # capacity-limited days do not always starve the same tail.
        fp = str(target.get("fingerprint") or target.get("market_price_key_id") or "")
        window = max(1, int(self.config.rolling_window_hours))
        hour_bucket = int(now.timestamp() // 3600) // window
        rotate = (zlib.crc32(f"{fp}:{hour_bucket}".encode("utf-8")) & 0xFFFF) % 500
        score += rotate

        score = max(score, int(demand_row.final_priority))
        details["owned_priority_band"] = band
        details["score"] = score
        details["rolling_rotate"] = rotate
        details["final_priority"] = demand_row.final_priority
        details["schedulerLane"] = demand_row.scheduler_lane
        return SchedulerDecision(
            should_enqueue=True,
            priority=priority,
            reason=f"owned_daily:{reason_suffix}",
            score=score,
            details=details,
        )

    def _ensure_key_id(self, target: dict[str, Any]) -> str | None:
        key_id = str(target.get("market_price_key_id") or "").strip()
        if key_id:
            return key_id
        if not hasattr(self.client, "ensure_market_price_key_from_owned_target"):
            return None
        return self.client.ensure_market_price_key_from_owned_target(target)

    def _load_demand_index(self, now: datetime) -> DemandIndex:
        if hasattr(self.client, "list_recent_user_demand_jobs"):
            try:
                rows = self.client.list_recent_user_demand_jobs(hours=168) or []
            except Exception:
                rows = []
            return DemandIndex(events_from_job_rows(rows))
        return DemandIndex([])

    def run_once(self) -> dict[str, Any]:
        now = self.now_func()
        started_at = utc_iso(now)
        self._demand_index = self._load_demand_index(now)
        sync_result: dict[str, Any] | None = None
        if self.config.sync_keys_before_run and hasattr(self.client, "sync_owned_market_price_keys"):
            if not self.config.dry_run:
                sync_result = self.client.sync_owned_market_price_keys()
            else:
                sync_result = {"dryRun": True, "skipped": True}

        targets_payload = self.client.list_owned_market_pricing_targets(include_zero_owners=False)
        targets = list(targets_payload.get("targets") or [])
        decisions: list[dict[str, Any]] = []
        for target in targets:
            # Overlay cache via resolved key so legacy fingerprint case mismatches
            # (GG30 vs gg30) cannot falsely classify a priced identity as P0_NEVER_PRICED.
            if hasattr(self.client, "enrich_owned_target_from_cache"):
                target = self.client.enrich_owned_target_from_cache(target)
            decision = self.evaluate_target(target, now=now)
            decisions.append({"target": target, "decision": decision})

        decisions.sort(
            key=lambda item: (
                item["decision"].priority if item["decision"].priority is not None else 999,
                -item["decision"].score,
                str(item["target"].get("fingerprint") or ""),
            )
        )

        queue_depth = 0
        if hasattr(self.client, "count_refresh_queue_depth"):
            try:
                queue_depth = int(self.client.count_refresh_queue_depth() or 0)
            except Exception:
                queue_depth = 0
        watermarks = QueueWatermarks(
            low=int(self.config.queue_low_watermark),
            high=int(self.config.queue_high_watermark),
        )
        enqueue_limit = enqueue_budget(
            queue_depth=queue_depth,
            watermarks=watermarks,
            max_enqueues_per_run=self.config.max_enqueues_per_run,
        )
        if any(
            bool((item["decision"].details or {}).get("probeSlot"))
            for item in decisions
            if item["decision"].should_enqueue
        ):
            enqueue_limit = min(int(enqueue_limit or 0), 1)

        eligible = [item for item in decisions if item["decision"].should_enqueue]
        key_ids = []
        for item in eligible:
            kid = str(item["target"].get("market_price_key_id") or "").strip()
            if kid:
                key_ids.append(kid)
        active_jobs = {}
        if key_ids and hasattr(self.client, "get_active_jobs_for_keys"):
            active_jobs = self.client.get_active_jobs_for_keys(price_key_ids=key_ids)

        enqueues_done = 0
        skipped_active = 0
        skipped_limits = 0
        skipped_fresh = 0
        skipped_no_key = 0
        enqueued_jobs: list[dict[str, Any]] = []
        top_reason_counts: dict[str, int] = {}
        metrics = {
            "OWNED_PRICE_TARGETS_SCANNED": len(targets),
            "OWNED_PRICE_DUE": len(eligible),
            "OWNED_PRICE_ENQUEUED": 0,
            "OWNED_PRICE_SKIPPED_FRESH": 0,
            "OWNED_PRICE_SKIPPED_ACTIVE": 0,
            "OWNED_PRICE_SKIPPED_LIMIT": 0,
            "OWNED_PRICE_CAPACITY_GAP": 0,
            "OWNED_PRICE_P0": 0,
            "OWNED_PRICE_P0_NEEDS_VERIFIED": 0,
            "OWNED_PRICE_P1": 0,
            "OWNED_PRICE_P2": 0,
            "OWNED_PRICE_P3": 0,
        }

        day_stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%d")

        for item in decisions:
            decision: SchedulerDecision = item["decision"]
            top_reason_counts[decision.reason] = top_reason_counts.get(decision.reason, 0) + 1
            band = str(decision.details.get("owned_priority_band") or "")
            if band == "P0_NEVER_PRICED":
                metrics["OWNED_PRICE_P0"] += 1
            elif band == "P0_NEEDS_VERIFIED_LOCAL":
                metrics["OWNED_PRICE_P0_NEEDS_VERIFIED"] += 1
            elif band == "P1_STALE_GT_24H":
                metrics["OWNED_PRICE_P1"] += 1
            elif band == "P2_FAILED_RETRY":
                metrics["OWNED_PRICE_P2"] += 1
            elif band == "P3_APPROACHING_DUE":
                metrics["OWNED_PRICE_P3"] += 1
            if not decision.should_enqueue:
                if str(decision.reason).startswith("fresh_lt") or decision.reason == "not_due":
                    skipped_fresh += 1
                    metrics["OWNED_PRICE_SKIPPED_FRESH"] += 1

        work_items: list[dict[str, Any]] = []
        for item in eligible:
            target = item["target"]
            price_key_id = self._ensure_key_id(target)
            if not price_key_id:
                skipped_no_key += 1
                continue
            if active_jobs.get(price_key_id) is not None:
                skipped_active += 1
                metrics["OWNED_PRICE_SKIPPED_ACTIVE"] += 1
                continue
            demand_row = item["decision"].details.get("_demand_row")
            if not isinstance(demand_row, ExplainableSchedulerRow):
                demand_row = evaluate_demand_aware_target(
                    target, now=now, demand_index=self._demand_index
                )
            work_items.append({**item, "price_key_id": price_key_id, "demand_row": demand_row})

        mix_rows = select_fair_lane_mix(
            [w["demand_row"] for w in work_items],
            budget=enqueue_limit,
        )
        mix_ids = [
            (r.price_key_id or "") + "|" + (r.market or "") + "|" + (r.fingerprint or "")
            for r in mix_rows
        ]
        ordered: list[dict[str, Any]] = []
        leftover: list[dict[str, Any]] = []
        used: set[str] = set()
        by_ident: dict[str, dict[str, Any]] = {}
        for w in work_items:
            ident = (
                str(w["price_key_id"])
                + "|"
                + str(w["target"].get("market_country") or "").strip().upper()
                + "|"
                + str(w["target"].get("fingerprint") or "")
            )
            by_ident[ident] = w
        for ident in mix_ids:
            w = by_ident.get(ident)
            if w is None:
                continue
            ordered.append(w)
            used.add(ident)
        for w in work_items:
            ident = (
                str(w["price_key_id"])
                + "|"
                + str(w["target"].get("market_country") or "").strip().upper()
                + "|"
                + str(w["target"].get("fingerprint") or "")
            )
            if ident not in used:
                leftover.append(w)

        capacity_gap = max(0, len(work_items) - len(ordered))
        metrics["OWNED_PRICE_CAPACITY_GAP"] = capacity_gap
        skipped_limits = len(leftover)
        metrics["OWNED_PRICE_SKIPPED_LIMIT"] = skipped_limits
        metrics["OWNED_PRICE_SKIPPED_ACTIVE"] = skipped_active

        for item in ordered:
            if enqueues_done >= enqueue_limit:
                skipped_limits += 1
                continue
            decision: SchedulerDecision = item["decision"]
            target = item["target"]
            price_key_id = item["price_key_id"]
            demand_row: ExplainableSchedulerRow = item["demand_row"]
            if not demand_row.due or not demand_row.would_hit_ebay:
                skipped_fresh += 1
                metrics["OWNED_PRICE_SKIPPED_FRESH"] += 1
                continue
            dedupe_key = f"owned_daily:{price_key_id}:{day_stamp}"
            reason = f"owned_daily:{decision.details.get('owned_priority_band')}"
            payload = {
                "price_key_id": price_key_id,
                "fingerprint": target.get("fingerprint"),
                "market_country": target.get("market_country"),
                "currency": target.get("currency"),
                "priority": decision.priority,
                "reason": reason,
                "score": decision.score,
                "dedupe_key": dedupe_key,
                "owner_count": target.get("owner_count"),
                "total_owned_quantity": target.get("total_owned_quantity"),
                "demandAware": demand_row.to_public_dict(),
            }
            if self.config.dry_run:
                enqueues_done += 1
                enqueued_jobs.append({**payload, "status": "dry_run_only"})
                continue
            job_row = self.client.enqueue_refresh_job(
                price_key_id=price_key_id,
                reason=reason,
                priority=int(decision.priority or 100),
                dedupe_key=dedupe_key,
            )
            enqueues_done += 1
            enqueued_jobs.append(
                {
                    **payload,
                    "status": str(job_row.get("status") or "unknown"),
                    "job_id": str(job_row.get("id") or ""),
                }
            )

        metrics["OWNED_PRICE_ENQUEUED"] = 0 if self.config.dry_run else enqueues_done
        if self.config.dry_run:
            metrics["OWNED_PRICE_DRY_RUN_CANDIDATES"] = enqueues_done
        # Drain/worker outcome metrics (filled by pilot/worker reports; enqueue cycle leaves zeros).
        for key in (
            "OWNED_PRICE_CHECKS_COMPLETED",
            "OWNED_PRICE_ESTIMATES_UPDATED",
            "OWNED_PRICE_ESTIMATES_UNCHANGED",
            "OWNED_PRICE_NO_NEW_EVIDENCE",
            "OWNED_PRICE_BROWSER_FAILURES",
            "OWNED_PRICE_CHALLENGES",
            "OWNED_PRICE_LAST_GOOD_RETAINED",
        ):
            metrics.setdefault(key, 0)

        health = None
        if hasattr(self.client, "owned_price_health_report"):
            try:
                health = self.client.owned_price_health_report()
            except Exception as exc:
                health = {"error": str(exc)[:300]}

        pacing_cfg = OwnedDailyPacingConfig.from_env()
        report = {
            "status": "success",
            "mode": "owned_daily",
            "startedAtUtc": started_at,
            "finishedAtUtc": utc_iso(self.now_func()),
            "dryRun": self.config.dry_run,
            "fullEnable": self.config.enable_full_daily,
            "outcomeModel": [
                "UPDATED_FROM_EBAY",
                "UNCHANGED_FROM_EBAY",
                "CHECKED_NO_NEW_EXACT_EVIDENCE",
                "TEMPORARY_BROWSER_FAILURE",
                "CHALLENGE_REQUIRED",
                "NO_PRICE_EVER_FOUND",
            ],
            "limits": {
                "maxEnqueuesPerRun": self.config.max_enqueues_per_run,
                "queueLowWatermark": self.config.queue_low_watermark,
                "queueHighWatermark": self.config.queue_high_watermark,
                "effectiveEnqueueBudget": enqueue_limit,
                "queueDepthAtStart": queue_depth,
                "allowedMarkets": list(self.config.allowed_markets),
                "successFreshHours": self.config.success_fresh_hours,
                "pacing": {
                    "minInterJobDelaySeconds": pacing_cfg.min_inter_job_delay_seconds,
                    "maxInterJobDelaySeconds": pacing_cfg.max_inter_job_delay_seconds,
                    "failureBackoffSeconds": pacing_cfg.failure_base_delay_seconds,
                    "sessionRestEveryN": pacing_cfg.session_rest_every_n_checks,
                    "sessionRestSeconds": pacing_cfg.session_rest_seconds,
                    "concurrency": 1,
                },
            },
            "syncOwnedKeys": sync_result,
            "health": health,
            "metrics": metrics,
            "summary": {
                "targetsScanned": len(targets),
                "keysEligible": len(eligible),
                "jobsEnqueued": 0 if self.config.dry_run else enqueues_done,
                "jobsDryRunOnly": enqueues_done if self.config.dry_run else 0,
                "jobsSkippedFresh": skipped_fresh,
                "jobsSkippedAlreadyActive": skipped_active,
                "jobsSkippedByLimit": skipped_limits,
                "jobsSkippedNoKey": skipped_no_key,
                "capacityGap": capacity_gap,
                "fairRollingApplied": capacity_gap > 0,
            },
            "topPriorityReasons": [
                {"reason": reason, "count": count}
                for reason, count in sorted(top_reason_counts.items(), key=lambda item: (-item[1], item[0]))
            ][:12],
            "enqueuedJobs": enqueued_jobs,
            "deferredDueToCapacity": [
                {
                    "fingerprint": item["target"].get("fingerprint"),
                    "market_country": item["target"].get("market_country"),
                    "currency": item["target"].get("currency"),
                    "band": item["decision"].details.get("owned_priority_band"),
                    "owner_count": item["target"].get("owner_count"),
                    "score": item["decision"].score,
                    "schedulerLane": item.get("demand_row").scheduler_lane
                    if item.get("demand_row") is not None
                    else None,
                }
                for item in leftover[:50]
            ]
            if capacity_gap > 0
            else [],
            "demandAwarePolicy": {
                "highDemandFreshHours": DEFAULT_DEMAND_AWARE_POLICY.hot_verified_ttl_hours,
                "normalFreshHours": DEFAULT_DEMAND_AWARE_POLICY.normal_verified_ttl_hours,
                "highDemandRequests24h": DEFAULT_DEMAND_AWARE_POLICY.high_min_requests_24h,
                "laneShares": {
                    "DEMAND": DEFAULT_DEMAND_AWARE_POLICY.lane_demand_share,
                    "STALE_OWNED": DEFAULT_DEMAND_AWARE_POLICY.lane_stale_share,
                    "COVERAGE": DEFAULT_DEMAND_AWARE_POLICY.lane_coverage_share,
                },
                "auFirst": True,
            },
        }
        if hasattr(self.client, "upsert_pipeline_heartbeat"):
            try:
                self.client.upsert_pipeline_heartbeat(
                    component="owned_daily_scheduler",
                    worker_id=os.getenv("OWNED_DAILY_WORKER_ID", "owned-daily-scheduler"),
                    state="idle" if enqueues_done == 0 else "enqueued",
                    meta={
                        "startedAtUtc": started_at,
                        "finishedAtUtc": report["finishedAtUtc"],
                        "jobsEnqueued": report["summary"]["jobsEnqueued"],
                        "capacityGap": capacity_gap,
                        "fullEnable": self.config.enable_full_daily,
                        "dryRun": self.config.dry_run,
                    },
                )
            except Exception:
                pass
        return report

    def run_and_write_reports(self) -> dict[str, Any]:
        report = self.run_once()
        clean = sanitize_scheduler_report(report)
        write_json(self.config.latest_report_path, clean)
        append_jsonl(self.config.runs_report_path, clean)
        return clean
