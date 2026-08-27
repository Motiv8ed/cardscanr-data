"""Refresh official ECB FX reference rates into the shared CardScanR cache."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.international.ecb_client import ECB_DAILY_XML_URL, ECB_SOURCE_LABEL
from cardscanr_market_engine.international.fx_cache import (
    DEFAULT_FETCH_MAX_AGE,
    evaluate_ecb_fx_freshness,
    fx_health_payload,
    maybe_refresh_ecb_fx_cache,
    resolve_fx_cache_path,
)
from cardscanr_market_engine.supabase_env_loader import load_supabase_env


def _maybe_sync_supabase(payload: dict) -> dict | None:
    """Mirror FX health into Supabase for Admin (service role)."""
    try:
        load_supabase_env()
        from cardscanr_market_engine.config import MarketEngineConfig
        from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient

        config = MarketEngineConfig.from_env(require_supabase=True)
        client = SupabaseMarketEngineClient(
            supabase_url=config.supabase_url,
            service_role_key=config.supabase_service_role_key,
        )
        fx = evaluate_ecb_fx_freshness(cache=payload, now=datetime.now(timezone.utc))
        row = {
            "id": 1,
            "source": "ECB",
            "source_label": ECB_SOURCE_LABEL,
            "source_url": payload.get("sourceUrl") or ECB_DAILY_XML_URL,
            "provider_rate_date": payload.get("providerRateDate"),
            "fetched_at": payload.get("fetchedAt"),
            "last_attempt_at": payload.get("lastAttemptAt") or payload.get("fetchedAt"),
            "last_success_at": payload.get("fetchedAt") if payload.get("status") != "failed" else None,
            "last_error": payload.get("lastError"),
            "consecutive_failures": int(payload.get("consecutiveFailures") or 0),
            "health": fx.health,
            "allows_conversion": fx.allows_conversion,
            "block_reason": fx.block_reason,
            "currencies": payload.get("currencies") or [],
            "eur_rates": payload.get("eurRates") or {},
            "pair_rates": payload.get("pairRates") or {},
            "fetch_max_age_hours": int(DEFAULT_FETCH_MAX_AGE.total_seconds() // 3600),
            "outage_grace_hours": 96,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        client._table_post(  # noqa: SLF001
            "market_fx_rate_cache",
            row,
            prefer="resolution=merge-duplicates,return=representation",
            on_conflict="id",
        )
        return row
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh ECB FX reference rates for CardScanR")
    parser.add_argument("--force", action="store_true", help="Force fetch even if cache is fresh")
    parser.add_argument(
        "--ttl-hours",
        type=float,
        default=12.0,
        help="Skip fetch when last successful refresh is newer than this TTL (ignored with --force)",
    )
    parser.add_argument("--no-supabase-sync", action="store_true")
    args = parser.parse_args()

    load_supabase_env()
    try:
        payload = maybe_refresh_ecb_fx_cache(
            ttl=timedelta(hours=max(0.1, args.ttl_hours)),
            force=args.force,
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 1

    synced = None if args.no_supabase_sync else _maybe_sync_supabase(payload)
    health = fx_health_payload(now=datetime.now(timezone.utc))
    report = {
        "status": payload.get("status") or "success",
        "cachePath": str(resolve_fx_cache_path()),
        "source": payload.get("source"),
        "sourceLabel": ECB_SOURCE_LABEL,
        "sourceUrl": payload.get("sourceUrl") or ECB_DAILY_XML_URL,
        "providerRateDate": payload.get("providerRateDate"),
        "fetchedAt": payload.get("fetchedAt"),
        "currencies": payload.get("currencies"),
        "samplePairRates": {
            key: (payload.get("pairRates") or {}).get(key)
            for key in ("EUR:AUD", "USD:AUD", "GBP:AUD", "CAD:AUD", "JPY:AUD", "AUD:USD")
        },
        "health": health,
        "supabaseSynced": synced is not None,
    }
    print(json.dumps(report, indent=2))
    return 0 if health.get("allowsConversion") else 2


if __name__ == "__main__":
    raise SystemExit(main())
