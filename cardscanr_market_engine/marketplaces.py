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
            f"Unsupported marketplace '{normalized_marketplace or marketplace}'. Supported marketplaces: ebay."
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
            "Unsupported ebay market route "
            f"'{normalized_country or '?'}'/'{normalized_currency or '?'}'. "
            f"Supported routes: {supported}. No fallback is applied."
        )
    return config
