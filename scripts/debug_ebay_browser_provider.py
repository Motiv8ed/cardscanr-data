#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.fingerprints import build_market_price_fingerprint, normalize_market_variant, normalize_name
from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.filters import filter_comps
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest
from cardscanr_market_engine.providers import create_market_comps_provider
from cardscanr_market_engine.providers.errors import sanitize_provider_diagnostics
from cardscanr_market_engine.providers.query_builder import build_provider_search_queries
from cardscanr_market_engine.pricing_stats import calculate_pricing_stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one local eBay browser provider lookup without writing to Supabase.")
    parser.add_argument("--market", default="AU", help="Market country, e.g. AU, US, GB, CA.")
    parser.add_argument("--currency", default="AUD", help="Currency, e.g. AUD, USD, GBP, CAD.")
    parser.add_argument("--card-name", required=True)
    parser.add_argument("--collector-number", required=True)
    parser.add_argument("--set-name", required=True)
    parser.add_argument("--set-code", default="")
    parser.add_argument("--language", default="en")
    parser.add_argument("--variant", default="raw")
    parser.add_argument("--condition", default="raw")
    parser.add_argument("--headed", action="store_true", help="Run Chrome headed for manual QA.")
    parser.add_argument("--browser-launch-timeout-seconds", type=int, default=120)
    parser.add_argument("--per-query-timeout-seconds", type=int, default=120)
    parser.add_argument("--total-card-timeout-seconds", type=int, default=120)
    return parser.parse_args()


def build_request(args: argparse.Namespace) -> ProviderRequest:
    market = resolve_marketplace_config(
        market_country=args.market,
        currency=args.currency,
        marketplace="ebay",
    )
    variant = normalize_market_variant(args.variant)
    fingerprint = build_market_price_fingerprint(
        game="pokemon",
        language=args.language,
        set_code=args.set_code,
        set_name=args.set_name,
        collector_number=args.collector_number,
        card_name=args.card_name,
        variant=variant,
        condition=args.condition,
        market_country=market.market_country,
        currency=market.currency,
    )
    price_key = MarketPriceKey(
        id="debug-local",
        game="pokemon",
        card_name=args.card_name,
        normalized_card_name=normalize_name(args.card_name),
        set_name=args.set_name,
        set_code=args.set_code or None,
        collector_number=args.collector_number,
        language=args.language.lower(),
        variant=variant,
        condition=args.condition.lower(),
        market_country=market.market_country.lower(),
        currency=market.currency.lower(),
        fingerprint=fingerprint,
    )
    return ProviderRequest(
        price_key=price_key,
        market_country=market.market_country,
        currency=market.currency,
        marketplace=market.marketplace,
        provider_marketplace_id=market.provider_marketplace_id,
        provider_domain=market.provider_domain,
        search_locale=market.search_locale,
        display_name=market.display_name,
        market_config=market,
    )


def comp_to_dict(comp: Any) -> dict[str, Any]:
    return sanitize_provider_diagnostics(
        {
            "source_listing_id": comp.source_listing_id,
            "title": comp.title,
            "sold_price": comp.sold_price,
            "shipping_price": comp.shipping_price,
            "total_price": comp.total_price,
            "currency": comp.currency,
            "sold_date": (
                comp.sold_date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if comp.sold_date is not None
                else None
            ),
            "listing_url": comp.listing_url,
            "condition_text": comp.condition_text,
            "raw_metadata": comp.raw_metadata,
        }
    )


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}
    return {}


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    debug_dir = ROOT / "reports" / "ebay_browser_debug" / "latest"
    os.environ.setdefault("EBAY_BROWSER_DEBUG_ARTIFACT_DIR", str(debug_dir))
    os.environ["EBAY_BROWSER_HEADLESS"] = "false" if args.headed else os.environ.get("EBAY_BROWSER_HEADLESS", "true")
    os.environ.setdefault("EBAY_BROWSER_LAUNCH_TIMEOUT_SECONDS", str(max(1, int(args.browser_launch_timeout_seconds))))
    os.environ.setdefault("EBAY_BROWSER_TIMEOUT_SECONDS", str(max(1, int(args.per_query_timeout_seconds))))
    request = build_request(args)
    query_ladder = build_provider_search_queries(request)
    provider = create_market_comps_provider("ebay_browser")
    browser_config = getattr(getattr(provider, "config", None), "safe_diagnostics", lambda: {})()
    try:
        result = provider.fetch_comps(request)
    except Exception as exc:
        diagnostics = getattr(exc, "diagnostics", {}) if hasattr(exc, "diagnostics") else {}
        debug_summary = _read_json_if_exists(debug_dir / "debug_summary.json")
        payload = sanitize_provider_diagnostics(
            {
                "status": "failed",
                "finishedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "elapsedMs": round((time.monotonic() - started) * 1000, 2),
                "error": str(exc),
                "errorType": type(exc).__name__,
                "timedOutStage": (diagnostics or {}).get("timedOutStage")
                or (debug_summary.get("stage_timings") or {}).get("timedOutStage"),
                "stageTimings": (diagnostics or {}).get("stageTimings") or debug_summary.get("stage_timings") or {},
                "providerDiagnostics": diagnostics,
                "browserConfig": browser_config,
                "debugArtifacts": {
                    "directory": str(debug_dir),
                    "pageHtml": str(debug_dir / "page.html"),
                    "screenshot": str(debug_dir / "screenshot.png"),
                    "summary": str(debug_dir / "debug_summary.json"),
                    "runsJsonl": str(ROOT / "reports" / "ebay_browser_debug" / "runs.jsonl"),
                },
                "debugSummary": debug_summary,
                "queryAttempts": [
                    {
                        "query_index": query.query_index,
                        "query_source": query.query_source,
                        "query_text": query.query_text,
                        "search_url": query.search_url,
                    }
                    for query in query_ladder
                ],
            }
        )
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
        return 2
    evaluated = filter_comps(request.price_key, result.comps)
    pricing_stats = calculate_pricing_stats(evaluated, config=MarketEngineConfig.from_env(require_supabase=False))
    included_count = sum(1 for item in evaluated if item.included_in_estimate)
    rejected_count = sum(1 for item in evaluated if not item.included_in_estimate)
    rejection_reason_summary: dict[str, int] = {}
    for item in evaluated:
        if item.included_in_estimate:
            continue
        key = str(item.rejection_reason or "unknown")
        rejection_reason_summary[key] = rejection_reason_summary.get(key, 0) + 1
    payload = sanitize_provider_diagnostics(
        {
            "status": "success",
            "finishedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "elapsedMs": round((time.monotonic() - started) * 1000, 2),
            "provider": result.provider_name,
            "marketplace": result.marketplace,
            "browserConfig": browser_config,
            "debugArtifacts": {
                "directory": str(debug_dir),
                "pageHtml": str(debug_dir / "page.html"),
                "screenshot": str(debug_dir / "screenshot.png"),
                "summary": str(debug_dir / "debug_summary.json"),
                "runsJsonl": str(ROOT / "reports" / "ebay_browser_debug" / "runs.jsonl"),
            },
            "query": {
                "query_text": query_ladder[0].query_text if query_ladder else None,
                "search_url": query_ladder[0].search_url if query_ladder else None,
                "market_country": query_ladder[0].market_country if query_ladder else request.market_country,
                "currency": query_ladder[0].currency if query_ladder else request.currency,
            },
            "queryAttempts": result.raw_metadata.get("queryAttempts")
            or [
                {
                    "query_index": query.query_index,
                    "query_source": query.query_source,
                    "query_text": query.query_text,
                    "search_url": query.search_url,
                }
                for query in query_ladder
            ],
            "queryAttemptsUsed": result.raw_metadata.get("queryAttemptsUsed"),
            "queryStopReason": result.raw_metadata.get("queryStopReason"),
            "failedQueryAttempts": result.raw_metadata.get("failedQueryAttempts") or [],
            "stageTimings": result.raw_metadata.get("stageTimings") or {},
            "resultCount": len(result.comps),
            "includedCount": included_count,
            "rejectedCount": rejected_count,
            "rejectionReasonSummary": dict(sorted(rejection_reason_summary.items(), key=lambda pair: pair[0])),
            "urlQualityCounts": result.raw_metadata.get("qualitySummary") or {},
            "priceSpreadRatio": pricing_stats.price_spread_ratio,
            "confidence": pricing_stats.confidence,
            "confidenceWarnings": list(pricing_stats.confidence_warnings),
            "recommendedPrice": pricing_stats.recommended_price,
            "priceBasis": pricing_stats.price_basis,
            "noReliablePriceReason": pricing_stats.no_reliable_price_reason,
            "topIncludedComps": [
                comp_to_dict(item.comp) for item in evaluated if item.included_in_estimate
            ][:5],
            "topRejectedComps": [
                {
                    **comp_to_dict(item.comp),
                    "rejection_reason": item.rejection_reason,
                }
                for item in evaluated
                if not item.included_in_estimate
            ][:10],
            "results": [comp_to_dict(comp) for comp in result.comps],
            "raw_metadata": result.raw_metadata,
        }
    )
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
