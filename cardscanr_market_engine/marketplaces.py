from __future__ import annotations

from dataclasses import dataclass


class UnsupportedMarketError(ValueError):
    """Raised when a requested market/currency/provider-marketplace route is unsupported."""


@dataclass(frozen=True)
class LocalMarketConfig:
    market_country: str
    currency: str
    marketplace: str
    provider_marketplace_id: str
    provider_domain: str
    search_locale: str
    display_name: str


_COUNTRY_ALIAS = {
    "UK": "GB",
}

_EBAY_MARKETS: dict[tuple[str, str], LocalMarketConfig] = {
    ("AU", "AUD"): LocalMarketConfig(
        market_country="AU",
        currency="AUD",
        marketplace="ebay",
        provider_marketplace_id="EBAY_AU",
        provider_domain="ebay.com.au",
        search_locale="en-AU",
        display_name="Australia",
    ),
    ("US", "USD"): LocalMarketConfig(
        market_country="US",
        currency="USD",
        marketplace="ebay",
        provider_marketplace_id="EBAY_US",
        provider_domain="ebay.com",
        search_locale="en-US",
        display_name="United States",
    ),
    ("GB", "GBP"): LocalMarketConfig(
        market_country="GB",
        currency="GBP",
        marketplace="ebay",
        provider_marketplace_id="EBAY_GB",
        provider_domain="ebay.co.uk",
        search_locale="en-GB",
        display_name="United Kingdom",
    ),
    ("CA", "CAD"): LocalMarketConfig(
        market_country="CA",
        currency="CAD",
        marketplace="ebay",
        provider_marketplace_id="EBAY_CA",
        provider_domain="ebay.ca",
        search_locale="en-CA",
        display_name="Canada",
    ),
    ("DE", "EUR"): LocalMarketConfig(
        market_country="DE",
        currency="EUR",
        marketplace="ebay",
        provider_marketplace_id="EBAY_DE",
        provider_domain="ebay.de",
        search_locale="de-DE",
        display_name="Germany",
    ),
    ("FR", "EUR"): LocalMarketConfig(
        market_country="FR",
        currency="EUR",
        marketplace="ebay",
        provider_marketplace_id="EBAY_FR",
        provider_domain="ebay.fr",
        search_locale="fr-FR",
        display_name="France",
    ),
    ("IT", "EUR"): LocalMarketConfig(
        market_country="IT",
        currency="EUR",
        marketplace="ebay",
        provider_marketplace_id="EBAY_IT",
        provider_domain="ebay.it",
        search_locale="it-IT",
        display_name="Italy",
    ),
    ("ES", "EUR"): LocalMarketConfig(
        market_country="ES",
        currency="EUR",
        marketplace="ebay",
        provider_marketplace_id="EBAY_ES",
        provider_domain="ebay.es",
        search_locale="es-ES",
        display_name="Spain",
    ),
}

_EBAY_MARKETS_BY_ID: dict[str, LocalMarketConfig] = {
    config.provider_marketplace_id: config for config in _EBAY_MARKETS.values()
}


def normalize_market_country(value: object) -> str:
    country = str(value or "").strip().upper()
    return _COUNTRY_ALIAS.get(country, country)


def normalize_currency(value: object) -> str:
    return str(value or "").strip().upper()


def normalize_marketplace(value: object) -> str:
    return str(value or "").strip().lower()


def resolve_marketplace_config(
    *,
    market_country: object,
    currency: object,
    marketplace: object,
) -> LocalMarketConfig:
    normalized_marketplace = normalize_marketplace(marketplace)
    if normalized_marketplace != "ebay":
        raise UnsupportedMarketError(
            f"Unsupported marketplace '{normalized_marketplace or marketplace}'. Supported marketplaces: eBay."
        )
    normalized_country = normalize_market_country(market_country)
    normalized_currency = normalize_currency(currency)
    config = _EBAY_MARKETS.get((normalized_country, normalized_currency))
    if config is None:
        supported = ", ".join(
            sorted(
                f"{country}/{currency}"
                for country, currency in _EBAY_MARKETS.keys()
            )
        )
        raise UnsupportedMarketError(
            "Unsupported eBay market route "
            f"'{normalized_country or '?'}'/'{normalized_currency or '?'}'. "
            f"Supported routes: {supported}. No fallback is applied."
        )
    return config


def resolve_ebay_marketplace_id(provider_marketplace_id: object) -> LocalMarketConfig:
    normalized = str(provider_marketplace_id or "").strip().upper()
    config = _EBAY_MARKETS_BY_ID.get(normalized)
    if config is None:
        supported = ", ".join(sorted(_EBAY_MARKETS_BY_ID))
        raise UnsupportedMarketError(
            f"Unsupported eBay marketplace '{normalized or provider_marketplace_id}'. "
            f"Supported marketplaces: {supported}."
        )
    return config


def ebay_marketplace_fallback_order(
    *,
    requested_market_country: object,
    requested_currency: object,
    marketplace: object,
    configured_order: tuple[str, ...],
) -> tuple[LocalMarketConfig, ...]:
    """Return marketplaces to attempt for a pricing job.

    The home market from the canonical price key is always first.
    Additional marketplaces are attempted only when ``configured_order`` is
    non-empty (explicit opt-in). Production default is home-market only so an
    AU job cannot silently accept US/UK/CA comps (or vice versa).
    """
    home = resolve_marketplace_config(
        market_country=requested_market_country,
        currency=requested_currency,
        marketplace=marketplace,
    )
    ordered: list[LocalMarketConfig] = [home]
    seen = {home.provider_marketplace_id}
    for marketplace_id in configured_order:
        config = resolve_ebay_marketplace_id(marketplace_id)
        if config.provider_marketplace_id in seen:
            continue
        ordered.append(config)
        seen.add(config.provider_marketplace_id)
    return tuple(ordered)


def normalize_ebay_host(value: object) -> str:
    host = str(value or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def ebay_host_matches_provider_domain(*, final_url_or_host: object, provider_domain: object) -> bool:
    expected = normalize_ebay_host(provider_domain)
    if not expected:
        return False
    raw = str(final_url_or_host or "").strip()
    if not raw:
        return False
    if "://" in raw:
        from urllib.parse import urlparse

        host = normalize_ebay_host(urlparse(raw).netloc)
    else:
        host = normalize_ebay_host(raw)
    return host == expected


def browser_supported_market_routes() -> tuple[tuple[str, str], ...]:
    """Routes currently accepted by the live ebay_browser provider."""
    return (
        ("AU", "AUD"),
        ("US", "USD"),
        ("GB", "GBP"),
        ("CA", "CAD"),
    )


def is_browser_supported_market(*, market_country: object, currency: object) -> bool:
    route = (normalize_market_country(market_country), normalize_currency(currency))
    return route in set(browser_supported_market_routes())
