#!/usr/bin/env python3
"""Dry-run/live pilot for international pricing fallback on unresolved production keys."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.international.fallback_eligibility import (
    evaluate_international_fallback_eligibility,
)
from cardscanr_market_engine.international.market_fallback_policy import fallback_markets_for_key
from cardscanr_market_engine.models import MarketPriceKey
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


def _is_smoke_or_demo(fingerprint: str, card_name: str) -> bool:
    blob = f"{fingerprint} {card_name}".lower()
    return any(token in blob for token in ("demo_", "smoke", "qa_", "test_key"))


def _row_to_key(row: dict) -> MarketPriceKey:
    return MarketPriceKey.from_row(
        {
            "id": row.get("id"),
            "game": row.get("game") or "pokemon",
            "card_name": row.get("card_name") or "",
            "normalized_card_name": row.get("normalized_card_name") or "",
            "set_name": row.get("set_name") or "",
            "set_code": row.get("set_code"),
            "collector_number": row.get("collector_number") or "",
            "language": row.get("language") or "en",
            "variant": row.get("variant") or "raw",
            "condition": row.get("condition") or "raw",
            "market_country": row.get("market_country") or "AU",
            "currency": row.get("currency") or "AUD",
            "fingerprint": row.get("fingerprint") or "",
        }
    )


def load_unresolved_candidate_rows(client: SupabaseMarketEngineClient, *, limit: int) -> list[dict]:
    """Load null-price / missing-cache production keys for international eligibility audit.

    Intentionally ignores local eBay next_refresh_due_at cooldown: international
    fallback is an exception path when local/reference evidence is insufficient.
    """
    rows: list[dict] = []
    seen: set[str] = set()

    # Prefer dedicated client helper when available.
    if hasattr(client, "list_international_fallback_candidates"):
        for row in client.list_international_fallback_candidates(limit=limit):
            key_id = str(row.get("id") or "").strip()
            if key_id and key_id not in seen:
                rows.append(row)
                seen.add(key_id)
        return rows[:limit]

    missing = client.list_missing_cache_keys(limit=limit)
    for row in missing:
        key_id = str(row.get("id") or "").strip()
        if not key_id or key_id in seen:
            continue
        rows.append(row)
        seen.add(key_id)

    # Also pull cache rows with null price via refresh candidates with a wide due window.
    # Fall back to table scan of cache null prices through list_cache_refresh_candidates
    # is insufficient when next_refresh_due_at is still in the future, so query via
    # raw table get when client supports it.
    if hasattr(client, "_table_get"):
        cache_rows = client._table_get(
            "market_price_cache",
            params={
                "select": (
                    "price_key_id,current_market_price,display_price_source,provider,"
                    "verification_required,next_refresh_due_at,stale_after,last_error_message,"
                    "market_price_keys!inner(id,fingerprint,market_country,currency,language,game,"
                    "card_name,normalized_card_name,set_name,set_code,collector_number,variant,condition)"
                ),
                "current_market_price": "is.null",
                "limit": str(max(1, min(limit * 3, 200))),
            },
        )
        for row in cache_rows:
            key = row.get("market_price_keys")
            if isinstance(key, list):
                key = key[0] if key else None
            if not isinstance(key, dict):
                continue
            key_id = str(key.get("id") or "").strip()
            if not key_id or key_id in seen:
                continue
            rows.append(
                {
                    **key,
                    "current_market_price": row.get("current_market_price"),
                    "display_price_source": row.get("display_price_source"),
                    "provider": row.get("provider"),
                    "verification_required": row.get("verification_required"),
                    "next_refresh_due_at": row.get("next_refresh_due_at"),
                    "stale_after": row.get("stale_after"),
                    "last_error_message": row.get("last_error_message"),
                }
            )
            seen.add(key_id)
            if len(rows) >= limit:
                break
    return rows[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description="International pricing fallback pilot")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports" / "international_fallback_pilot_latest.json",
    )
    args = parser.parse_args()
    dry_run = not args.live

    config = MarketEngineConfig.from_env(require_supabase=True)
    client = SupabaseMarketEngineClient(
        supabase_url=config.supabase_url,
        service_role_key=config.supabase_service_role_key,
    )
    rows = load_unresolved_candidate_rows(client, limit=max(args.limit * 3, 50))
    candidates: list[dict] = []
    reason_counts: dict[str, int] = {}
    for row in rows:
        key = _row_to_key(row)
        if _is_smoke_or_demo(key.fingerprint, key.card_name):
            reason_counts["smoke_or_demo"] = reason_counts.get("smoke_or_demo", 0) + 1
            continue
        cache = {
            "current_market_price": row.get("current_market_price"),
            "display_price_source": row.get("display_price_source"),
            "verification_required": row.get("verification_required"),
            "provider": row.get("provider"),
            "next_refresh_due_at": row.get("next_refresh_due_at"),
            "stale_after": row.get("stale_after"),
            "last_error_message": row.get("last_error_message"),
        }
        eligibility = evaluate_international_fallback_eligibility(price_key=key, cache=cache)
        reason_counts[eligibility.reason] = reason_counts.get(eligibility.reason, 0) + 1
        candidates.append(
            {
                "priceKeyId": key.id,
                "fingerprint": key.fingerprint,
                "cardName": key.card_name,
                "setCode": key.set_code,
                "collectorNumber": key.collector_number,
                "language": key.language,
                "homeMarket": key.market_country,
                "currency": key.currency,
                "eligible": eligibility.eligible,
                "reason": eligibility.reason,
                "fallbackMarkets": list(fallback_markets_for_key(key)),
                "firstFallbackMarket": (
                    fallback_markets_for_key(key)[0] if fallback_markets_for_key(key) else None
                ),
            }
        )

    eligible = [item for item in candidates if item["eligible"]][: args.limit]
    from cardscanr_market_engine.international.fx_freshness import (
        evaluate_fx_freshness,
        fx_health_payload,
    )
    from datetime import datetime, timezone

    fx = evaluate_fx_freshness(
        rate_source=config.currency_rate_source,
        rate_timestamp=None,
        now=datetime.now(timezone.utc),
    )
    enqueued = []
    fx_blocked = not fx.allows_conversion
    if not dry_run and fx_blocked:
        # Do not burn eBay budget when conversion cannot safely publish.
        print(
            json.dumps(
                {
                    "livePilotBlocked": True,
                    "reason": fx.block_reason,
                    "fxHealth": fx_health_payload(rate_source=config.currency_rate_source),
                    "eligibleCount": len(eligible),
                    "note": "Eligible keys exist but FX is stale; refusing live enqueue.",
                },
                indent=2,
            )
        )
    elif not dry_run:
        for item in eligible[: min(5, args.limit)]:
            target = item["firstFallbackMarket"]
            if not target:
                continue
            reason = f"international_fallback:{target}"
            job = client.enqueue_refresh_job(
                price_key_id=item["priceKeyId"],
                reason=reason,
                priority=95,
            )
            enqueued.append(
                {
                    "priceKeyId": item["priceKeyId"],
                    "reason": reason,
                    "jobId": job.get("id"),
                    "status": job.get("status"),
                    "cardName": item["cardName"],
                }
            )

    payload = {
        "dryRun": dry_run,
        "rowsScanned": len(rows),
        "candidatesEvaluated": len(candidates),
        "eligibleCount": len(eligible),
        "reasonCounts": reason_counts,
        "eligibleCandidates": eligible,
        "allCandidates": candidates,
        "enqueued": enqueued,
        "fxHealth": fx_health_payload(rate_source=config.currency_rate_source),
        "livePilotBlockedByFx": (not dry_run) and fx_blocked,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "dryRun": dry_run,
                "eligibleCount": len(eligible),
                "reasonCounts": reason_counts,
                "eligibleSample": eligible[:10],
                "enqueued": enqueued,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
