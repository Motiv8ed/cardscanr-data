#!/usr/bin/env python3
"""Focused US title + AU→US FX acceptance proofs for eBay sold core.

Usage:
  python tools/live_ebay_us_title_acceptance.py --forensic
  python tools/live_ebay_us_title_acceptance.py --us-local
  python tools/live_ebay_us_title_acceptance.py --au-us-fx
  python tools/live_ebay_us_title_acceptance.py --au-local
  python tools/live_ebay_us_title_acceptance.py --all
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("EBAY_BROWSER_ENABLED", "true")
os.environ.setdefault("EBAY_BROWSER_TIMEOUT_SECONDS", "60")

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.currency_conversion import resolve_currency_conversion
from cardscanr_market_engine.filters import filter_comps
from cardscanr_market_engine.international.evidence_gate import (
    evaluate_international_evidence_gate_from_stats,
)
from cardscanr_market_engine.international.fx_cache import load_production_pair_rates
from cardscanr_market_engine.international.fx_freshness import assert_fx_allows_international_conversion
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest
from cardscanr_market_engine.pricing_stats import calculate_pricing_stats
from cardscanr_market_engine.providers.ebay_browser_provider import (
    EbayBrowserProviderConfig,
    EbayBrowserSoldCompsProvider,
    is_chrome_only_title,
)

OUT = ROOT / "reports" / "live_ebay_us_title_acceptance_latest.json"
CHROME_ACCEPTED = 0


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _key(*, name: str, set_name: str, set_code: str, number: str, lang: str, market: str, currency: str) -> MarketPriceKey:
    return MarketPriceKey.from_row(
        {
            "id": f"accept-{set_code}-{number}-{market}",
            "game": "pokemon",
            "card_name": name,
            "normalized_card_name": name.lower().replace(" ", "_"),
            "set_name": set_name,
            "set_code": set_code,
            "collector_number": number,
            "language": lang,
            "variant": "raw",
            "condition": "raw",
            "market_country": market.lower(),
            "currency": currency.lower(),
            "fingerprint": f"pokemon|{lang}|{set_code}|{number}|{name}|raw|raw|{market}|{currency}",
        }
    )


def _request(key: MarketPriceKey, market: str, currency: str) -> ProviderRequest:
    config = resolve_marketplace_config(market_country=market, currency=currency, marketplace="ebay")
    return ProviderRequest(
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


def _search(provider: EbayBrowserSoldCompsProvider, *, key: MarketPriceKey, market: str, currency: str) -> dict[str, Any]:
    global CHROME_ACCEPTED
    request = _request(key, market, currency)
    try:
        result = provider.fetch_comps(request)
    except Exception as exc:
        return {
            "market": market,
            "currency": currency,
            "status": "error",
            "error": str(exc)[:300],
            "candidateCount": 0,
            "acceptedCount": 0,
            "rejectedCount": 0,
            "recommendedPrice": None,
            "confidence": None,
            "accepted": [],
            "rejectedReasons": {},
            "chromeAccepted": 0,
        }
    engine = MarketEngineConfig.from_env(require_supabase=False)
    evaluated = filter_comps(key, result.comps)
    stats = calculate_pricing_stats(evaluated, config=engine)
    gate = evaluate_international_evidence_gate_from_stats(stats)
    accepted = []
    rejected_reasons: dict[str, int] = {}
    for item in evaluated:
        if item.included_in_estimate:
            chrome = is_chrome_only_title(item.comp.title)
            if chrome:
                CHROME_ACCEPTED += 1
            accepted.append(
                {
                    "title": item.comp.title,
                    "soldPrice": item.comp.sold_price,
                    "currency": item.comp.currency,
                    "soldDate": item.comp.sold_date.isoformat() if item.comp.sold_date else None,
                    "listingId": item.comp.source_listing_id,
                    "titleSource": item.comp.raw_metadata.get("titleSource"),
                    "chromeOnly": chrome,
                    "matchScore": item.match_score,
                    "collectorQuality": item.comp.raw_metadata.get("collector_number_match_quality"),
                    "setMatch": item.comp.raw_metadata.get("set_name_match"),
                }
            )
        else:
            reason = item.rejection_reason or "unknown"
            rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
    return {
        "market": market,
        "currency": currency,
        "status": "ok",
        "query": result.query_used,
        "candidateCount": len(result.comps),
        "acceptedCount": stats.included_count,
        "rejectedCount": stats.rejected_count,
        "recommendedPrice": stats.recommended_price,
        "confidence": str(stats.confidence),
        "evidenceGate": gate.to_dict(),
        "accepted": accepted,
        "rejectedReasons": rejected_reasons,
        "chromeAccepted": sum(1 for row in accepted if row["chromeOnly"]),
    }


def run_forensic(provider: EbayBrowserSoldCompsProvider) -> dict[str, Any]:
    """Capture title-source diagnostics for a small US sold query."""
    key = _key(
        name="Pikachu",
        set_name="Base Set",
        set_code="base1",
        number="58/102",
        lang="en",
        market="US",
        currency="USD",
    )
    request = _request(key, "US", "USD")
    # Use internal collect path via fetch, then inspect raw metadata.
    result = provider.fetch_comps(request)
    samples = []
    for comp in result.comps[:8]:
        samples.append(
            {
                "extractedTitle": comp.title,
                "titleSource": comp.raw_metadata.get("titleSource"),
                "chromeOnly": is_chrome_only_title(comp.title),
                "soldPrice": comp.sold_price,
                "currency": comp.currency,
                "listingId": comp.source_listing_id,
                "priceText": (comp.raw_metadata.get("priceText") or "")[:80],
                "rawSnippet": (comp.raw_metadata.get("rawTextSnippet") or "")[:180],
                "candidateSource": comp.raw_metadata.get("candidateSource"),
            }
        )
    chrome_count = sum(1 for s in samples if s["chromeOnly"])
    return {
        "query": result.query_used,
        "candidateCount": len(result.comps),
        "sampleCount": len(samples),
        "chromeTitleCount": chrome_count,
        "rootCauseHypothesis": (
            "NESTED_CONTROL_CAPTURED + FALLBACK_TEXT_TOO_BROAD"
            if chrome_count
            else "FIXED_AT_SOURCE"
        ),
        "samples": samples,
    }


def run_us_local(provider: EbayBrowserSoldCompsProvider) -> dict[str, Any]:
    key = _key(
        name="Pikachu",
        set_name="Base Set",
        set_code="base1",
        number="58/102",
        lang="en",
        market="US",
        currency="USD",
    )
    row = _search(provider, key=key, market="US", currency="USD")
    row["card"] = "Pikachu Base Set 58/102"
    row["pass"] = (
        int(row["acceptedCount"] or 0) >= 3
        and int(row["chromeAccepted"] or 0) == 0
        and row["recommendedPrice"] is not None
    )
    row["verdict"] = "US_LOCAL_LIVE_PASS" if row["pass"] else "US_LOCAL_LIVE_FAIL"
    return row


def run_au_local(provider: EbayBrowserSoldCompsProvider) -> dict[str, Any]:
    key = _key(
        name="Iron Valiant ex",
        set_name="Paradox Rift",
        set_code="sv4",
        number="249/182",
        lang="en",
        market="AU",
        currency="AUD",
    )
    row = _search(provider, key=key, market="AU", currency="AUD")
    row["card"] = "Iron Valiant ex Paradox Rift 249/182"
    row["localRemainsPrimary"] = int(row["acceptedCount"] or 0) >= 3
    row["pass"] = row["localRemainsPrimary"] and int(row["chromeAccepted"] or 0) == 0
    return row


def run_au_us_fx(provider: EbayBrowserSoldCompsProvider) -> dict[str, Any]:
    # Prefer AU-scarce / AU-timeout path: Charizard Base #4 historically times out
    # or yields weak AU evidence while US supplies strong exact comps.
    key_au = _key(
        name="Charizard",
        set_name="Base Set",
        set_code="base1",
        number="4/102",
        lang="en",
        market="AU",
        currency="AUD",
    )
    key_us = _key(
        name="Charizard",
        set_name="Base Set",
        set_code="base1",
        number="4/102",
        lang="en",
        market="US",
        currency="USD",
    )
    au = _search(provider, key=key_au, market="AU", currency="AUD")
    # Brief cooldown before foreign market to reduce challenge/timeout risk.
    time.sleep(8)
    us = _search(provider, key=key_us, market="US", currency="USD")
    # If Charizard US also times out, try Blastoise Base as a second
    # AU-scarce / US-rich path without broadening EU/NZ/JP.
    if int(us.get("acceptedCount") or 0) < 3:
        time.sleep(8)
        key_au2 = _key(
            name="Blastoise",
            set_name="Base Set",
            set_code="base1",
            number="2/102",
            lang="en",
            market="AU",
            currency="AUD",
        )
        key_us2 = _key(
            name="Blastoise",
            set_name="Base Set",
            set_code="base1",
            number="2/102",
            lang="en",
            market="US",
            currency="USD",
        )
        au_alt = _search(provider, key=key_au2, market="AU", currency="AUD")
        time.sleep(8)
        us_alt = _search(provider, key=key_us2, market="US", currency="USD")
        if int(us_alt.get("acceptedCount") or 0) >= int(us.get("acceptedCount") or 0):
            au, us = au_alt, us_alt
            card_label = "Blastoise Base Set 2/102"
        else:
            card_label = "Charizard Base Set 4/102"
    else:
        card_label = "Charizard Base Set 4/102"
    fx_block: dict[str, Any] = {"status": "not_run"}
    disclosure = None
    aud_estimate = None
    try:
        now = datetime.now(timezone.utc)
        rates, freshness, cache = load_production_pair_rates(now=now)
        assert_fx_allows_international_conversion(freshness)
        conversion = resolve_currency_conversion(
            source_currency="USD",
            target_currency="AUD",
            rates=rates,
            rate_source=str(cache.get("source") or "ECB"),
            now=now,
        )
        usd = us.get("recommendedPrice")
        aud_estimate = conversion.amount(usd) if usd is not None else None
        fx_ts = freshness.rate_timestamp
        if hasattr(fx_ts, "isoformat"):
            fx_ts_text = fx_ts.isoformat().replace("+00:00", "Z")
        else:
            fx_ts_text = str(fx_ts)
        fx_block = {
            "status": "ok",
            "sourceCurrency": "USD",
            "displayCurrency": "AUD",
            "fxRate": conversion.rate,
            "fxSource": conversion.rate_source,
            "fxProvider": cache.get("sourceLabel") or cache.get("source") or "European Central Bank",
            "fxTimestamp": fx_ts_text,
            "usdEstimate": usd,
            "audEstimate": aud_estimate,
            "freshness": {
                "health": freshness.health,
                "allowsConversion": freshness.allows_conversion,
                "stale": freshness.stale,
                "source": freshness.source,
            },
        }
        if aud_estimate is not None and usd is not None and int(us.get("acceptedCount") or 0) >= 3:
            disclosure = (
                f"Estimated market value\n"
                f"A${aud_estimate:.2f}\n"
                f"No sufficient Australian sold history was found.\n"
                f"Based on {us['acceptedCount']} recent US eBay sold listings.\n"
                f"US${usd:.2f} ≈ A${aud_estimate:.2f}\n"
                f"International shipping cost unavailable."
            )
    except Exception as exc:
        fx_block = {"status": "error", "error": str(exc)[:300]}

    au_insufficient = (
        int(au.get("acceptedCount") or 0) < 3
        or au.get("recommendedPrice") is None
        or str(au.get("confidence") or "").lower() == "low"
        or au.get("status") == "error"
    )
    return {
        "card": card_label,
        "au": au,
        "us": us,
        "auInsufficient": au_insufficient,
        "fx": fx_block,
        "customerDisclosure": disclosure,
        "pass": bool(
            au_insufficient
            and int(us.get("acceptedCount") or 0) >= 3
            and int(us.get("chromeAccepted") or 0) == 0
            and fx_block.get("status") == "ok"
            and aud_estimate is not None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forensic", action="store_true")
    parser.add_argument("--us-local", action="store_true")
    parser.add_argument("--au-us-fx", action="store_true")
    parser.add_argument("--au-local", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if not any([args.forensic, args.us_local, args.au_us_fx, args.au_local, args.all]):
        parser.error("Pass a mode flag")

    config = EbayBrowserProviderConfig.from_env()
    provider = EbayBrowserSoldCompsProvider(config=config)
    report: dict[str, Any] = {"capturedAtUtc": _utc()}
    try:
        if args.all or args.forensic:
            print("[forensic] US title sample", flush=True)
            report["forensic"] = run_forensic(provider)
        if args.all or args.us_local:
            print("[us-local] Pikachu Base 58/102", flush=True)
            report["usLocal"] = run_us_local(provider)
        if args.all or args.au_local:
            print("[au-local] Iron Valiant 249/182", flush=True)
            report["auLocal"] = run_au_local(provider)
        if args.all or args.au_us_fx:
            print("[au-us-fx] Charizard Base 4/102", flush=True)
            report["auUsFx"] = run_au_us_fx(provider)
    finally:
        provider._close_browser_session()

    report["chromeAcceptedTotal"] = CHROME_ACCEPTED
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"wrote": str(OUT), "keys": list(report.keys()), "chromeAcceptedTotal": CHROME_ACCEPTED}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
