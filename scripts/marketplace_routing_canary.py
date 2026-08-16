#!/usr/bin/env python3
"""Marketplace routing canary — no queue drain, no price cache writes.

Proves CardScanR market → eBay hostname/currency URL construction, then
optionally probes live final URLs (auth redirects allowed; wrong-domain
redirects are failures).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.marketplaces import (  # noqa: E402
    browser_supported_market_routes,
    ebay_host_matches_provider_domain,
    normalize_ebay_host,
    resolve_marketplace_config,
)
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest  # noqa: E402
from cardscanr_market_engine.providers.ebay_browser_provider import (  # noqa: E402
    assert_final_url_matches_requested_marketplace,
    is_ebay_authentication_url,
)
from cardscanr_market_engine.providers.errors import (  # noqa: E402
    ProviderAuthenticationRequiredError,
    ProviderMarketplaceMismatchError,
)
from cardscanr_market_engine.providers.query_builder import build_provider_search_queries  # noqa: E402


CANARY_CARD = {
    "game": "pokemon",
    "card_name": "Pikachu",
    "normalized_card_name": "pikachu",
    "set_name": "Scarlet & Violet 151",
    "set_code": "sv3pt5",
    "collector_number": "025/165",
    "language": "en",
    "variant": "non_holo",
    "condition": "raw",
}


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_request(market_country: str, currency: str) -> ProviderRequest:
    config = resolve_marketplace_config(
        market_country=market_country,
        currency=currency,
        marketplace="ebay",
    )
    price_key = MarketPriceKey(
        id=None,
        game=CANARY_CARD["game"],
        card_name=CANARY_CARD["card_name"],
        normalized_card_name=CANARY_CARD["normalized_card_name"],
        set_name=CANARY_CARD["set_name"],
        set_code=CANARY_CARD["set_code"],
        collector_number=CANARY_CARD["collector_number"],
        language=CANARY_CARD["language"],
        variant=CANARY_CARD["variant"],
        condition=CANARY_CARD["condition"],
        market_country=config.market_country.lower(),
        currency=config.currency.lower(),
        fingerprint="|".join(
            [
                CANARY_CARD["game"],
                CANARY_CARD["language"],
                CANARY_CARD["set_code"],
                CANARY_CARD["collector_number"],
                CANARY_CARD["normalized_card_name"],
                CANARY_CARD["variant"],
                CANARY_CARD["condition"],
                config.market_country.lower(),
                config.currency.lower(),
            ]
        ),
    )
    return ProviderRequest(
        price_key=price_key,
        market_country=config.market_country,
        currency=config.currency,
        marketplace=config.marketplace,
        provider_marketplace_id=config.provider_marketplace_id,
        provider_domain=config.provider_domain,
        search_locale=config.search_locale,
        display_name=config.display_name,
        market_config=config,
    )


def offline_route_row(market_country: str, currency: str) -> dict:
    request = build_request(market_country, currency)
    queries = build_provider_search_queries(request, max_attempts=1)
    search_url = queries[0].search_url
    host = normalize_ebay_host(urlparse(search_url).netloc)
    return {
        "requestedMarket": market_country,
        "cardLanguage": CANARY_CARD["language"],
        "urlRequested": search_url,
        "expectedHostname": request.provider_domain,
        "constructedHostname": host,
        "expectedCurrency": currency,
        "providerMarketplaceId": request.provider_marketplace_id,
        "hostnameMatch": host == request.provider_domain,
        "soldFiltersPresent": "LH_Sold=1" in search_url and "LH_Complete=1" in search_url,
        "marketValidationPassed": host == request.provider_domain,
        "pricingAcceptedOrRejected": "offline_url_construction_only",
    }


def live_probe_row(market_country: str, currency: str, *, headless: bool, profile_dir: Path) -> dict:
    row = offline_route_row(market_country, currency)
    row["pricingAcceptedOrRejected"] = "live_probe"
    row["observedCurrency"] = None
    row["resultCount"] = None
    row["finalUrl"] = None
    row["finalHostname"] = None
    row["authRedirect"] = False
    row["marketplaceMismatch"] = False
    row["error"] = None

    from playwright.sync_api import sync_playwright

    request = build_request(market_country, currency)
    search_url = row["urlRequested"]
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                channel="chrome",
                headless=headless,
            )
            try:
                page = context.new_page()
                page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                final_url = page.url
                row["finalUrl"] = final_url
                row["finalHostname"] = normalize_ebay_host(urlparse(final_url).netloc)
                if is_ebay_authentication_url(final_url):
                    row["authRedirect"] = True
                    row["marketValidationPassed"] = False
                    row["pricingAcceptedOrRejected"] = "rejected_auth_required"
                    return row
                try:
                    assert_final_url_matches_requested_marketplace(
                        final_url=final_url,
                        expected_provider_domain=request.provider_domain,
                        requested_market_country=market_country,
                        requested_currency=currency,
                    )
                    row["marketValidationPassed"] = True
                    row["pricingAcceptedOrRejected"] = "accepted_domain_match"
                except ProviderMarketplaceMismatchError as exc:
                    row["marketplaceMismatch"] = True
                    row["marketValidationPassed"] = False
                    row["pricingAcceptedOrRejected"] = "rejected_marketplace_mismatch"
                    row["error"] = str(exc)
                # Best-effort currency sample from page text (no cache write).
                body = ""
                try:
                    body = page.inner_text("body")[:4000]
                except Exception:
                    body = ""
                currency_markers = {
                    "AUD": ("A$", "AU$", "AUD"),
                    "USD": ("US $", "USD", "US$"),
                    "GBP": ("£", "GBP"),
                    "CAD": ("C$", "CAD", "CA$"),
                }
                markers = currency_markers.get(currency, ())
                row["observedCurrency"] = currency if any(marker in body for marker in markers) else None
                try:
                    row["resultCount"] = page.locator("li.s-item, .s-item").count()
                except Exception:
                    row["resultCount"] = None
            finally:
                context.close()
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["marketValidationPassed"] = False
        row["pricingAcceptedOrRejected"] = "probe_error"
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Open each marketplace URL once (no queue/jobs)")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    routes = list(browser_supported_market_routes())
    offline = [offline_route_row(country, currency) for country, currency in routes]
    live: list[dict] = []
    if args.live:
        profile = Path(
            os.getenv("EBAY_BROWSER_USER_DATA_DIR")
            or (ROOT / ".browser_profiles" / "cardscanr")
        )
        for country, currency in routes:
            live.append(
                live_probe_row(
                    country,
                    currency,
                    headless=not args.headed,
                    profile_dir=profile,
                )
            )

    payload = {
        "startedAtUtc": utc_iso(),
        "mode": "live" if args.live else "offline",
        "supportedBrowserRoutes": [{"market": c, "currency": cur} for c, cur in routes],
        "offlineRouting": offline,
        "liveProbes": live,
        "notes": [
            "Does not claim Supabase jobs or write market_price_cache.",
            "Language is intentionally fixed to en; marketplace is independent of card language.",
            "Auth redirects are reported, not bypassed.",
        ],
    }
    out = ROOT / "reports" / "marketplace_routing_canary_latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    print(f"wrote={out}")

    offline_ok = all(row["marketValidationPassed"] and row["soldFiltersPresent"] for row in offline)
    if not offline_ok:
        return 2
    if args.live:
        # Live pass criteria: no wrong-domain acceptance. Auth redirects are not failures
        # for this routing audit (they block pricing, not routing construction).
        if any(row.get("marketplaceMismatch") for row in live):
            return 3
        if any(row.get("pricingAcceptedOrRejected") == "probe_error" for row in live):
            return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
