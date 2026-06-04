from __future__ import annotations

from dataclasses import dataclass, field
import os
from urllib.parse import urlencode

from ..fingerprints import normalize_market_variant
from ..models import ProviderRequest
from .errors import ProviderIdentityUnavailableError
from .identity_guard import ENGLISH_MARKET_IDENTITY_UNAVAILABLE, evaluate_english_market_identity


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
    query_index: int = 0
    query_source: str = "exact"
    diagnostics: dict[str, object] = field(default_factory=dict)


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _is_graded_condition(value: object) -> bool:
    text = _clean(value).lower().replace("-", "_")
    return any(marker in text for marker in GRADED_MARKERS)


def _use_negative_terms() -> bool:
    raw = os.getenv("EBAY_QUERY_USE_NEGATIVE_TERMS", "true").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _quote_query_term(value: str) -> str:
    clean = _clean(value)
    if not clean:
        return ""
    escaped = clean.replace('"', "")
    return f'"{escaped}"'


def _collector_parts(value: object) -> tuple[str, str, bool]:
    full = _clean(value)
    short = full.split("/", 1)[0] if "/" in full else full
    return full, short, bool("/" in full)


def _build_search_url(*, request: ProviderRequest, query_text: str) -> str:
    params = {
        "_nkw": query_text,
        "LH_Sold": "1",
        "LH_Complete": "1",
    }
    return f"https://www.{request.provider_domain}/sch/i.html?{urlencode(params)}"


def _with_negative_terms(include_terms: list[str], exclude_terms: tuple[str, ...]) -> str:
    query_terms = [term for term in include_terms if term]
    if _use_negative_terms():
        query_terms.extend(f"-{term}" for term in exclude_terms)
    return " ".join(query_terms)


def _japanese_origin_hint_applies(request: ProviderRequest, identity_diagnostics: dict[str, object]) -> bool:
    language = str(request.price_key.language or "").strip().lower()
    if language in {"jp", "ja", "japanese"}:
        return True
    if bool(identity_diagnostics.get("english_alias_available")):
        return True
    return bool(identity_diagnostics.get("non_latin_detected"))


def build_provider_search_queries(
    request: ProviderRequest,
    *,
    max_attempts: int | None = None,
) -> list[ProviderSearchQuery]:
    key = request.price_key
    identity_guard = evaluate_english_market_identity(request)
    if identity_guard.blocked:
        raise ProviderIdentityUnavailableError(
            ENGLISH_MARKET_IDENTITY_UNAVAILABLE,
            diagnostics=identity_guard.diagnostics,
        )
    variant = normalize_market_variant(key.variant)
    variant_include_terms = {
        "reverse_holo": ("reverse holo",),
        "holo": ("holo",),
    }.get(variant, ())
    search_card_name = _clean(identity_guard.search_card_name)
    collector_full, collector_short, collector_has_full_number = _collector_parts(key.collector_number)
    set_name = _clean(key.set_name)
    set_code = _clean(key.set_code)

    include_terms = tuple(
        item
        for item in (
            search_card_name,
            collector_full,
            set_name or set_code,
            *variant_include_terms,
            "Pokemon card",
        )
        if item
    )
    include_terms = tuple(dict.fromkeys(include_terms))
    graded = _is_graded_condition(key.condition) or _is_graded_condition(key.variant)
    exclude_terms_list = [term for term in RAW_EXCLUDE_TERMS if not (graded and term in GRADED_MARKERS)]
    if variant == "non_holo":
        exclude_terms_list.extend(("holo", "reverse"))
    elif variant == "holo":
        exclude_terms_list.append("reverse")
    exclude_terms = tuple(dict.fromkeys(exclude_terms_list))

    common_diagnostics: dict[str, object] = {
        "graded": graded,
        "variant": variant,
        "useNegativeTerms": _use_negative_terms(),
        "marketplace": request.marketplace,
        "searchLocale": request.search_locale,
        "displayName": request.display_name,
        "identityGuard": identity_guard.diagnostics,
        "collectorNumberConfidence": "full" if collector_has_full_number else "short_or_unknown",
        "searchStrategy": "ordered_query_ladder",
    }

    attempts: list[tuple[str, list[str], dict[str, object]]] = []
    variant_terms = list(variant_include_terms)
    base_name = _quote_query_term(search_card_name)
    full_number = _quote_query_term(collector_full)
    short_number = _quote_query_term(collector_short)
    readable_set = _quote_query_term(set_name)
    quoted_set_code = _quote_query_term(set_code.upper() if set_code else "")
    pokemon_card = "Pokemon card"

    exact_terms = [base_name, full_number, readable_set or quoted_set_code, *variant_terms, pokemon_card]
    attempts.append(
        (
            "exact",
            exact_terms,
            {
                "usesSetName": bool(readable_set),
                "usesSetCode": bool(not readable_set and quoted_set_code),
                "usesFullCollectorNumber": bool(collector_full),
            },
        )
    )

    without_set_terms = [base_name, full_number, *variant_terms, pokemon_card]
    attempts.append(
        (
            "without_set",
            without_set_terms,
            {
                "usesSetName": False,
                "usesSetCode": False,
                "usesFullCollectorNumber": bool(collector_full),
            },
        )
    )

    if quoted_set_code and short_number:
        attempts.append(
            (
                "set_code_fallback",
                [base_name, quoted_set_code, short_number, *variant_terms, pokemon_card],
                {
                    "usesSetName": False,
                    "usesSetCode": True,
                    "usesFullCollectorNumber": False,
                },
            )
        )

    if readable_set and _japanese_origin_hint_applies(request, identity_guard.diagnostics):
        attempts.append(
            (
                "japanese_origin_hint",
                [base_name, full_number, readable_set, *variant_terms, "Japanese Pokemon card"],
                {
                    "usesSetName": True,
                    "usesSetCode": False,
                    "usesFullCollectorNumber": bool(collector_full),
                    "japaneseOriginHint": True,
                },
            )
        )

    queries: list[ProviderSearchQuery] = []
    seen_texts: set[str] = set()
    for index, (source, terms, diagnostics) in enumerate(attempts):
        query_text = _with_negative_terms([term for term in terms if term], exclude_terms)
        if not query_text or query_text in seen_texts:
            continue
        seen_texts.add(query_text)
        query_diagnostics = {
            **common_diagnostics,
            **diagnostics,
            "queryIndex": len(queries),
            "querySource": source,
        }
        queries.append(
            ProviderSearchQuery(
                query_text=query_text,
                include_terms=include_terms,
                exclude_terms=exclude_terms,
                provider_domain=request.provider_domain,
                provider_marketplace_id=request.provider_marketplace_id,
                search_url=_build_search_url(request=request, query_text=query_text),
                currency=request.currency.upper(),
                market_country=request.market_country.upper(),
                query_index=len(queries),
                query_source=source,
                diagnostics=query_diagnostics,
            )
        )
        if max_attempts is not None and len(queries) >= max_attempts:
            break
    return queries


def build_provider_search_query(request: ProviderRequest) -> ProviderSearchQuery:
    queries = build_provider_search_queries(request, max_attempts=1)
    if not queries:
        raise ProviderIdentityUnavailableError(
            ENGLISH_MARKET_IDENTITY_UNAVAILABLE,
            diagnostics={"marketCountry": request.market_country, "providerMarketplaceId": request.provider_marketplace_id},
        )
    return queries[0]
