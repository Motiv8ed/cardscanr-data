#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.fingerprints import build_market_price_fingerprint, normalize_market_variant, normalize_name
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.providers import create_market_comps_provider
from cardscanr_market_engine.providers.errors import sanitize_provider_diagnostics
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient

LATEST_REPORT = ROOT / "reports" / "ebay_browser_live_write_smoke_latest.json"
RUNS_REPORT = ROOT / "reports" / "ebay_browser_live_write_smoke_runs.jsonl"


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run exactly one live eBay browser Supabase write smoke.")
    parser.add_argument("--market", default="AU")
    parser.add_argument("--currency", default="AUD")
    parser.add_argument("--card-name", default="Charizard ex")
    parser.add_argument("--collector-number", default="125/197")
    parser.add_argument("--set-name", default="Obsidian Flames")
    parser.add_argument("--set-code", default="sv03")
    parser.add_argument("--condition", default="raw")
    parser.add_argument("--variant", default="raw")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Plan the one-card request without Supabase writes or browser work.")
    return parser.parse_args()


def _require_flags() -> None:
    required = {
        "MARKET_LOOKUP_PROVIDER": "ebay_browser",
        "ENABLE_EBAY_REAL_LOOKUP": "true",
        "CONFIRM_LIVE_EBAY_WRITE": "true",
    }
    for name, expected in required.items():
        if os.getenv(name, "").strip().lower() != expected:
            raise RuntimeError(f"{name}={expected} is required")


def _identity(args: argparse.Namespace) -> dict[str, str]:
    market = args.market.lower()
    currency = args.currency.lower()
    variant = normalize_market_variant(args.variant)
    fingerprint = build_market_price_fingerprint(
        game="pokemon",
        language="en",
        set_code=args.set_code,
        set_name=args.set_name,
        collector_number=args.collector_number,
        card_name=args.card_name,
        variant=variant,
        condition=args.condition,
        market_country=market,
        currency=currency,
    )
    return {
        "game": "pokemon",
        "card_name": args.card_name,
        "normalized_card_name": normalize_name(args.card_name),
        "set_name": args.set_name,
        "set_code": args.set_code,
        "collector_number": args.collector_number,
        "language": "en",
        "variant": variant,
        "condition": args.condition,
        "market_country": market,
        "currency": currency,
        "fingerprint": fingerprint,
    }


def _summarize_bundle(bundle: dict[str, Any] | None) -> dict[str, Any]:
    if not bundle:
        return {}
    cache = bundle.get("cache") or {}
    snapshot = bundle.get("latest_snapshot") or {}
    diagnostics = snapshot.get("diagnostics_json") or {}
    price_views = diagnostics.get("priceViews") or {}
    item_price = price_views.get("itemPrice") or {}
    landed_price = price_views.get("landedPrice") or {}
    evidence = bundle.get("sold_listing_evidence") or []
    included = [item for item in evidence if item.get("included_in_estimate")]
    rejected = [item for item in evidence if not item.get("included_in_estimate")]
    included_variants: dict[str, int] = {}
    rejected_variant_mismatch_count = 0
    def compact_comp(item: dict[str, Any]) -> dict[str, Any]:
        raw = item.get("raw_json") or {}
        quality = raw.get("compQuality") or {}
        return {
            "query_index": raw.get("query_index"),
            "query_source": raw.get("query_source"),
            "query_sources": raw.get("query_sources") or [],
            "item_id": raw.get("item_id"),
            "title": item.get("title"),
            "sold_price": item.get("sold_price"),
            "shipping_price": item.get("shipping_price"),
            "total_price": item.get("total_price"),
            "listing_url": item.get("listing_url"),
            "match_score": item.get("match_score"),
            "url_quality": quality.get("url_quality") or raw.get("url_quality"),
            "requested_variant": quality.get("requested_variant") or raw.get("requested_variant"),
            "detected_variant": quality.get("detected_variant") or raw.get("detected_variant"),
            "variant_match": quality.get("variant_match"),
            "variant_warning": quality.get("variant_warning") or raw.get("variant_warning"),
            "collector_number_match_quality": raw.get("collector_number_match_quality"),
            "set_match_quality": raw.get("set_match_quality"),
            "rejection_reason": item.get("rejection_reason"),
        }
    for item in included:
        variant = str(compact_comp(item).get("detected_variant") or "unknown")
        included_variants[variant] = included_variants.get(variant, 0) + 1
    for item in rejected:
        if str(item.get("rejection_reason") or "").startswith(("wrong_variant_", "weak_variant_")):
            rejected_variant_mismatch_count += 1
    return {
        "cache_row_id": cache.get("id"),
        "snapshot_id": snapshot.get("id"),
        "recommended_price": cache.get("current_market_price") or cache.get("recommended_price"),
        "confidence": cache.get("confidence") or snapshot.get("confidence"),
        "bundle_state": bundle.get("state"),
        "bundle_cache_state": bundle.get("cache_state"),
        "bundle_refresh_state": bundle.get("refresh_state"),
        "current_market_evidence_available": bundle.get("current_market_evidence_available"),
        "stale_cache_available": bundle.get("stale_cache_available"),
        "cache_price_summary": {
            "current_market_price": cache.get("current_market_price"),
            "recommended_price": cache.get("recommended_price"),
            "item_recommended_price": item_price.get("recommended"),
            "item_median_price": item_price.get("median"),
            "item_low_price": item_price.get("low"),
            "item_high_price": item_price.get("high"),
            "landed_recommended_price": landed_price.get("recommended"),
            "landed_median_price": landed_price.get("median"),
            "landed_low_price": landed_price.get("low"),
            "landed_high_price": landed_price.get("high"),
            "median_price": cache.get("median_price"),
            "sample_size": cache.get("sample_size"),
            "confidence": cache.get("confidence"),
            "last_updated_at": cache.get("last_updated_at"),
            "marketplace": cache.get("marketplace"),
            "market_country": cache.get("market_country"),
            "currency": cache.get("currency"),
            "included_count": snapshot.get("included_count"),
            "rejected_count": snapshot.get("rejected_count"),
            "price_basis": price_views.get("priceBasis") or "item_price",
            "landed_price_available": bool(price_views.get("landedPriceAvailable")),
        },
        "evidence_count": len(evidence),
        "included_count": len(included),
        "rejected_count": len(rejected),
        "url_quality_counts": diagnostics.get("url_quality_counts") or {},
        "confidence_warnings": diagnostics.get("confidence_warnings") or [],
        "price_spread_ratio": diagnostics.get("price_spread_ratio"),
        "included_price_distribution": diagnostics.get("included_price_distribution") or [],
        "no_reliable_price_reason": diagnostics.get("no_reliable_price_reason"),
        "query_attempts": diagnostics.get("query_attempts") or [],
        "query_attempts_used": diagnostics.get("query_attempts_used"),
        "query_stop_reason": diagnostics.get("query_stop_reason"),
        "included_variants_summary": included_variants,
        "rejected_variant_mismatch_count": rejected_variant_mismatch_count,
        "top_included_comps": [compact_comp(item) for item in included[:5]],
        "top_rejected_comps": [compact_comp(item) for item in rejected[:10]],
    }


def _validation_flags(*, action: str, worker_result: dict[str, Any] | None) -> dict[str, Any]:
    if action == "cache_fresh":
        return {
            "live_lookup_performed": False,
            "used_cached_result": True,
            "pricing_model_validated": False,
            "message": "Cache was fresh; rerun with -ForceRefresh to validate a new live pricing calculation.",
        }
    completed = bool(worker_result and worker_result.get("status") == "completed")
    return {
        "live_lookup_performed": completed,
        "used_cached_result": False,
        "pricing_model_validated": completed,
    }


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    started = utc_iso()
    identity = _identity(args)
    market_config = resolve_marketplace_config(
        market_country=identity["market_country"],
        currency=identity["currency"],
        marketplace="ebay",
    )
    dry_run = bool(getattr(args, "dry_run", False))
    force_refresh = bool(getattr(args, "force_refresh", False))
    if dry_run:
        return sanitize_provider_diagnostics(
            {
                "status": "dry_run",
                "startedAtUtc": started,
                "finishedAtUtc": utc_iso(),
                "identity": identity,
                "requested_variant": identity["variant"],
                "request_market_price_refresh": {"action": "dry_run_only", "force_refresh": force_refresh},
                "force_refresh_requested": force_refresh,
                "key_id": None,
                "job_id": None,
                "job_status": "dry_run",
                "evidence_count": None,
                "included_count": None,
                "rejected_count": None,
                "recommended_price": None,
                "confidence": None,
                "cache_row_id": None,
                "snapshot_id": None,
                "market": {
                    "market_country": identity["market_country"].upper(),
                    "currency": identity["currency"].upper(),
                    "provider_domain": market_config.provider_domain,
                    "provider_marketplace_id": market_config.provider_marketplace_id,
                    "market_scope": os.getenv("EBAY_MARKET_SCOPE", "marketplace").strip().lower() or "marketplace",
                },
            }
        )

    _require_flags()
    config = MarketEngineConfig.from_env(require_supabase=True)
    client = SupabaseMarketEngineClient(
        supabase_url=config.supabase_url,
        service_role_key=config.supabase_service_role_key,
        timeout_seconds=60,
    )
    refresh = client.request_market_price_refresh(
        **identity,
        reason="live_ebay_write_smoke",
        force_refresh=force_refresh,
    )
    action = str(refresh.get("action"))
    worker_result: dict[str, Any] | None = None
    job_id = str(refresh.get("job_id") or "")
    if action == "cache_fresh":
        bundle = client.get_market_price_bundle(fingerprint=identity["fingerprint"], evidence_limit=100)
    elif action in {"job_enqueued", "active_job_exists"} and job_id:
        existing_job = client.get_refresh_job(job_id=job_id)
        if existing_job is None:
            raise RuntimeError(f"Refresh job not found: {job_id}")
        if existing_job.price_key_id != str(refresh.get("price_key_id")):
            raise RuntimeError("Refresh job price_key_id does not match requested key")
        if existing_job.status == "queued":
            job = client.claim_specific_refresh_job(job_id=job_id, worker_id=config.worker_id)
            if job is None:
                raise RuntimeError(f"Refresh job {job_id} could not be claimed safely")
            runner = MarketPriceJobRunner(
                client=client,
                provider=create_market_comps_provider("ebay_browser"),
                config=config,
            )
            worker_result = runner.run_job(job)
        else:
            worker_result = {"status": "skipped", "reason": f"job_status_{existing_job.status}", "jobId": job_id}
        bundle = client.get_market_price_bundle(fingerprint=identity["fingerprint"], evidence_limit=100)
    else:
        raise RuntimeError(f"Unexpected refresh action: {action}")

    report = {
        "status": "success",
        "startedAtUtc": started,
        "identity": identity,
        "requested_variant": identity["variant"],
        "request_market_price_refresh": refresh,
        "force_refresh_requested": force_refresh,
        "key_id": refresh.get("price_key_id"),
        "job_id": job_id or None,
        "job_status": refresh.get("job_status"),
        "worker_result": worker_result,
        "market": {
            "market_country": identity["market_country"].upper(),
            "currency": identity["currency"].upper(),
            "provider_domain": market_config.provider_domain,
            "provider_marketplace_id": market_config.provider_marketplace_id,
            "market_scope": os.getenv("EBAY_MARKET_SCOPE", "marketplace").strip().lower() or "marketplace",
        },
        "cooldown": {
            "cooldown_hours": refresh.get("cooldown_hours"),
            "cooldown_until": refresh.get("cooldown_until"),
            "cooldown_reason": refresh.get("cooldown_reason"),
            "cache_is_fresh": refresh.get("cache_is_fresh"),
        },
        **_validation_flags(action=action, worker_result=worker_result),
        **_summarize_bundle(bundle),
        "finishedAtUtc": utc_iso(),
    }
    return sanitize_provider_diagnostics(report)


def main() -> int:
    args = parse_args()
    try:
        report = run_smoke(args)
    except Exception as exc:
        report = sanitize_provider_diagnostics(
            {
                "status": "failed",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "finishedAtUtc": utc_iso(),
            }
        )
        write_json(LATEST_REPORT, report)
        append_jsonl(RUNS_REPORT, report)
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        return 1
    write_json(LATEST_REPORT, report)
    append_jsonl(RUNS_REPORT, report)
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
