from __future__ import annotations

from dataclasses import dataclass, field
import os
from urllib.parse import urlencode

from ..models import ProviderRequest


RAW_EXCLUDE_TERMS = (
    "proxy",
    "custom",
    "digital",
    "code",
    "jumbo",
    "lot",
    "bundle",
    "pack",
    "booster",
    "sealed",
    "psa",
    "cgc",
    "bgs",
    "graded",
)

GRADED_MARKERS = ("graded", "psa", "cgc", "bgs", "sgc", "ace")


@dataclass(frozen=True)
class ProviderSearchQuery:
    query_text: str
    include_terms: tuple[str, ...]
    exclude_terms: tuple[str, ...]
    provider_domain: str
    provider_marketplace_id: str
    search_url: str
    currency: str
    market_country: str
    diagnostics: dict[str, object] = field(default_factory=dict)


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _is_graded_condition(value: object) -> bool:
    text = _clean(value).lower().replace("-", "_")
    return any(marker in text for marker in GRADED_MARKERS)


def _use_negative_terms() -> bool:
    raw = os.getenv("EBAY_QUERY_USE_NEGATIVE_TERMS", "true").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def build_provider_search_query(request: ProviderRequest) -> ProviderSearchQuery:
    key = request.price_key
    include_terms = tuple(
        item
        for item in (
            _clean(key.card_name),
            _clean(key.collector_number),
            _clean(key.set_name or key.set_code),
            "Pokemon card",
        )
        if item
    )
    graded = _is_graded_condition(key.condition) or _is_graded_condition(key.variant)
    exclude_terms = tuple(term for term in RAW_EXCLUDE_TERMS if not (graded and term in GRADED_MARKERS))
    query_terms = list(include_terms)
    if _use_negative_terms():
        query_terms.extend(f"-{term}" for term in exclude_terms)
    query_text = " ".join(query_terms)
    params = {
        "_nkw": query_text,
        "LH_Sold": "1",
        "LH_Complete": "1",
    }
    search_url = f"https://www.{request.provider_domain}/sch/i.html?{urlencode(params)}"
    return ProviderSearchQuery(
        query_text=query_text,
        include_terms=include_terms,
        exclude_terms=exclude_terms,
        provider_domain=request.provider_domain,
        provider_marketplace_id=request.provider_marketplace_id,
        search_url=search_url,
        currency=request.currency.upper(),
        market_country=request.market_country.upper(),
        diagnostics={
            "graded": graded,
            "useNegativeTerms": _use_negative_terms(),
            "marketplace": request.marketplace,
            "searchLocale": request.search_locale,
            "displayName": request.display_name,
        },
    )
