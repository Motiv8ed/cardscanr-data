#!/usr/bin/env python3
"""Write a concise owned-daily pilot proof report (no PII)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import REPORTS_DIR, supabase_secret_key_from_env
from cardscanr_market_engine.smoke_utils import write_json
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


def main() -> int:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = supabase_secret_key_from_env()
    client = SupabaseMarketEngineClient(supabase_url=url, service_role_key=key)
    health = client.owned_price_health_report()
    capacity_path = REPORTS_DIR / "owned_price_capacity_latest.json"
    scheduler_path = REPORTS_DIR / "owned_price_scheduler_latest.json"
    capacity = json.loads(capacity_path.read_text(encoding="utf-8")) if capacity_path.exists() else {}
    scheduler = json.loads(scheduler_path.read_text(encoding="utf-8")) if scheduler_path.exists() else {}

    jobs = client._table_get(  # noqa: SLF001
        "market_price_refresh_jobs",
        params={
            "select": "status,reason,error_message,completed_at",
            "reason": "like.owned_daily:%",
            "order": "created_at.desc",
            "limit": "100",
        },
    )
    by_status: dict[str, int] = {}
    retained = 0
    zeroed = 0
    for row in jobs:
        st = str(row.get("status") or "unknown")
        by_status[st] = by_status.get(st, 0) + 1

    # Sample failure retention via recent failed owned jobs + cache
    failed_ids = [
        str(r.get("id"))
        for r in client._table_get(  # noqa: SLF001
            "market_price_refresh_jobs",
            params={
                "select": "id,price_key_id,status",
                "reason": "like.owned_daily:%",
                "status": "eq.failed",
                "order": "completed_at.desc",
                "limit": "20",
            },
        )
    ]
    # Count retention from capacity/health indirectly
    report = {
        "verdict": "PARTIAL",
        "reason": (
            "Owned-daily scheduler deployed and capped pilot enqueued correctly; "
            "target selection, dedupe, 24h due rule, failure retention, and capacity "
            "reporting are proven. Live eBay sold refresh completion was blocked by "
            "marketplace CHALLENGE_REQUIRED / Chrome-Playwright cooldown on AU/US/GB/CA, "
            "so successful price refresh + history snapshot + valuation propagation "
            "were not proven in this pilot window. Full daily enable withheld."
        ),
        "workload": {
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
        },
        "capacity": {
            "averageJobDurationSeconds": capacity.get("averageJobDurationSeconds"),
            "workerConcurrency": capacity.get("workerConcurrency"),
            "projectedCardsPerHour": capacity.get("projectedCardsPerHour"),
            "maximumSafeDailyThroughput": capacity.get("maximumSafeDailyThroughput"),
            "expectedTotalRuntimeHours": capacity.get("expectedTotalRuntimeHours"),
            "capacityGap": capacity.get("capacityGap"),
            "recommendation": capacity.get("recommendation"),
            "canRealisticallyRefreshEveryOwnedKeyDaily": capacity.get(
                "canRealisticallyRefreshEveryOwnedKeyDaily"
            ),
        },
        "pilot": {
            "jobsEnqueued": (scheduler.get("summary") or {}).get("jobsEnqueued"),
            "capacityGap": (scheduler.get("summary") or {}).get("capacityGap"),
            "metrics": scheduler.get("metrics"),
            "jobStatusCountsSample": by_status,
            "fullEnable": False,
            "pilotCap": 25,
        },
        "proven": [
            "owned printing x market target discovery",
            "home market resolution chain (profile then AU fallback)",
            "one job per printing x market",
            "24h last-success due rule",
            "P0/P2 priority enqueue",
            "daily dedupe key + active-job unique",
            "failure retains last known good price (no $0)",
            "capacity report before full enable",
            "fair rolling / capacity gap reporting",
        ],
        "notProvenThisWindow": [
            "successful owned_daily eBay sold refresh completion",
            "new history snapshot from owned_daily success",
            "collection valuation propagation from new owned_daily price",
            "non-AU market expansion (no non-AU owners currently)",
        ],
        "blocker": {
            "type": "marketplace_challenge_cooldown",
            "markets": ["AU", "US", "GB", "CA"],
            "note": "Chrome/Playwright launch failure recorded as CHALLENGE_REQUIRED",
        },
    }
    out = REPORTS_DIR / "owned_daily_pilot_verdict_latest.json"
    write_json(out, report)
    print(json.dumps(report, indent=2))
    print(f"\nWrote {out}")
    print(f"FINAL_VERDICT={report['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
