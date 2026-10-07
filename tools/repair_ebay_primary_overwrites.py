#!/usr/bin/env python3
"""Restore selected market estimates from preserved eBay snapshots.

Does not invent prices. Re-selects from existing snapshots/cache observations
using shared source-precedence policy. Preserves reference_* secondary fields.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import supabase_secret_key_from_env
from cardscanr_market_engine.price_source_precedence import (
    observations_from_cache_and_snapshots,
    select_customer_market_price,
    TIER_FRESH_EBAY,
    TIER_STALE_EBAY,
)
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


def utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def repair_key(client: SupabaseMarketEngineClient, price_key_id: str, *, dry_run: bool) -> dict:
    key_rows = client._table_get(
        "market_price_keys",
        params={
            "select": "id,fingerprint,card_name,set_code,collector_number,market_country,currency",
            "id": f"eq.{price_key_id}",
            "limit": "1",
        },
    )
    if not key_rows:
        return {"price_key_id": price_key_id, "status": "missing_key"}
    key = key_rows[0]
    cache_rows = client._table_get(
        "market_price_cache",
        params={"select": "*", "price_key_id": f"eq.{price_key_id}", "limit": "1"},
    )
    cache = cache_rows[0] if cache_rows else None
    snaps = client._table_get(
        "market_price_snapshots",
        params={
            "select": "id,provider,marketplace,median_price,recommended_price,sample_size,confidence,created_at",
            "price_key_id": f"eq.{price_key_id}",
            "order": "created_at.desc",
            "limit": "40",
        },
    )
    observations = observations_from_cache_and_snapshots(cache=cache, snapshots=snaps)
    selected = select_customer_market_price(
        observations,
        market_country=str(key.get("market_country") or ""),
    )
    if selected.tier not in {TIER_FRESH_EBAY, TIER_STALE_EBAY} or selected.price is None:
        return {
            "price_key_id": price_key_id,
            "fingerprint": key.get("fingerprint"),
            "status": "no_ebay_to_restore",
            "selected_tier": selected.tier,
            "selected_provider": selected.provider,
        }

    current_provider = str((cache or {}).get("provider") or "").lower()
    current_price = (cache or {}).get("current_market_price")
    already_ok = (
        current_provider in {"ebay_browser", "ebay"}
        and current_price is not None
        and abs(float(current_price) - float(selected.price)) < 0.001
    )
    result = {
        "price_key_id": price_key_id,
        "fingerprint": key.get("fingerprint"),
        "card": f"{key.get('card_name')} {key.get('set_code')} #{key.get('collector_number')}",
        "market": key.get("market_country"),
        "currency": key.get("currency"),
        "previous_provider": (cache or {}).get("provider"),
        "previous_price": current_price,
        "previous_display_source": (cache or {}).get("display_price_source"),
        "restored_price": selected.price,
        "restored_provider": selected.provider,
        "restored_display_source": selected.display_source,
        "restored_snapshot_id": selected.snapshot_id,
        "reference_price_preserved": (cache or {}).get("reference_price"),
        "reference_provider_preserved": (cache or {}).get("reference_provider"),
        "status": "already_correct" if already_ok else ("dry_run_restore" if dry_run else "restored"),
    }
    if already_ok or dry_run:
        return result

    payload = {
        "price_key_id": price_key_id,
        "current_market_price": selected.price,
        "median_price": selected.price,
        "recommended_price": selected.price,
        "sample_size": selected.sample_size,
        "confidence": selected.confidence or "low",
        "provider": selected.provider,
        "marketplace": selected.marketplace or f"EBAY_{str(key.get('market_country') or 'AU').upper()}",
        "market_country": str(key.get("market_country") or "au").upper(),
        "currency": str(key.get("currency") or "aud").upper(),
        "display_price_source": selected.display_source,
        "latest_snapshot_id": selected.snapshot_id,
        "last_updated_at": utc_iso(selected.observed_at) if selected.observed_at else utc_iso(),
        "refresh_status": "completed",
        "last_error_message": None,
        # Keep secondary reference observation when present.
        "reference_price": (cache or {}).get("reference_price"),
        "reference_provider": (cache or {}).get("reference_provider"),
        "reference_updated_at": (cache or {}).get("reference_updated_at"),
    }
    client.upsert_cache(payload)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-json", default="reports/artifacts/price_precedence/ebay_primary_overwrite_audit.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    client = SupabaseMarketEngineClient(
        supabase_url=os.environ["SUPABASE_URL"].rstrip("/"),
        service_role_key=supabase_secret_key_from_env(),
    )
    audit = json.loads(Path(args.audit_json).read_text(encoding="utf-8-sig"))
    overwrites = list(audit.get("overwrites") or [])
    if args.limit > 0:
        overwrites = overwrites[: args.limit]

    repaired = []
    for row in overwrites:
        # Only repair when current selected provider is still a lower-tier overwrite.
        provider = str(row.get("replacement_provider") or "").lower()
        if provider in {"ebay_browser", "ebay"}:
            continue
        repaired.append(repair_key(client, str(row["price_key_id"]), dry_run=args.dry_run))

    out = {
        "generatedAtUtc": utc_iso(),
        "dryRun": args.dry_run,
        "candidates": len(overwrites),
        "attempted": len(repaired),
        "restored": sum(1 for r in repaired if r.get("status") == "restored"),
        "alreadyCorrect": sum(1 for r in repaired if r.get("status") == "already_correct"),
        "noEbay": sum(1 for r in repaired if r.get("status") == "no_ebay_to_restore"),
        "results": repaired,
    }
    out_path = Path("reports/artifacts/price_precedence/ebay_primary_restore_report.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: out[k] for k in out if k != "results"}, indent=2))
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
