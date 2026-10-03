#!/usr/bin/env python3
"""Offline audit of five stopped-run scheduler candidates. NO eBay contact."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig, supabase_secret_key_from_env
from cardscanr_market_engine.owned_daily_scheduler import OwnedDailySchedulerConfig, OwnedPrintingRefreshScheduler
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.supabase_env_loader import load_supabase_env

KEYS = [
    "52e98068-30ba-466a-a9e6-0316b2af59e7",
    "35349da5-49f8-4d28-9dc2-39e6261bec97",
    "83382041-41a4-4fda-a359-8d0db5265934",
    "96d5f5a8-a248-4b85-856f-921ec6d6015f",
    "db661320-9d44-4a88-8c96-722ca1c78f12",
]


def main() -> int:
    load_supabase_env()
    cfg = MarketEngineConfig.from_env()
    client = SupabaseMarketEngineClient(
        supabase_url=cfg.supabase_url,
        service_role_key=supabase_secret_key_from_env(),
    )
    sched = OwnedPrintingRefreshScheduler(client=client, config=OwnedDailySchedulerConfig.from_env())
    now = datetime.now(timezone.utc)
    payload = client.list_owned_market_pricing_targets(include_zero_owners=False)
    targets = payload.get("targets") or []
    by_id = {
        str(t.get("market_price_key_id") or ""): t
        for t in targets
        if isinstance(t, dict) and t.get("market_price_key_id")
    }
    out = []
    for kid in KEYS:
        t = by_id.get(kid)
        cache = client.get_cache_row(price_key_id=kid) or {}
        decision = sched.evaluate_target(t, now=now) if t else None
        # Correct category from cache truth
        price = cache.get("current_market_price")
        rec = cache.get("recommended_price")
        last = cache.get("last_updated_at")
        refresh = str(cache.get("refresh_status") or "").lower()
        never = price is None or float(price or 0) <= 0
        if never:
            correct = "P0_NEVER_PRICED"
        elif refresh == "failed":
            correct = "P2_FAILED_RETRY"
        elif last is None:
            correct = "P1_STALE_GT_24H"
        else:
            try:
                lu = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                age_h = (now - lu).total_seconds() / 3600.0
                correct = "P1_STALE_GT_24H" if age_h >= 24 else ("P3_APPROACHING_DUE" if age_h >= 22 else "FRESH_SKIP")
            except Exception:
                correct = "UNKNOWN"
        rpc_price = None if not t else t.get("current_market_price")
        out.append(
            {
                "priceKeyId": kid,
                "foundInRpc": t is not None,
                "rpc": {
                    "fingerprint": None if not t else t.get("fingerprint"),
                    "card": None if not t else t.get("card_name"),
                    "set": None if not t else t.get("set_name"),
                    "collector": None if not t else t.get("collector_number"),
                    "current_market_price": rpc_price,
                    "recommended_absent_in_rpc": True,
                    "last_updated_at": None if not t else t.get("last_updated_at"),
                    "refresh_status": None if not t else t.get("refresh_status"),
                    "owned_priority_band": None if not t else t.get("owned_priority_band"),
                    "display_price_source": None if not t else t.get("display_price_source"),
                    "provider": None if not t else t.get("provider"),
                }
                if t
                else None,
                "cache": {
                    "current_market_price": price,
                    "recommended_price": rec,
                    "last_updated_at": last,
                    "updated_at": cache.get("updated_at"),
                    "refresh_status": cache.get("refresh_status"),
                    "display_price_source": cache.get("display_price_source"),
                    "provider": cache.get("provider"),
                    "latest_snapshot_id": cache.get("latest_snapshot_id"),
                    "last_error_message": (cache.get("last_error_message") or "")[:300],
                },
                "schedulerDecision": None
                if decision is None
                else {
                    "shouldEnqueue": decision.should_enqueue,
                    "reason": decision.reason,
                    "band": decision.details.get("owned_priority_band"),
                    "score": decision.score,
                    "detailsPrice": decision.details.get("current_market_price"),
                },
                "correctCategoryFromCache": correct,
                "categoryCorrect": (
                    None
                    if decision is None
                    else decision.details.get("owned_priority_band") == correct
                ),
                "mismatchNote": None,
            }
        )
        row = out[-1]
        if t and rpc_price is None and price is not None and float(price) > 0:
            row["mismatchNote"] = "RPC_NULL_PRICE_BUT_CACHE_HAS_PRICE"
        elif t and rpc_price is not None and float(rpc_price) > 0 and decision and decision.details.get("owned_priority_band") == "P0_NEVER_PRICED":
            row["mismatchNote"] = "EVALUATE_TARGET_IGNORED_PRICE"
        elif not t:
            row["mismatchNote"] = "NOT_IN_RPC_TARGETS"
        # Harness BEFORE used current or recommended
        harness_before_price = price if price is not None else rec
        row["harnessBeforePriceSemantics"] = harness_before_price
        if never and harness_before_price is not None and float(harness_before_price or 0) > 0:
            row["mismatchNote"] = (row["mismatchNote"] or "") + "|HARNESS_USED_RECOMMENDED_AS_PRICE"
    art = ROOT / "reports" / "artifacts" / "x11_nav_reliability_harness_closure"
    art.mkdir(parents=True, exist_ok=True)
    path = art / "scheduler_five_card_audit.json"
    path.write_text(json.dumps({"auditedAtUtc": now.isoformat().replace("+00:00", "Z"), "cards": out}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(path), "cards": out}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
