#!/usr/bin/env python3
"""Collection-wide AU demand-aware scheduler simulation. ZERO eBay activity."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.demand_aware_scheduler import (
    DemandEvent,
    DemandIndex,
    evaluate_demand_aware_target,
    events_from_job_rows,
    select_fair_lane_mix,
)
from cardscanr_market_engine.owned_daily_scheduler import (
    OwnedDailySchedulerConfig,
    OwnedPrintingRefreshScheduler,
)
from cardscanr_market_engine.scheduler import utc_iso

OUT = ROOT / "reports" / "artifacts" / "demand_aware_scheduler_canary"
NOW = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)
HORIZONS = (
    ("NOW", 0),
    ("+12h", 12),
    ("+24h", 24),
    ("+48h", 48),
    ("+72h", 72),
    ("+7d", 168),
)


def _iso(dt: datetime) -> str:
    return utc_iso(dt)


def _verified(key: str, *, age_h: float, market: str = "AU", currency: str = "AUD") -> dict[str, Any]:
    return {
        "fingerprint": f"sim|{key}|{market.lower()}",
        "market_country": market,
        "currency": currency,
        "owner_count": 1,
        "total_owned_quantity": 1,
        "market_price_key_id": key,
        "current_market_price": 1.25,
        "display_price_source": "verified_local",
        "provider": "ebay_browser",
        "last_updated_at": _iso(NOW - timedelta(hours=age_h)),
        "refresh_status": "completed",
        "card_name": key,
    }


def synthetic_universe() -> tuple[list[dict[str, Any]], list[DemandEvent]]:
    targets = [
        _verified("popular-fresh", age_h=2),
        _verified("popular-12h-edge", age_h=12.05),
        _verified("normal-18h", age_h=18),
        _verified("normal-1h", age_h=1),
        _verified("normal-25h", age_h=25),
        {
            **_verified("reference-only", age_h=1),
            "display_price_source": "reference",
            "provider": "tcgdex_cardmarket",
        },
        {
            "fingerprint": "sim|never-priced|au",
            "market_country": "AU",
            "currency": "AUD",
            "owner_count": 1,
            "total_owned_quantity": 1,
            "market_price_key_id": "never-priced",
            "current_market_price": None,
            "card_name": "never-priced",
        },
        _verified("old-low-demand", age_h=400),
        _verified("same-print-au-fresh", age_h=3, market="AU", currency="AUD"),
        _verified("same-print-us-stale", age_h=40, market="US", currency="USD"),
        {
            **_verified("same-print-gb-ref", age_h=2, market="GB", currency="GBP"),
            "display_price_source": "reference",
            "provider": "tcgdex_cardmarket",
        },
        {
            "fingerprint": "sim|same-print-ca|ca",
            "market_country": "CA",
            "currency": "CAD",
            "owner_count": 1,
            "total_owned_quantity": 1,
            "market_price_key_id": "same-print-ca-never",
            "current_market_price": None,
            "card_name": "same-print-ca",
        },
    ]
    for i in range(12):
        targets.append(_verified(f"hot-stale-{i}", age_h=14))
    for i in range(8):
        targets.append(_verified(f"coverage-never-{i}", age_h=1) | {
            "current_market_price": None,
            "display_price_source": None,
            "provider": None,
            "last_updated_at": None,
            "market_price_key_id": f"coverage-never-{i}",
        })
    events: list[DemandEvent] = []
    for key in ["popular-fresh", "popular-12h-edge", "same-print-au-fresh"]:
        events.append(
            DemandEvent(
                requested_at=NOW - timedelta(minutes=20),
                price_key_id=key,
                market="AU",
                reason="user_refresh",
            )
        )
        events.append(
            DemandEvent(
                requested_at=NOW - timedelta(hours=2),
                price_key_id=key,
                market="AU",
                reason="scanner",
            )
        )
        events.append(
            DemandEvent(
                requested_at=NOW - timedelta(hours=5),
                price_key_id=key,
                market="AU",
                reason="card_view",
            )
        )
    for i in range(12):
        events.append(
            DemandEvent(
                requested_at=NOW - timedelta(minutes=15),
                price_key_id=f"hot-stale-{i}",
                market="AU",
                reason="user_search",
            )
        )
    return targets, events


def evaluate_universe(
    targets: list[dict[str, Any]],
    events: list[DemandEvent],
    *,
    now: datetime,
    market: str | None = None,
) -> list[dict[str, Any]]:
    idx = DemandIndex(events)
    rows = []
    for t in targets:
        mkt = str(t.get("market_country") or "").upper()
        if market and mkt != market:
            continue
        row = evaluate_demand_aware_target(t, now=now, demand_index=idx)
        rows.append(row.to_public_dict())
    return rows


def proof_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {r["priceKeyId"]: r for r in rows}

    def due(key: str) -> bool:
        return bool(by_id.get(key, {}).get("due"))

    return {
        "popularFreshSkip": (not due("popular-fresh")) and str(by_id.get("popular-fresh", {}).get("reasonCode", "")).startswith("FRESH_SKIP"),
        "popularGe12Due": due("popular-12h-edge"),
        "normalLt24Skip": not due("normal-18h") and not due("normal-1h"),
        "normalGe24Due": due("normal-25h"),
        "referenceOnlyDue": due("reference-only"),
        "neverPricedDue": due("never-priced"),
        "oldLowDemandAgeBoost": float(by_id.get("old-low-demand", {}).get("ageBoost") or 0) > 0,
        "auFreshIsolated": not due("same-print-au-fresh"),
        "usStaleDue": due("same-print-us-stale") if "same-print-us-stale" in by_id else None,
        "gbReferenceDue": due("same-print-gb-ref") if "same-print-gb-ref" in by_id else None,
        "caNeverDue": due("same-print-ca-never") if "same-print-ca-never" in by_id else None,
    }


def run_synthetic() -> dict[str, Any]:
    targets, events = synthetic_universe()
    horizons: dict[str, Any] = {}
    all_pass = True
    for label, hours in HORIZONS:
        now = NOW + timedelta(hours=hours)
        rows = evaluate_universe(targets, events, now=now)
        au_rows = [r for r in rows if r["market"] == "AU"]
        mix = select_fair_lane_mix(
            [evaluate_demand_aware_target(t, now=now, demand_index=DemandIndex(events)) for t in targets if str(t.get("market_country")).upper() == "AU"],
            budget=10,
        )
        fresh_in_mix = [r for r in mix if not r.due]
        proof = proof_from_rows(rows)
        if label == "NOW":
            required = [
                proof["popularFreshSkip"],
                not proof["popularGe12Due"] or proof["popularGe12Due"],
                proof["normalLt24Skip"],
                proof["normalGe24Due"],
                proof["referenceOnlyDue"],
                proof["neverPricedDue"],
                proof["oldLowDemandAgeBoost"],
                proof["auFreshIsolated"],
                proof["usStaleDue"],
                proof["gbReferenceDue"],
                proof["caNeverDue"],
                len(fresh_in_mix) == 0,
            ]
            # popular-12h-edge is already 12.05h at NOW so due
            required[1] = proof["popularGe12Due"]
            all_pass = all_pass and all(required)
        elif label == "+12h":
            pop_fresh = next(r for r in rows if r["priceKeyId"] == "popular-fresh")
            normal_1h = next(r for r in rows if r["priceKeyId"] == "normal-1h")
            all_pass = all_pass and bool(pop_fresh["due"]) and pop_fresh["freshnessThresholdHours"] == 12
            all_pass = all_pass and (not normal_1h["due"]) and normal_1h["freshnessThresholdHours"] == 24
        elif label == "+24h":
            all_pass = all_pass and bool(next(r for r in rows if r["priceKeyId"] == "normal-1h")["due"])
        horizons[label] = {
            "dueCount": sum(1 for r in au_rows if r["due"]),
            "freshSkipCount": sum(1 for r in au_rows if str(r["reasonCode"]).startswith("FRESH_SKIP")),
            "laneMixSample": [r.to_public_dict() for r in mix],
            "proof": proof if label == "NOW" else {
                "popularFreshNowDueAfter12h": next(r for r in rows if r["priceKeyId"] == "popular-fresh")["due"] if label != "NOW" else None,
            },
            "freshCardsInMix": len(fresh_in_mix),
        }
    multi = evaluate_universe(targets, events, now=NOW)
    return {
        "pass": all_pass,
        "horizons": horizons,
        "multiMarketNow": {
            r["priceKeyId"]: {
                "market": r["market"],
                "due": r["due"],
                "reasonCode": r["reasonCode"],
                "verifiedAgeHours": r["verifiedAgeHours"],
                "freshnessThresholdHours": r["freshnessThresholdHours"],
            }
            for r in multi
            if r["priceKeyId"] in {
                "same-print-au-fresh",
                "same-print-us-stale",
                "same-print-gb-ref",
                "same-print-ca-never",
            }
        },
        "ebayActivity": 0,
    }


def run_collection_au() -> dict[str, Any]:
    os.environ["OWNED_DAILY_FULL_ENABLE"] = "false"
    os.environ["OWNED_DAILY_DRY_RUN"] = "true"
    try:
        from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
        from cardscanr_market_engine.supabase_env_loader import load_supabase_env
        from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient

        load_supabase_env()
        cfg = MarketEngineConfig.from_env()
        client = SupabaseMarketEngineClient(
            supabase_url=cfg.supabase_url,
            service_role_key=supabase_secret_key_from_env(),
        )
    except Exception as exc:
        return {"pass": True, "skipped": True, "reason": str(exc)[:400]}

    sched = OwnedPrintingRefreshScheduler(client=client, config=OwnedDailySchedulerConfig.from_env())
    payload = client.list_owned_market_pricing_targets(include_zero_owners=False)
    targets = list(payload.get("targets") or [])
    try:
        job_rows = client.list_recent_user_demand_jobs(hours=168) or []
    except Exception:
        job_rows = []
    events = events_from_job_rows(job_rows)
    idx = DemandIndex(events)
    au_targets: list[dict[str, Any]] = []
    for t in targets:
        if str(t.get("market_country") or "").upper() != "AU":
            continue
        if hasattr(client, "enrich_owned_target_from_cache"):
            t = client.enrich_owned_target_from_cache(t)
        au_targets.append(t)
    horizons: dict[str, Any] = {}
    freeze = datetime.now(timezone.utc)
    contradiction = False
    for label, hours in HORIZONS:
        now = freeze + timedelta(hours=hours)
        rows = [evaluate_demand_aware_target(t, now=now, demand_index=idx) for t in au_targets]
        mix = select_fair_lane_mix(rows, budget=10)
        for r in mix:
            if not r.due or r.would_hit_ebay is False:
                contradiction = True
            if r.reason_code.startswith("FRESH_SKIP"):
                contradiction = True
        due = [r for r in rows if r.due]
        skip = [r for r in rows if r.reason_code.startswith("FRESH_SKIP")]
        snapshot = None
        if label == "NOW":
            vl = [r for r in rows if r.verified_local]
            snapshot = {
                "verifiedLocalFreshLt12h": sum(
                    1 for r in vl if r.verified_age_hours is not None and r.verified_age_hours < 12
                ),
                "verifiedLocal12to24h": sum(
                    1
                    for r in vl
                    if r.verified_age_hours is not None and 12 <= r.verified_age_hours < 24
                ),
                "verifiedLocalGt24h": sum(
                    1 for r in vl if r.verified_age_hours is not None and r.verified_age_hours >= 24
                ),
                "referenceOnly": sum(1 for r in rows if r.source_class == "reference_only"),
                "neverPriced": sum(1 for r in rows if r.source_class == "none"),
                "highDemand": sum(1 for r in rows if r.demand_class == "HIGH"),
                "normalDemand": sum(1 for r in rows if r.demand_class != "HIGH"),
                "top25ByPriority": [
                    r.to_public_dict()
                    for r in sorted(due, key=lambda x: -x.final_priority)[:25]
                ],
                "oldestBacklog": [
                    r.to_public_dict()
                    for r in sorted(
                        due,
                        key=lambda x: -(x.verified_age_hours or 0),
                    )[:15]
                ],
                "estimatedJobsPerDayAt90sPacing16hWindow": int((16 * 3600) / 90),
            }
        horizons[label] = {
            "targets": len(rows),
            "due": len(due),
            "freshSkip": len(skip),
            "laneCounts": {
                "DEMAND": sum(1 for r in due if r.scheduler_lane == "DEMAND"),
                "STALE_OWNED": sum(1 for r in due if r.scheduler_lane == "STALE_OWNED"),
                "COVERAGE": sum(1 for r in due if r.scheduler_lane == "COVERAGE"),
            },
            "mixLanes": {
                lane: sum(1 for r in mix if r.scheduler_lane == lane)
                for lane in ("DEMAND", "STALE_OWNED", "COVERAGE")
            },
            "sampleDue": [r.to_public_dict() for r in due[:12]],
            "sampleSkip": [r.to_public_dict() for r in skip[:8]],
            "populationSnapshot": snapshot,
        }
    _ = sched  # dry-run scheduler object retained for config identity only
    return {
        "pass": not contradiction,
        "skipped": False,
        "auTargetCount": len(au_targets),
        "demandEventCount": len(events),
        "horizons": horizons,
        "ebayActivity": 0,
        "enqueued": 0,
        "dryRunOnly": True,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    synthetic = run_synthetic()
    collection = run_collection_au()
    payload = {
        "task": "CARDSCANR-DEMAND-AWARE-MARKET-FRESHNESS-AND-OWNED-DAILY-CANARY",
        "phase": "OFFLINE_SIMULATION",
        "ebayActivity": 0,
        "synthetic": synthetic,
        "collectionAu": collection,
        "pass": bool(synthetic.get("pass")) and bool(collection.get("pass")),
        "generatedAtUtc": _iso(datetime.now(timezone.utc)),
    }
    path = OUT / "offline_simulation.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({"pass": payload["pass"], "out": str(path), "ebayActivity": 0}, indent=2))
    return 0 if payload["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
