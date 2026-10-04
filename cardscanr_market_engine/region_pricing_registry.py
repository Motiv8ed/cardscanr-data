"""Authoritative region definitions for verified-local continuous pricing.

Existing marketplace configuration is the source of truth. This module does not
invent browser endpoints for JP or a fictional ebay.eu host.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .international.market_fallback_policy import NO_NATIVE_SOLD_ROUTE_MARKETS
from .marketplaces import LocalMarketConfig, resolve_marketplace_config


RegionStatus = Literal[
    "CONTINUOUS",
    "CANARY",
    "COOLDOWN",
    "DISABLED",
    "BLOCKED_NEEDS_PROVIDER",
    "HARD_STOP",
    "READY_FOR_BROWSER_CANARY",
]

CARDSCANR_REGIONS = ("AU", "US", "GB", "CA", "JP", "EU")
BROWSER_READY_REGIONS = ("AU", "US", "GB", "CA")
GLOBAL_BROWSER_PRICING_CONCURRENCY = 1
SEARCH_MODE = "RENDERED_UI_X11"
PROVIDER_EBAY_BROWSER = "ebay_browser"


@dataclass(frozen=True)
class RegionPricingDefinition:
    region: str
    currency: str
    canonical_market_key: str
    provider: str
    marketplace_host: str
    homepage: str
    locale: str
    search_mode: str
    sold_capability: bool
    sold_labels: tuple[str, ...]
    currency_parser: str
    availability_namespace: str
    worker_enable_default: bool
    production_ready: bool
    browser_capable: bool
    status: RegionStatus
    reason: str | None = None
    provider_marketplace_id: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "currency": self.currency,
            "canonicalMarketKey": self.canonical_market_key,
            "provider": self.provider,
            "marketplaceHost": self.marketplace_host,
            "homepage": self.homepage,
            "locale": self.locale,
            "searchMode": self.search_mode,
            "soldCapability": self.sold_capability,
            "soldLabels": list(self.sold_labels),
            "currencyParser": self.currency_parser,
            "availabilityNamespace": self.availability_namespace,
            "workerEnableDefault": self.worker_enable_default,
            "productionReady": self.production_ready,
            "browserCapable": self.browser_capable,
            "status": self.status,
            "reason": self.reason,
            "providerMarketplaceId": self.provider_marketplace_id,
            "notes": list(self.notes),
        }


def _from_ebay_config(
    config: LocalMarketConfig,
    *,
    worker_enable_default: bool,
    status: RegionStatus,
    reason: str | None = None,
) -> RegionPricingDefinition:
    host = str(config.provider_domain)
    return RegionPricingDefinition(
        region=config.market_country,
        currency=config.currency,
        canonical_market_key=config.market_country,
        provider=PROVIDER_EBAY_BROWSER,
        marketplace_host=host,
        homepage=f"https://www.{host}/",
        locale=config.search_locale,
        search_mode=SEARCH_MODE,
        sold_capability=True,
        sold_labels=("sold items",),
        currency_parser=config.currency,
        availability_namespace=config.market_country,
        worker_enable_default=worker_enable_default,
        production_ready=True,
        browser_capable=True,
        status=status,
        reason=reason,
        provider_marketplace_id=config.provider_marketplace_id,
        notes=("English Sold-items control identity; X11 activation only.",),
    )


def region_definition(region: str) -> RegionPricingDefinition:
    code = str(region or "").strip().upper()
    if code == "UK":
        code = "GB"
    if code == "JP":
        return RegionPricingDefinition(
            region="JP",
            currency="JPY",
            canonical_market_key="JP",
            provider="NONE",
            marketplace_host="",
            homepage="",
            locale="ja-JP",
            search_mode=SEARCH_MODE,
            sold_capability=False,
            sold_labels=(),
            currency_parser="JPY",
            availability_namespace="JP",
            worker_enable_default=False,
            production_ready=False,
            browser_capable=False,
            status="BLOCKED_NEEDS_PROVIDER",
            reason=(
                "BLOCKED_NEEDS_PROVIDER: a verified-local JP provider has not been "
                "selected. CardScanR has no native JP sold browser route; foreign "
                "eBay estimates are not JP verified-local."
            ),
            notes=(
                "Do not invent ebay.co.jp sold support.",
                "Market and card language remain independent dimensions.",
            ),
        )
    if code == "EU":
        return RegionPricingDefinition(
            region="EU",
            currency="EUR",
            canonical_market_key="EU",
            provider="NONE",
            marketplace_host="",
            homepage="",
            locale="en-EU",
            search_mode=SEARCH_MODE,
            sold_capability=False,
            sold_labels=(),
            currency_parser="EUR",
            availability_namespace="EU",
            worker_enable_default=False,
            production_ready=False,
            browser_capable=False,
            status="BLOCKED_NEEDS_PROVIDER",
            reason=(
                "BLOCKED_NEEDS_PROVIDER: EU is display-only. No ebay.eu host exists "
                "and DE/FR/IT/ES are not silently treated as canonical EU."
            ),
            notes=(
                "Do not treat DE/FR/IT/ES as EU verified-local without an owner "
                "canonical-country decision.",
            ),
        )
    if code in NO_NATIVE_SOLD_ROUTE_MARKETS:
        return RegionPricingDefinition(
            region=code,
            currency="",
            canonical_market_key=code,
            provider="NONE",
            marketplace_host="",
            homepage="",
            locale="",
            search_mode=SEARCH_MODE,
            sold_capability=False,
            sold_labels=(),
            currency_parser="",
            availability_namespace=code,
            worker_enable_default=False,
            production_ready=False,
            browser_capable=False,
            status="BLOCKED_NEEDS_PROVIDER",
            reason=f"{code} is in NO_NATIVE_SOLD_ROUTE_MARKETS",
        )
    currency = {"AU": "AUD", "US": "USD", "GB": "GBP", "CA": "CAD"}.get(code)
    if not currency:
        return RegionPricingDefinition(
            region=code or "UNKNOWN",
            currency="",
            canonical_market_key=code or "UNKNOWN",
            provider="NONE",
            marketplace_host="",
            homepage="",
            locale="",
            search_mode=SEARCH_MODE,
            sold_capability=False,
            sold_labels=(),
            currency_parser="",
            availability_namespace=code or "UNKNOWN",
            worker_enable_default=False,
            production_ready=False,
            browser_capable=False,
            status="BLOCKED_NEEDS_PROVIDER",
            reason="region not in CardScanR verified-local browser registry",
        )
    config = resolve_marketplace_config(
        market_country=code,
        currency=currency,
        marketplace="ebay",
    )
    status: RegionStatus = "CONTINUOUS" if code == "AU" else "READY_FOR_BROWSER_CANARY"
    return _from_ebay_config(
        config,
        worker_enable_default=code == "AU",
        status=status,
    )


def all_region_definitions() -> dict[str, RegionPricingDefinition]:
    return {code: region_definition(code) for code in CARDSCANR_REGIONS}


def browser_ready_region_codes() -> tuple[str, ...]:
    return tuple(
        code
        for code in CARDSCANR_REGIONS
        if region_definition(code).browser_capable and region_definition(code).sold_capability
    )


def is_region_dispatchable(region: str) -> bool:
    """True only for browser-capable sold markets with a real provider."""
    definition = region_definition(region)
    if definition.provider in {"", "NONE"}:
        return False
    return bool(definition.browser_capable and definition.sold_capability and definition.production_ready)


def approved_sold_labels_for_market(market: str) -> frozenset[str]:
    definition = region_definition(market)
    if definition.sold_labels:
        return frozenset(definition.sold_labels)
    return frozenset({"sold items"})
