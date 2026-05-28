#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.fingerprints import build_market_price_fingerprint, normalize_name
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest
from cardscanr_market_engine.providers import create_market_comps_provider
from cardscanr_market_engine.providers.errors import sanitize_provider_diagnostics
from cardscanr_market_engine.providers.query_builder import build_provider_search_query


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
    return parser.parse_args()


def build_request(args: argparse.Namespace) -> ProviderRequest:
    market = resolve_marketplace_config(
        market_country=args.market,
        currency=args.currency,
        marketplace="ebay",
    )
    fingerprint = build_market_price_fingerprint(
        game="pokemon",
        language=args.language,
        set_code=args.set_code,
        set_name=args.set_name,
        collector_number=args.collector_number,
        card_name=args.card_name,
        variant=args.variant,
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
        variant=args.variant.lower(),
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
            "sold_date": comp.sold_date.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "listing_url": comp.listing_url,
            "condition_text": comp.condition_text,
            "raw_metadata": comp.raw_metadata,
        }
    )


def main() -> int:
    args = parse_args()
    request = build_request(args)
    query = build_provider_search_query(request)
    provider = create_market_comps_provider("ebay_browser")
    result = provider.fetch_comps(request)
    payload = sanitize_provider_diagnostics(
        {
            "status": "success",
            "finishedAtUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "provider": result.provider_name,
            "marketplace": result.marketplace,
            "query": {
                "query_text": query.query_text,
                "search_url": query.search_url,
                "market_country": query.market_country,
                "currency": query.currency,
            },
            "resultCount": len(result.comps),
            "results": [comp_to_dict(comp) for comp in result.comps],
            "raw_metadata": result.raw_metadata,
        }
    )
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
