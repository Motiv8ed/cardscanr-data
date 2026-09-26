#!/usr/bin/env python3
"""Live ebay_browser sold capability + pilot proofs for location-aware pricing.

Requires Playwright + Chrome. Does not invent sold results.

Usage:
  python tools/live_ebay_sold_market_proof.py --probe-markets
  python tools/live_ebay_sold_market_proof.py --pilot --limit 12
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.filters import filter_comps
from cardscanr_market_engine.international.acquisition_cost import (
    combine_acquisition_cost,
    unavailable_shipping,
)
from cardscanr_market_engine.international.evidence_gate import (
    evaluate_international_evidence_gate_from_stats,
)
from cardscanr_market_engine.international.market_fallback_policy import (
    classify_foreign_market_attempt,
    has_native_sold_route,
)
from cardscanr_market_engine.marketplaces import (
    browser_supported_market_routes,
    resolve_marketplace_config,
)
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest
from cardscanr_market_engine.pricing_stats import calculate_pricing_stats
from cardscanr_market_engine.providers.ebay_browser_provider import (
    EbayBrowserProviderConfig,
    EbayBrowserSoldCompsProvider,
)

REPORTS = ROOT / "reports"
OUT = REPORTS / "live_ebay_sold_market_proof_latest.json"


@dataclass(frozen=True)
class PilotCard:
    label: str
    card_name: str
    set_name: str
    set_code: str
    collector_number: str
    language: str
    home_market: str
    home_currency: str
    variant: str = "raw"
    condition: str = "raw"


FINAL_PILOT_CARDS: tuple[PilotCard, ...] = (
    PilotCard("AU common EN", "Pikachu", "Base Set", "base1", "58/102", "en", "AU", "AUD"),
    PilotCard("AU valuable EN", "Charizard", "Base Set", "base1", "4/102", "en", "AU", "AUD"),
    PilotCard("AU modern EN", "Iron Valiant ex", "Paradox Rift", "sv4", "249/182", "en", "AU", "AUD"),
    PilotCard("AU→US fallback EN", "Tropius", "Pitch Black", "me5", "001/078", "en", "AU", "AUD"),
    PilotCard("JA exact m6a", "ピカチュウ", "M6a", "m6a", "1", "ja", "AU", "AUD"),
    PilotCard("US home", "Pikachu", "Base Set", "base1", "58/102", "en", "US", "USD"),
    PilotCard("GB home", "Pikachu", "Base Set", "base1", "58/102", "en", "GB", "GBP"),
    PilotCard("CA probe", "Pikachu", "Base Set", "base1", "58/102", "en", "CA", "CAD"),
)

PILOT_CARDS = FINAL_PILOT_CARDS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _key_for(card: PilotCard, market: str, currency: str) -> MarketPriceKey:
    return MarketPriceKey.from_row(
        {
            "id": f"pilot-{card.set_code}-{card.collector_number}-{market}",
            "game": "pokemon",
            "card_name": card.card_name,
            "normalized_card_name": card.card_name.lower().replace(" ", "_"),
            "set_name": card.set_name,
            "set_code": card.set_code,
            "collector_number": card.collector_number,
            "language": card.language,
            "variant": card.variant,
            "condition": card.condition,
            "market_country": market.lower(),
            "currency": currency.lower(),
            "fingerprint": (
                f"pokemon|{card.language}|{card.set_code}|{card.collector_number}|"
                f"{card.card_name.lower()}|{card.variant}|{card.condition}|{market.lower()}|{currency.lower()}"
            ),
        }
    )


def _search_one(
    provider: EbayBrowserSoldCompsProvider,
    *,
    card: PilotCard,
    market: str,
    currency: str,
) -> dict[str, Any]:
    try:
        config = resolve_marketplace_config(
            market_country=market,
            currency=currency,
            marketplace="ebay",
        )
    except Exception as exc:
        return {
            "market": market,
            "currency": currency,
            "status": "UNSUPPORTED",
            "error": str(exc),
            "classification": "UNSUPPORTED",
        }

    key = _key_for(card, market, currency)
    request = ProviderRequest(
        price_key=key,
        market_country=config.market_country,
        currency=config.currency,
        marketplace=config.marketplace,
        provider_marketplace_id=config.provider_marketplace_id,
        provider_domain=config.provider_domain,
        search_locale=config.search_locale,
        display_name=config.display_name,
        market_config=config,
    )
    try:
        result = provider.fetch_comps(request)
        evaluated = filter_comps(key, result.comps)
        engine_config = MarketEngineConfig.from_env(require_supabase=False)
        stats = calculate_pricing_stats(evaluated, config=engine_config)
        gate = evaluate_international_evidence_gate_from_stats(stats)
        attempt = classify_foreign_market_attempt(
            included_count=int(stats.included_count or 0),
            recommended_price=stats.recommended_price,
            confidence=str(stats.confidence),
            evidence_outcome=gate.outcome,
        )
        accepted = [
            {
                "listingId": item.comp.source_listing_id,
                "soldPrice": item.comp.sold_price,
                "shippingPrice": item.comp.shipping_price,
                "currency": item.comp.currency,
                "soldDate": item.comp.sold_date.isoformat() if item.comp.sold_date else None,
                "title": (item.comp.title or "")[:120],
            }
            for item in evaluated
            if item.included_in_estimate
        ]
        rejected = [
            {
                "listingId": item.comp.source_listing_id,
                "reason": item.rejection_reason,
                "title": (item.comp.title or "")[:120],
            }
            for item in evaluated
            if item.rejection_reason
        ]
        shipping = unavailable_shipping(
            source_market=market,
            destination_market=card.home_market,
            reason="destination_specific_shipping_unavailable_for_sold_comps",
        )
        acquisition = combine_acquisition_cost(
            market_value=stats.recommended_price,
            market_value_currency=currency,
            shipping=shipping,
        )
        if stats.included_count and stats.recommended_price is not None:
            classification = "LIVE_SOLD_SUPPORTED"
        elif result.comps:
            classification = "SEARCH_SUPPORTED_SOLD_UNPROVEN"
        else:
            classification = "SEARCH_SUPPORTED_SOLD_UNPROVEN"
        return {
            "market": market,
            "currency": currency,
            "domain": config.provider_domain,
            "status": "ok",
            "classification": classification,
            "nativeRoute": has_native_sold_route(market),
            "query": result.query_used,
            "candidateCount": len(result.comps),
            "acceptedCount": stats.included_count,
            "rejectedCount": stats.rejected_count,
            "recommendedPrice": stats.recommended_price,
            "confidence": str(stats.confidence),
            "attemptStatus": attempt,
            "evidenceGate": gate.to_dict(),
            "accepted": accepted[:12],
            "rejected": rejected[:12],
            "shipping": shipping.to_dict(),
            "acquisition": acquisition.to_dict(),
        }
    except Exception as exc:
        return {
            "market": market,
            "currency": currency,
            "domain": config.provider_domain,
            "status": "error",
            "classification": "UNSUPPORTED" if "Unsupported" in str(exc) else "SEARCH_SUPPORTED_SOLD_UNPROVEN",
            "error": str(exc)[:400],
            "attemptStatus": classify_foreign_market_attempt(
                included_count=0,
                recommended_price=None,
                confidence=None,
                error=str(exc),
            ),
        }


def probe_markets(provider: EbayBrowserSoldCompsProvider) -> list[dict[str, Any]]:
    probe_card = PilotCard("probe", "Pikachu", "Base", "base1", "58", "en", "AU", "AUD")
    routes = list(browser_supported_market_routes())
    # Also attempt known unsupported identifiers for honest reporting.
    extra = [("NZ", "NZD"), ("JP", "JPY")]
    rows: list[dict[str, Any]] = []
    for market, currency in routes + extra:
        print(f"[probe] {market}/{currency}", flush=True)
        rows.append(_search_one(provider, card=probe_card, market=market, currency=currency))
    return rows


def run_pilot(provider: EbayBrowserSoldCompsProvider, *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for card in PILOT_CARDS[: max(1, limit)]:
        print(f"[pilot] {card.label} home={card.home_market}", flush=True)
        home_result: dict[str, Any]
        if has_native_sold_route(card.home_market):
            home_result = _search_one(
                provider,
                card=card,
                market=card.home_market,
                currency=card.home_currency,
            )
        else:
            home_result = {
                "market": card.home_market,
                "currency": card.home_currency,
                "status": "NO_NATIVE_SOLD_ROUTE",
                "classification": "UNSUPPORTED",
                "nativeRoute": False,
                "attemptStatus": "NO_NATIVE_SOLD_ROUTE",
                "note": (
                    "No verified native eBay sold browser route for this home market. "
                    "Any later estimate must be disclosed as foreign."
                ),
            }
        markets_attempted = [home_result]
        # If home insufficient / missing, try first foreign browser market.
        need_fallback = (
            home_result.get("status") == "NO_NATIVE_SOLD_ROUTE"
            or home_result.get("status") == "error"
            or int(home_result.get("acceptedCount") or 0) < 3
            or home_result.get("recommendedPrice") is None
            or str(home_result.get("confidence") or "").lower() == "low"
            or "fallback" in card.label.lower()
            or card.language in {"ja", "jp"}
        )
        if need_fallback:
            foreign = "US" if card.home_market != "US" else "GB"
            foreign_currency = "USD" if foreign == "US" else "GBP"
            print(f"[pilot] {card.label} fallback → {foreign}", flush=True)
            markets_attempted.append(
                _search_one(provider, card=card, market=foreign, currency=foreign_currency)
            )
        # CA intermittent: one retry on timeout/error.
        if card.home_market == "CA" and home_result.get("status") == "error":
            print(f"[pilot] {card.label} CA retry", flush=True)
            retry = _search_one(
                provider,
                card=card,
                market="CA",
                currency="CAD",
            )
            markets_attempted.append(retry)
            if retry.get("status") != "ok":
                home_result = {
                    **home_result,
                    "classification": "LIVE_SUPPORTED_INTERMITTENT",
                    "note": "CA timed out; US fallback is the safe degradation path.",
                }
                markets_attempted[0] = home_result
        rows.append(
            {
                "label": card.label,
                "card": asdict(card),
                "homeMarket": card.home_market,
                "marketsAttempted": markets_attempted,
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-markets", action="store_true")
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    if not args.probe_markets and not args.pilot:
        parser.error("Pass --probe-markets and/or --pilot")

    # Modest timeout bump for CA/session flakiness — no anti-bot bypass.
    import os

    os.environ.setdefault("EBAY_BROWSER_TIMEOUT_SECONDS", "60")
    os.environ.setdefault("EBAY_BROWSER_ENABLED", "true")

    config = EbayBrowserProviderConfig.from_env()
    provider = EbayBrowserSoldCompsProvider(config=config)
    report: dict[str, Any] = {
        "capturedAtUtc": _utc_now(),
        "browserSupportedRoutes": [
            {"market": m, "currency": c} for m, c in browser_supported_market_routes()
        ],
    }
    try:
        if args.probe_markets:
            report["marketProbe"] = probe_markets(provider)
        if args.pilot:
            report["pilot"] = run_pilot(provider, limit=args.limit)
    finally:
        provider._close_browser_session()

    REPORTS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"wrote": str(OUT), "keys": list(report.keys())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
