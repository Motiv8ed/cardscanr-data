#!/usr/bin/env python3
"""Capacity / load report for daily owned-card pricing.

Must be reviewed before OWNED_DAILY_FULL_ENABLE=true.
No PII in output.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import REPORTS_DIR, supabase_secret_key_from_env
from cardscanr_market_engine.queue_capacity import hours_for_keys, projected_cards_per_hour
from cardscanr_market_engine.smoke_utils import write_json
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _median_job_seconds(client: SupabaseMarketEngineClient) -> float:
    env_override = os.getenv("OWNED_DAILY_MEDIAN_JOB_SECONDS", "").strip()
    if env_override:
        return float(env_override)
    try:
        rows = client._table_get(  # noqa: SLF001 - ops tooling
            "market_price_refresh_jobs",
            params={
                "select": "started_at,completed_at,status",
                "status": "eq.completed",
                "order": "completed_at.desc",
                "limit": "80",
            },
        )
    except Exception:
        return 90.0
    durations: list[float] = []
    for row in rows:
        try:
            start = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(row["completed_at"]).replace("Z", "+00:00"))
            seconds = (end - start).total_seconds()
            if 5 <= seconds <= 1800:
                durations.append(seconds)
        except Exception:
            continue
    if not durations:
        return 90.0
    return float(statistics.median(durations))


def build_report(
    *,
    client: SupabaseMarketEngineClient,
    worker_concurrency: int,
    safe_hours_per_day: float,
) -> dict:
    health = client.owned_price_health_report()
    targets_payload = client.list_owned_market_pricing_targets(include_zero_owners=False)
    targets = list(targets_payload.get("targets") or [])
    due = [t for t in targets if t.get("due_for_owned_daily")]
    by_market: dict[str, int] = {}
    home_searches = 0
    fallback_expected = 0
    for t in targets:
        m = f"{t.get('market_country')}|{t.get('currency')}"
        by_market[m] = by_market.get(m, 0) + 1
    for t in due:
        home_searches += 1
        # Conservative: GB/CA/thin markets often need international fallback.
        market = str(t.get("market_country") or "").lower()
        if market in {"gb", "ca"} or t.get("price_state") in {"unpriced", "stale", "stale_gt_24h"}:
            # Count potential fallback attempt separately from home search.
            if market != "au":
                fallback_expected += 1

    median_seconds = _median_job_seconds(client)
    cph = projected_cards_per_hour(median_job_seconds=median_seconds, workers=worker_concurrency)
    max_safe_daily = int(cph * safe_hours_per_day) if cph else 0
    due_count = len(due)
    runtime_hours = hours_for_keys(due_count, cph)
    gap = max(0, due_count - max_safe_daily)
    can_cover = gap == 0 and due_count > 0
    if due_count == 0:
        can_cover = True

    # Capacity utilisation vs maximum safe daily throughput (unique printing×market).
    required_max = int(health.get("marketSpecificKeys") or len(targets) or 0)
    utilisation = (float(required_max) / float(max_safe_daily)) if max_safe_daily > 0 else None
    if utilisation is None:
        capacity_status = "UNKNOWN"
    elif utilisation < 0.70:
        capacity_status = "HEALTHY"
    elif utilisation < 0.90:
        capacity_status = "WATCH"
    elif utilisation <= 1.0:
        capacity_status = "CAPACITY_TIGHT"
    else:
        capacity_status = "CAPACITY_GAP"

    return {
        "capturedAtUtc": utc_iso(),
        "usersWithOwnedCards": health.get("usersWithOwnedCards"),
        "uniqueOwnedPrintings": health.get("uniqueOwnedPrintings"),
        "totalCopies": health.get("totalCopies"),
        "uniquePrintingMarketContexts": health.get("marketSpecificKeys"),
        "freshLt24h": health.get("freshLt24h"),
        "stale": health.get("stale"),
        "neverPriced": health.get("unpriced"),
        "dueToday": health.get("dueToday"),
        "byPriority": health.get("byPriority"),
        "byMarket": health.get("byMarket"),
        "expectedHomeMarketSearches": home_searches,
        "expectedFallbackSearches": fallback_expected,
        "averageJobDurationSeconds": round(median_seconds, 2),
        "workerConcurrency": worker_concurrency,
        "safeHoursPerDay": safe_hours_per_day,
        "projectedCardsPerHour": round(cph, 2),
        "maximumSafeDailyThroughput": max_safe_daily,
        "expectedTotalRuntimeHours": None if runtime_hours is None else round(runtime_hours, 2),
        "capacityGap": gap,
        "capacityUtilisation": None if utilisation is None else round(utilisation, 4),
        "capacityStatus": capacity_status,
        "capacityThresholds": {
            "HEALTHY": "<0.70",
            "WATCH": "0.70–0.90",
            "CAPACITY_TIGHT": "0.90–1.00",
            "CAPACITY_GAP": ">1.00",
            "basis": "unique_printing_market_contexts / maximum_safe_daily_throughput",
        },
        "canRealisticallyRefreshEveryOwnedKeyDaily": can_cover and capacity_status != "CAPACITY_GAP",
        "recommendation": (
            "ENABLE_FULL_DAILY"
            if can_cover and capacity_status in {"HEALTHY", "WATCH", "CAPACITY_TIGHT"}
            else "KEEP_PILOT_AND_FAIR_ROLLING"
        ),
        "notes": [
            "Dedup unit is printing × home market; quantity does not create extra jobs.",
            "Catalogue-only cards are excluded.",
            "No user PII included.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Owned daily pricing capacity report")
    parser.add_argument("--workers", type=int, default=int(os.getenv("OWNED_DAILY_WORKER_CONCURRENCY", "1")))
    parser.add_argument("--safe-hours", type=float, default=float(os.getenv("OWNED_DAILY_SAFE_HOURS", "16")))
    parser.add_argument(
        "--out",
        type=Path,
        default=REPORTS_DIR / "owned_price_capacity_latest.json",
    )
    args = parser.parse_args()

    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = supabase_secret_key_from_env()
    if not url or not key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SECRET_KEY required")
    client = SupabaseMarketEngineClient(supabase_url=url, service_role_key=key)
    report = build_report(
        client=client,
        worker_concurrency=max(1, args.workers),
        safe_hours_per_day=max(1.0, float(args.safe_hours)),
    )
    write_json(args.out, report)
    print(json.dumps(report, indent=2))
    print(f"\nWrote {args.out}")
    print(f"VERDICT_CAPACITY={report['recommendation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
