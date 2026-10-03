"""Central demand-aware freshness and ranking policy.

All 12h/24h TTL, demand weights, HIGH-class rule, age boost, and lane mix
live here. Scheduler code must read this config rather than scattering literals.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Literal

DemandClass = Literal["HIGH", "MEDIUM", "LOW"]


def _env_int(*names: str, default: int) -> int:
    for name in names:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip():
            return int(str(raw).strip())
    return default


def _env_float(*names: str, default: float) -> float:
    for name in names:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip():
            return float(str(raw).strip())
    return default


@dataclass(frozen=True)
class DemandAwarePolicy:
    """Documented production defaults for CARDSCANR demand-aware owned_daily."""

    hot_verified_ttl_hours: int = 12
    normal_verified_ttl_hours: int = 24
    weight_requests_1h: float = 8.0
    weight_requests_24h: float = 4.0
    weight_requests_7d: float = 1.0
    recency_bonus_1h: float = 4.0
    recency_bonus_6h: float = 1.0
    # HIGH is rolling volume, not a single lookup and not lifetime popularity.
    high_min_requests_24h: int = 3
    medium_min_requests_24h: int = 1
    lane_demand_share: float = 0.50
    lane_stale_share: float = 0.30
    lane_coverage_share: float = 0.20
    age_boost_low_divisor: float = 6.0
    age_boost_low_cap: float = 40.0
    age_boost_medium_divisor: float = 24.0
    age_boost_medium_cap: float = 12.0
    age_boost_high_divisor: float = 48.0
    age_boost_high_cap: float = 4.0
    canary_max_live_jobs: int = 10
    canary_max_search_submission_started: int = 10
    canary_retries: int = 0
    canary_concurrency: int = 1

    @classmethod
    def from_env(cls) -> "DemandAwarePolicy":
        return cls(
            hot_verified_ttl_hours=_env_int("HOT_VERIFIED_TTL_HOURS", default=12),
            normal_verified_ttl_hours=_env_int("NORMAL_VERIFIED_TTL_HOURS", default=24),
            weight_requests_1h=_env_float("DEMAND_WEIGHT_REQUESTS_1H", default=8.0),
            weight_requests_24h=_env_float("DEMAND_WEIGHT_REQUESTS_24H", default=4.0),
            weight_requests_7d=_env_float("DEMAND_WEIGHT_REQUESTS_7D", default=1.0),
            recency_bonus_1h=_env_float("DEMAND_RECENCY_BONUS_1H", default=4.0),
            recency_bonus_6h=_env_float("DEMAND_RECENCY_BONUS_6H", default=1.0),
            high_min_requests_24h=_env_int(
                "HIGH_DEMAND_REQUESTS_24H",
                "DEMAND_HIGH_MIN_REQUESTS_24H",
                default=3,
            ),
            medium_min_requests_24h=_env_int("DEMAND_MEDIUM_MIN_REQUESTS_24H", default=1),
            lane_demand_share=_env_float("DEMAND_LANE_SHARE", default=0.50),
            lane_stale_share=_env_float("DEMAND_STALE_LANE_SHARE", default=0.30),
            lane_coverage_share=_env_float("DEMAND_COVERAGE_LANE_SHARE", default=0.20),
            canary_max_live_jobs=_env_int("DEMAND_AWARE_CANARY_MAX_LIVE_JOBS", default=10),
            canary_max_search_submission_started=_env_int(
                "DEMAND_AWARE_CANARY_MAX_SEARCH_SUBMISSION_STARTED",
                default=10,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def freshness_threshold_hours(
        self,
        demand_class: DemandClass,
        *,
        user_request_recent: bool = False,
    ) -> int:
        """12h only for HIGH rolling demand or an explicit recent user request.

        A single catalogue-wide 12h cycle is avoided: LOW/MEDIUM without a
        recent user request keep the 24h verified-local TTL.
        """
        if demand_class == "HIGH" or user_request_recent:
            return int(self.hot_verified_ttl_hours)
        return int(self.normal_verified_ttl_hours)


DEFAULT_DEMAND_AWARE_POLICY = DemandAwarePolicy()


def demand_score(
    *,
    requests_1h: int,
    requests_24h: int,
    requests_7d: int,
    last_requested_age_hours: float | None,
    policy: DemandAwarePolicy | None = None,
) -> float:
    cfg = policy or DEFAULT_DEMAND_AWARE_POLICY
    score = (
        (requests_1h * cfg.weight_requests_1h)
        + (requests_24h * cfg.weight_requests_24h)
        + (requests_7d * cfg.weight_requests_7d)
    )
    if last_requested_age_hours is not None:
        if last_requested_age_hours <= 1:
            score += cfg.recency_bonus_1h
        elif last_requested_age_hours <= 6:
            score += cfg.recency_bonus_6h
    return round(score, 3)


def classify_demand_class(
    *,
    requests_1h: int,
    requests_24h: int,
    score: float,
    policy: DemandAwarePolicy | None = None,
) -> DemandClass:
    cfg = policy or DEFAULT_DEMAND_AWARE_POLICY
    del requests_1h, score  # ranking-only; HIGH is volume in 24h
    if requests_24h >= cfg.high_min_requests_24h:
        return "HIGH"
    if requests_24h >= cfg.medium_min_requests_24h:
        return "MEDIUM"
    return "LOW"


__all__ = [
    "DEFAULT_DEMAND_AWARE_POLICY",
    "DemandAwarePolicy",
    "DemandClass",
    "classify_demand_class",
    "demand_score",
]
