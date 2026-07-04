from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlencode

from ..fingerprints import normalize_market_variant
from ..models import ProviderRequest
from .errors import ProviderIdentityUnavailableError
from .identity_guard import ENGLISH_MARKET_IDENTITY_UNAVAILABLE, evaluate_english_market_identity


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


def _quote_query_term(value: str) -> str:
    clean = _clean(value)
    if not clean:
        return ""
    escaped = clean.replace('"', "")
    return f'"{escaped}"'


def _unquoted_query_term(value: str) -> str:
    return _clean(value).replace('"', "")


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


def _japanese_origin_hint_applies(request: ProviderRequest, search_card_name: str) -> bool:
    language = str(request.price_key.language or "").strip().lower()
    search_name = _clean(search_card_name)
    return language in {"jp", "ja", "japanese"} and bool(search_name) and all(ord(ch) <= 127 for ch in search_name)


def _language_is_japanese(value: object) -> bool:
    return str(value or "").strip().lower() in {"jp", "ja", "japanese"}


def _original_name_candidates(raw: dict[str, object]) -> tuple[str, ...]:
    candidates: list[object] = []
    for field in (
        "original_card_name",
        "originalCardName",
        "source_name",
        "sourceName",
        "original_name",
        "originalName",
        "name_ja",
        "nameJa",
        "ja_name",
        "jaName",
    ):
        if field in raw:
            candidates.append(raw.get(field))
    aliases = raw.get("aliases")
    if isinstance(aliases, dict):
        for field in ("ja", "jp", "JA", "JP", "original", "source"):
            if aliases.get(field):
                candidates.append(aliases.get(field))
    names: list[str] = []
    for candidate in candidates:
        name = _clean(candidate)
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _variant_phrase(variant: str) -> str:
    return {
        "non_holo": "non holo",
        "reverse_holo": "reverse holo",
        "holo": "holo",
    }.get(variant, "")


def _query_text(terms: list[str]) -> str:
    return " ".join(term for term in terms if term)


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
    variant_phrase = _variant_phrase(variant)
    variant_include_terms = (variant_phrase,) if variant_phrase else ()
    variant_query_mode = {
        "non_holo": "broad_non_holo_filter_later",
        "reverse_holo": "positive_reverse_holo_filter_required",
        "holo": "positive_holo_filter_reverse_later",
    }.get(variant, "broad_variant_unknown_filter_later")
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
            "Pokemon",
        )
        if item
    )
    include_terms = tuple(dict.fromkeys(include_terms))
    graded = _is_graded_condition(key.condition) or _is_graded_condition(key.variant)
    exclude_terms: tuple[str, ...] = ()

    common_diagnostics: dict[str, object] = {
        "graded": graded,
        "variant": variant,
        "variantQueryMode": variant_query_mode,
        "queryPolicy": "simple_discovery_filter_after",
        "appliedNegativeTerms": [],
        "rejectionPolicy": "post_parse_only",
        "negativeTermPolicy": {"searchApplied": [], "reason": "post_parse_only"},
        "marketplace": request.marketplace,
        "searchLocale": request.search_locale,
        "displayName": request.display_name,
        "identityGuard": identity_guard.diagnostics,
        "collectorNumberConfidence": "full" if collector_has_full_number else "short_or_unknown",
        "searchStrategy": "ordered_query_ladder",
        "primaryQueryReason": "simple_human_search_terms",
    }

    attempts: list[tuple[str, list[str], dict[str, object]]] = []
    variant_terms = list(variant_include_terms)
    base_name = _unquoted_query_term(search_card_name)
    full_number = _unquoted_query_term(collector_full)
    short_number = _unquoted_query_term(collector_short)
    readable_set = _unquoted_query_term(set_name)
    set_code_text = _unquoted_query_term(set_code.upper() if set_code else "")
    quoted_base_name = _quote_query_term(search_card_name)
    quoted_full_number = _quote_query_term(collector_full)
    pokemon = "Pokemon"
    pokemon_card = "Pokemon card"
    language_is_jp = _language_is_japanese(key.language)
    jp_set_identity = readable_set or set_code_text
    jp_original_names = _original_name_candidates(key.raw)
    common_diagnostics["canonicalEnglishName"] = search_card_name
    common_diagnostics["originalSourceNames"] = list(jp_original_names)

    if language_is_jp:
        attempts.append(
            (
                "japanese_canonical_set_number",
                [base_name, "Japanese", jp_set_identity, full_number, *variant_terms, pokemon_card],
                {
                    "queryStyle": "unquoted_discovery",
                    "usesSetName": bool(readable_set),
                    "usesSetCode": bool(set_code_text and not readable_set),
                    "usesFullCollectorNumber": bool(collector_full),
                    "usesLanguage": True,
                    "usesVariantTerm": bool(variant_terms),
                    "primaryDiscoveryQuery": True,
                },
            )
        )
        if set_code_text:
            attempts.append(
                (
                    "japanese_canonical_set_code_number",
                    [base_name, "JP", set_code_text, full_number, *variant_terms, pokemon_card],
                    {
                        "queryStyle": "unquoted_discovery",
                        "usesSetName": False,
                        "usesSetCode": True,
                        "usesFullCollectorNumber": bool(collector_full),
                        "usesLanguage": True,
                        "usesVariantTerm": bool(variant_terms),
                    },
                )
            )
        for original_name in jp_original_names[:1]:
            attempts.append(
                (
                    "japanese_original_name_fallback",
                    [_unquoted_query_term(original_name), jp_set_identity, full_number, pokemon_card],
                    {
                        "queryStyle": "unquoted_discovery",
                        "usesSetName": bool(readable_set),
                        "usesSetCode": bool(set_code_text and not readable_set),
                        "usesFullCollectorNumber": bool(collector_full),
                        "usesOriginalSourceName": True,
                        "fallbackQuery": True,
                    },
                )
            )
        if quoted_base_name and quoted_full_number and _japanese_origin_hint_applies(request, search_card_name):
            attempts.append(
                (
                    "quoted_precision_fallback",
                    [quoted_base_name, quoted_full_number, "Japanese", pokemon],
                    {
                        "queryStyle": "quoted_precision",
                        "usesSetName": False,
                        "usesSetCode": False,
                        "usesFullCollectorNumber": bool(collector_full),
                        "usesLanguage": True,
                        "fallbackQuery": True,
                    },
                )
            )
    else:
        attempts.append(
            (
                "broad_number_unquoted",
                [base_name, full_number, pokemon],
                {
                    "queryStyle": "unquoted_discovery",
                    "usesSetName": False,
                    "usesSetCode": False,
                    "usesFullCollectorNumber": bool(collector_full),
                    "primaryDiscoveryQuery": True,
                },
            )
        )
        if variant_terms:
            attempts.append(
                (
                    "variant_unquoted",
                    [base_name, full_number, *variant_terms, pokemon],
                    {
                        "queryStyle": "unquoted_discovery",
                        "usesSetName": False,
                        "usesSetCode": False,
                        "usesFullCollectorNumber": bool(collector_full),
                        "usesVariantTerm": True,
                    },
                )
            )
        if set_code_text and short_number:
            attempts.append(
                (
                    "set_code_unquoted",
                    [base_name, set_code_text, short_number, pokemon],
                    {
                        "queryStyle": "unquoted_discovery",
                        "usesSetName": False,
                        "usesSetCode": True,
                        "usesFullCollectorNumber": False,
                    },
                )
            )
        if quoted_base_name and quoted_full_number:
            attempts.append(
                (
                    "quoted_precision_fallback",
                    [quoted_base_name, quoted_full_number, pokemon],
                    {
                        "queryStyle": "quoted_precision",
                        "usesSetName": False,
                        "usesSetCode": False,
                        "usesFullCollectorNumber": bool(collector_full),
                        "fallbackQuery": True,
                    },
                )
            )

    queries: list[ProviderSearchQuery] = []
    seen_texts: set[str] = set()
    for index, (source, terms, diagnostics) in enumerate(attempts):
        query_text = _query_text([term for term in terms if term])
        if not query_text or query_text in seen_texts:
            continue
        seen_texts.add(query_text)
        query_diagnostics = {
            **common_diagnostics,
            **diagnostics,
            "queryIndex": len(queries),
            "querySource": source,
            "queryStyle": diagnostics.get("queryStyle") or "unquoted_discovery",
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
