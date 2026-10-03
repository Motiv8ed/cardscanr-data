"""Demand-aware, market-specific owned_daily scheduling.

Freshness is per market-price key (home market). Demand raises PRIORITY and
may shorten the verified-local freshness window (12h high-demand vs 24h
normal). Demand never executes a still-fresh verified-local card.

Existing CardScanR demand signals (audit; no new product telemetry required)
------------------------------------------------------------------------
Primary rolling signal (already persisted):
  ``market_price_refresh_jobs.requested_at`` + ``reason`` for *user-origin*
  reasons (``user_refresh``, scanner/search/view/lookup aliases).
  Scheduler-owned reasons (``owned_daily:*``) are excluded so the engine
  cannot feed its own enqueue loop.

Not used as the main priority signal:
  lifetime ``owner_count`` / catalogue popularity.

Market isolation: events and freshness bind to ``price_key_id`` (and
fingerprint+market). AU verified age cannot mark US/GB/CA fresh or due.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Literal, Mapping, Sequence

from .demand_aware_policy import (
    DEFAULT_DEMAND_AWARE_POLICY,
    DemandAwarePolicy,
    DemandClass,
    classify_demand_class,
    demand_score,
)
from .owned_daily_source_policy import classify_owned_daily_band
from .owned_verified_local_execution import evaluate_owned_verified_local_execution
from .scheduler import _parse_utc, utc_iso

SchedulerLane = Literal["DEMAND", "STALE_OWNED", "COVERAGE", "FRESH_SKIP", "NOT_ELIGIBLE"]

HIGH_DEMAND_FRESH_HOURS = DEFAULT_DEMAND_AWARE_POLICY.hot_verified_ttl_hours
NORMAL_DEMAND_FRESH_HOURS = DEFAULT_DEMAND_AWARE_POLICY.normal_verified_ttl_hours

LANE_DEMAND_SHARE = DEFAULT_DEMAND_AWARE_POLICY.lane_demand_share
LANE_STALE_SHARE = DEFAULT_DEMAND_AWARE_POLICY.lane_stale_share
LANE_COVERAGE_SHARE = DEFAULT_DEMAND_AWARE_POLICY.lane_coverage_share

EXPLICIT_REQUEST_REASONS = frozenset(
    {
        "user_refresh",
        "user_request",
        "manual_refresh",
        "app_price_lookup",
        "price_lookup",
    }
)

# User / client request origins. owned_daily and worker probes are excluded.
USER_DEMAND_REASONS = frozenset(
    {
        "user_refresh",
        "user_request",
        "user_search",
        "search",
        "catalogue_search",
        "card_view",
        "collection_view",
        "scanner",
        "scanner_ocr",
        "scan",
        "app_price_lookup",
        "price_lookup",
        "manual_refresh",
        "website_search",
        "website_view",
    }
)

ENGINE_REASON_PREFIXES = ("owned_daily:", "live_ebay", "reliability", "probe", "canary")

MARKET_PRIORITY = {"AU": 0, "US": 1, "GB": 2, "CA": 3}


@dataclass(frozen=True)
class DemandEvent:
    requested_at: datetime
    price_key_id: str = ""
    fingerprint: str = ""
    market: str = ""
    reason: str = ""
    requester_key: str = ""


@dataclass(frozen=True)
class DemandWindows:
    requests_1h: int
    requests_24h: int
    requests_7d: int
    last_requested_at: datetime | None
    demand_score: float
    demand_class: DemandClass
    unique_requesters_24h: int = 0
    user_request_recent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests1h": self.requests_1h,
            "requests24h": self.requests_24h,
            "requests7d": self.requests_7d,
            "lastRequestedAt": utc_iso(self.last_requested_at) if self.last_requested_at else None,
            "demandScore": self.demand_score,
            "demandClass": self.demand_class,
            "uniqueRequesters24h": self.unique_requesters_24h,
            "userRequestRecent": self.user_request_recent,
        }


EMPTY_DEMAND = DemandWindows(
    requests_1h=0,
    requests_24h=0,
    requests_7d=0,
    last_requested_at=None,
    demand_score=0.0,
    demand_class="LOW",
    unique_requesters_24h=0,
    user_request_recent=False,
)


def is_user_demand_reason(reason: str | None) -> bool:
    text = str(reason or "").strip().lower()
    if not text:
        return False
    if any(text.startswith(p) for p in ENGINE_REASON_PREFIXES):
        return False
    if text in USER_DEMAND_REASONS:
        return True
    # Allow ``scanner:foo`` / ``user_refresh:card_view`` style suffixes.
    head = text.split(":", 1)[0].strip()
    return head in USER_DEMAND_REASONS


def _event_identity_keys(event: DemandEvent) -> list[str]:
    keys: list[str] = []
    kid = str(event.price_key_id or "").strip()
    if kid:
        keys.append(f"id:{kid}")
    fp = str(event.fingerprint or "").strip().lower()
    mkt = str(event.market or "").strip().upper()
    if fp and mkt:
        keys.append(f"fp:{fp}|{mkt}")
    elif fp:
        keys.append(f"fp:{fp}")
    return keys


def target_identity_keys(target: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    kid = str(
        target.get("market_price_key_id") or target.get("price_key_id") or ""
    ).strip()
    if kid:
        keys.append(f"id:{kid}")
    fp = str(target.get("fingerprint") or "").strip().lower()
    mkt = str(target.get("market_country") or target.get("market") or "").strip().upper()
    if fp and mkt:
        keys.append(f"fp:{fp}|{mkt}")
    elif fp:
        keys.append(f"fp:{fp}")
    return keys


def demand_score_from_counts(
    *,
    requests_1h: int,
    requests_24h: int,
    requests_7d: int,
    last_requested_at: datetime | None,
    now: datetime,
    policy: DemandAwarePolicy | None = None,
) -> float:
    """Rolling score. Lifetime counts are not an input."""
    age_h = None
    if last_requested_at is not None:
        age_h = max(0.0, (now - last_requested_at).total_seconds() / 3600.0)
    return demand_score(
        requests_1h=requests_1h,
        requests_24h=requests_24h,
        requests_7d=requests_7d,
        last_requested_age_hours=age_h,
        policy=policy,
    )


def build_demand_windows(
    events: Sequence[DemandEvent],
    *,
    now: datetime,
    identity_keys: Sequence[str],
) -> DemandWindows:
    keyset = set(identity_keys)
    if not keyset:
        return EMPTY_DEMAND
    t1 = now - timedelta(hours=1)
    t24 = now - timedelta(hours=24)
    t7 = now - timedelta(days=7)
    n1 = n24 = n7 = 0
    last: datetime | None = None
    unique_24h: set[str] = set()
    user_request_recent = False
    policy = DEFAULT_DEMAND_AWARE_POLICY
    for ev in events:
        if not is_user_demand_reason(ev.reason):
            continue
        if not keyset.intersection(_event_identity_keys(ev)):
            continue
        ts = ev.requested_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=now.tzinfo)
        if ts < t7:
            continue
        n7 += 1
        if ts >= t24:
            n24 += 1
            rk = str(ev.requester_key or "").strip()
            if rk:
                unique_24h.add(rk)
            head = str(ev.reason or "").split(":", 1)[0].strip().lower()
            if head in EXPLICIT_REQUEST_REASONS:
                user_request_recent = True
        if ts >= t1:
            n1 += 1
        if last is None or ts > last:
            last = ts
    score = demand_score_from_counts(
        requests_1h=n1,
        requests_24h=n24,
        requests_7d=n7,
        last_requested_at=last,
        now=now,
        policy=policy,
    )
    return DemandWindows(
        requests_1h=n1,
        requests_24h=n24,
        requests_7d=n7,
        last_requested_at=last,
        demand_score=score,
        demand_class=classify_demand_class(
            requests_1h=n1, requests_24h=n24, score=score, policy=policy
        ),
        unique_requesters_24h=len(unique_24h),
        user_request_recent=user_request_recent,
    )


class DemandIndex:
    """Pre-bucketed events for O(1) target lookup."""

    def __init__(self, events: Sequence[DemandEvent] | None = None) -> None:
        self.events: list[DemandEvent] = list(events or [])
        self._by_key: dict[str, list[DemandEvent]] = {}
        for ev in self.events:
            if not is_user_demand_reason(ev.reason):
                continue
            for k in _event_identity_keys(ev):
                self._by_key.setdefault(k, []).append(ev)

    def windows_for(self, target: Mapping[str, Any], *, now: datetime) -> DemandWindows:
        seen: set[int] = set()
        matched: list[DemandEvent] = []
        for k in target_identity_keys(target):
            for ev in self._by_key.get(k, []):
                i = id(ev)
                if i in seen:
                    continue
                seen.add(i)
                matched.append(ev)
        if not matched:
            return EMPTY_DEMAND
        return build_demand_windows(matched, now=now, identity_keys=target_identity_keys(target))


def freshness_threshold_hours(
    demand_class: DemandClass,
    *,
    user_request_recent: bool = False,
    policy: DemandAwarePolicy | None = None,
) -> int:
    cfg = policy or DEFAULT_DEMAND_AWARE_POLICY
    return cfg.freshness_threshold_hours(
        demand_class, user_request_recent=user_request_recent
    )


def age_boost_hours(
    verified_age_hours: float | None,
    *,
    demand_class: DemandClass,
    due: bool,
    policy: DemandAwarePolicy | None = None,
) -> float:
    """Low-demand stale/coverage cards gain age so they cannot starve forever."""
    if not due:
        return 0.0
    cfg = policy or DEFAULT_DEMAND_AWARE_POLICY
    age = float(verified_age_hours or 0.0)
    if demand_class == "LOW":
        return round(min(cfg.age_boost_low_cap, age / cfg.age_boost_low_divisor), 3)
    if demand_class == "MEDIUM":
        return round(min(cfg.age_boost_medium_cap, age / cfg.age_boost_medium_divisor), 3)
    return round(min(cfg.age_boost_high_cap, age / cfg.age_boost_high_divisor), 3)


def assign_scheduler_lane(
    *,
    due: bool,
    band: str,
    demand_class: DemandClass,
    source_class: str,
    verified_local: bool,
) -> SchedulerLane:
    if band in {"FRESH_SKIP"} or not due:
        return "FRESH_SKIP" if band == "FRESH_SKIP" else "NOT_ELIGIBLE"
    if demand_class in {"HIGH", "MEDIUM"}:
        return "DEMAND"
    if source_class in {"none", "reference_only", "structured_fallback"} or band in {
        "P0_NEVER_PRICED",
        "P0_NEEDS_VERIFIED_LOCAL",
    }:
        return "COVERAGE"
    if verified_local and band in {"P1_STALE_GT_24H", "P1_STALE_VERIFIED", "P2_FAILED_RETRY", "P3_APPROACHING_DUE"}:
        return "STALE_OWNED"
    return "COVERAGE"


def reason_code_for(
    *,
    due: bool,
    band: str,
    demand_class: DemandClass,
    source_class: str,
    threshold: int,
    active_duplicate: bool,
) -> str:
    if active_duplicate:
        return "DEDUPED_ACTIVE_CANONICAL_JOB"
    if source_class in {"reference_only", "structured_fallback"}:
        return "DUE_REFERENCE_ONLY_NEEDS_VERIFIED_LOCAL"
    if source_class == "none" or band == "P0_NEVER_PRICED":
        return "DUE_NEVER_PRICED"
    if not due or band == "FRESH_SKIP":
        return f"FRESH_SKIP_{demand_class}_LT_{threshold}H"
    if demand_class == "HIGH":
        return f"DUE_HIGH_DEMAND_STALE_GE_{threshold}H"
    if band == "P2_FAILED_RETRY":
        return "DUE_FAILED_RETRY"
    if demand_class == "LOW":
        return f"DUE_LOW_DEMAND_STALE_GE_{threshold}H"
    return f"DUE_MEDIUM_DEMAND_STALE_GE_{threshold}H"


@dataclass(frozen=True)
class ExplainableSchedulerRow:
    price_key_id: str
    fingerprint: str
    market: str
    currency: str
    source_class: str
    verified_local: bool
    verified_age_hours: float | None
    demand_class: DemandClass
    demand_score: float
    requests_1h: int
    requests_24h: int
    requests_7d: int
    last_requested_at: str | None
    freshness_threshold_hours: int
    due: bool
    scheduler_lane: SchedulerLane
    priority_band: str
    age_boost: float
    final_priority: float
    reason_code: str
    scheduler_due: bool
    would_hit_ebay: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "priceKeyId": self.price_key_id,
            "fingerprint": self.fingerprint,
            "market": self.market,
            "currency": self.currency,
            "sourceClass": self.source_class,
            "verifiedLocal": self.verified_local,
            "verifiedAgeHours": self.verified_age_hours,
            "demandClass": self.demand_class,
            "demandScore": self.demand_score,
            "requests1h": self.requests_1h,
            "requests24h": self.requests_24h,
            "requests7d": self.requests_7d,
            "lastRequestedAt": self.last_requested_at,
            "uniqueRequesters24h": self.details.get("uniqueRequesters24h")
            if isinstance(self.details, dict)
            else None,
            "userRequestRecent": bool((self.details or {}).get("userRequestRecent")),
            "freshnessThresholdHours": self.freshness_threshold_hours,
            "due": self.due,
            "schedulerLane": self.scheduler_lane,
            "priorityBand": self.priority_band,
            "ageBoost": self.age_boost,
            "finalPriority": self.final_priority,
            "reasonCode": self.reason_code,
        }


def evaluate_demand_aware_target(
    target: Mapping[str, Any],
    *,
    now: datetime,
    demand: DemandWindows | None = None,
    demand_index: DemandIndex | None = None,
    active_job: Mapping[str, Any] | None = None,
    owner_count: int | None = None,
) -> ExplainableSchedulerRow:
    windows = demand if demand is not None else (
        demand_index.windows_for(target, now=now) if demand_index is not None else EMPTY_DEMAND
    )
    threshold = freshness_threshold_hours(
        windows.demand_class,
        user_request_recent=bool(windows.user_request_recent),
    )
    band, due, reason_suffix, view, extras = classify_owned_daily_band(
        dict(target),
        now=now,
        success_fresh_hours=threshold,
    )
    exec_dec = evaluate_owned_verified_local_execution(
        dict(target),
        now=now,
        success_fresh_hours=threshold,
    )
    age = view.scheduler_age_hours
    if view.has_verified_local_price:
        verified_age = age
    else:
        verified_age = None

    due = bool(exec_dec.scheduler_due and exec_dec.should_execute)
    if exec_dec.would_skip_fresh:
        due = False
        band = "FRESH_SKIP"
    if (
        view.has_verified_local_price
        and verified_age is not None
        and verified_age < float(threshold)
        and band not in {"P2_FAILED_RETRY", "P0_NEVER_PRICED", "P0_NEEDS_VERIFIED_LOCAL"}
        and str(target.get("refresh_status") or "").strip().lower() != "failed"
    ):
        due = False
        band = "FRESH_SKIP"

    owners = owner_count
    if owners is None:
        try:
            owners = int(target.get("owner_count") or 0)
        except (TypeError, ValueError):
            owners = 0

    dup = bool(active_job)
    if dup:
        due_for_ebay = False
    else:
        due_for_ebay = due

    lane = assign_scheduler_lane(
        due=due_for_ebay,
        band=band,
        demand_class=windows.demand_class,
        source_class=str(view.source_class),
        verified_local=bool(view.has_verified_local_price),
    )
    boost = age_boost_hours(verified_age if verified_age is not None else age, demand_class=windows.demand_class, due=due_for_ebay)
    # Higher finalPriority = more urgent. Demand score never overrides FRESH_SKIP.
    base = 0.0
    if due_for_ebay:
        if lane == "DEMAND":
            base = 8_000.0 + windows.demand_score
        elif str(view.source_class) in {"none", "reference_only", "structured_fallback"}:
            base = 6_500.0 + min(float(age or 0.0), 5_000.0)
        else:
            base = 4_000.0
        if str(target.get("market_country") or target.get("market") or "").upper() == "AU":
            base += 50.0
        base += boost * 10.0
        base += min(float(owners or 0) * 2.0, 40.0)
    reason = reason_code_for(
        due=due_for_ebay,
        band=band,
        demand_class=windows.demand_class,
        source_class=str(view.source_class),
        threshold=threshold,
        active_duplicate=dup,
    )
    kid = str(target.get("market_price_key_id") or target.get("price_key_id") or "").strip()
    fp = str(target.get("fingerprint") or "")
    market = str(target.get("market_country") or target.get("market") or "").strip().upper()
    currency = str(target.get("currency") or "").strip().upper()
    return ExplainableSchedulerRow(
        price_key_id=kid,
        fingerprint=fp,
        market=market,
        currency=currency,
        source_class=str(view.source_class),
        verified_local=bool(view.has_verified_local_price),
        verified_age_hours=None if verified_age is None else round(float(verified_age), 4),
        demand_class=windows.demand_class,
        demand_score=windows.demand_score,
        requests_1h=windows.requests_1h,
        requests_24h=windows.requests_24h,
        requests_7d=windows.requests_7d,
        last_requested_at=utc_iso(windows.last_requested_at) if windows.last_requested_at else None,
        freshness_threshold_hours=threshold,
        due=due_for_ebay,
        scheduler_lane=lane,
        priority_band=band,
        age_boost=boost,
        final_priority=round(base, 3),
        reason_code=reason,
        scheduler_due=due_for_ebay,
        would_hit_ebay=due_for_ebay,
        details={
            **extras,
            "bandReason": reason_suffix,
            "executionReason": exec_dec.reason_code,
            "ownerCount": owners,
            "activeJobId": (active_job or {}).get("id") if isinstance(active_job, Mapping) else None,
            "uniqueRequesters24h": windows.unique_requesters_24h,
            "userRequestRecent": windows.user_request_recent,
        },
    )


def select_fair_lane_mix(
    rows: Sequence[ExplainableSchedulerRow],
    *,
    budget: int,
    demand_share: float = LANE_DEMAND_SHARE,
    stale_share: float = LANE_STALE_SHARE,
    coverage_share: float = LANE_COVERAGE_SHARE,
) -> list[ExplainableSchedulerRow]:
    """Pick due work only. Never execute FRESH_SKIP to fill a lane."""
    if budget <= 0:
        return []
    due_rows = [r for r in rows if r.due and r.would_hit_ebay and r.scheduler_lane != "FRESH_SKIP"]
    by_lane: dict[str, list[ExplainableSchedulerRow]] = {
        "DEMAND": [],
        "STALE_OWNED": [],
        "COVERAGE": [],
    }
    for r in due_rows:
        lane = r.scheduler_lane if r.scheduler_lane in by_lane else "COVERAGE"
        by_lane[lane].append(r)

    def _sort_lane(items: list[ExplainableSchedulerRow]) -> list[ExplainableSchedulerRow]:
        return sorted(
            items,
            key=lambda r: (
                MARKET_PRIORITY.get(r.market, 9),
                -r.final_priority,
                r.fingerprint,
            ),
        )

    queues = {k: _sort_lane(v) for k, v in by_lane.items()}
    quotas = {
        "DEMAND": max(0, int(budget * demand_share + 0.999999)),
        "STALE_OWNED": max(0, int(budget * stale_share + 0.000001)),
        "COVERAGE": max(0, int(budget * coverage_share + 0.000001)),
    }
    # Normalize quota sum to budget.
    while sum(quotas.values()) > budget:
        for lane in ("COVERAGE", "STALE_OWNED", "DEMAND"):
            if quotas[lane] > 0 and sum(quotas.values()) > budget:
                quotas[lane] -= 1
    while sum(quotas.values()) < budget:
        quotas["DEMAND"] += 1

    selected: list[ExplainableSchedulerRow] = []
    seen: set[str] = set()

    def _take(lane: str, n: int) -> None:
        q = queues[lane]
        while n > 0 and q:
            row = q.pop(0)
            ident = row.price_key_id or f"{row.fingerprint}|{row.market}"
            if ident in seen:
                continue
            seen.add(ident)
            selected.append(row)
            n -= 1

    _take("DEMAND", quotas["DEMAND"])
    _take("STALE_OWNED", quotas["STALE_OWNED"])
    _take("COVERAGE", quotas["COVERAGE"])

    leftover = budget - len(selected)
    if leftover > 0:
        leftover_order = (
            queues["DEMAND"] + queues["STALE_OWNED"] + queues["COVERAGE"]
        )
        for row in leftover_order:
            if len(selected) >= budget:
                break
            ident = row.price_key_id or f"{row.fingerprint}|{row.market}"
            if ident in seen:
                continue
            seen.add(ident)
            selected.append(row)
    return selected[:budget]


def events_from_job_rows(rows: Iterable[Mapping[str, Any]]) -> list[DemandEvent]:
    out: list[DemandEvent] = []
    for row in rows:
        ts = _parse_utc(row.get("requested_at") or row.get("requestedAt"))
        if ts is None:
            continue
        reason = str(row.get("reason") or "")
        if not is_user_demand_reason(reason):
            continue
        out.append(
            DemandEvent(
                requested_at=ts,
                price_key_id=str(row.get("price_key_id") or row.get("priceKeyId") or ""),
                fingerprint=str(row.get("fingerprint") or ""),
                market=str(row.get("market_country") or row.get("market") or "").upper(),
                reason=reason,
                requester_key=str(row.get("requested_by_user_id") or "")[:64],
            )
        )
    return out


__all__ = [
    "DemandEvent",
    "DemandIndex",
    "DemandWindows",
    "EMPTY_DEMAND",
    "ExplainableSchedulerRow",
    "HIGH_DEMAND_FRESH_HOURS",
    "NORMAL_DEMAND_FRESH_HOURS",
    "USER_DEMAND_REASONS",
    "assign_scheduler_lane",
    "build_demand_windows",
    "evaluate_demand_aware_target",
    "events_from_job_rows",
    "freshness_threshold_hours",
    "is_user_demand_reason",
    "select_fair_lane_mix",
]
