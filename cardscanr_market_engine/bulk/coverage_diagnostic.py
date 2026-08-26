"""Classify bulk pricing coverage gaps for production price keys."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import MarketPriceKey
from .display_price_policy import decide_display_price
from .price_semantics import ReferencePriceObservation
from .set_id_aliases import is_synthetic_set_code, resolve_pokewallet_set_id, resolve_static_set_id, resolve_tcgdex_set_id
from .static_price_index import lookup_static_reference, static_set_file_path
from .tcgdex_client import TcgdexRunCache, lookup_tcgdex_reference


@dataclass
class CoverageProbeResult:
    price_key_id: str
    set_code: str
    set_name: str
    language: str
    collector_number: str
    card_name: str
    variant: str
    reason: str
    provider: str | None = None
    mapping_status: str | None = None
    mapped: bool = False
    price_available: bool = False
    bulk_usable: bool = False
    quarantined: bool = False
    recommended_action: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _probe_static(key: MarketPriceKey) -> ReferencePriceObservation | None:
    return lookup_static_reference(
        game=key.game,
        language=key.language,
        set_code=key.set_code,
        collector_number=key.collector_number,
        card_name=key.card_name,
        normalized_card_name=key.normalized_card_name,
        variant=key.variant,
    )


def _probe_tcgdex(key: MarketPriceKey, cache: TcgdexRunCache) -> ReferencePriceObservation | None:
    return lookup_tcgdex_reference(
        language=key.language,
        set_code=key.set_code,
        collector_number=key.collector_number,
        card_name=key.card_name,
        normalized_card_name=key.normalized_card_name,
        variant=key.variant,
        cache=cache,
    )


def classify_key(
    key: MarketPriceKey,
    *,
    tcgdx_cache: TcgdexRunCache | None = None,
    prior_cache: dict[str, Any] | None = None,
    converted_price: float | None = None,
) -> CoverageProbeResult:
    cache = tcgdx_cache or TcgdexRunCache()
    base = CoverageProbeResult(
        price_key_id=key.id,
        set_code=str(key.set_code or ""),
        set_name=str(key.set_name or ""),
        language=str(key.language or ""),
        collector_number=str(key.collector_number or ""),
        card_name=str(key.card_name or ""),
        variant=str(key.variant or ""),
        reason="unresolved",
    )

    if is_synthetic_set_code(key.set_code, key.set_name):
        base.reason = "synthetic_test_data"
        base.recommended_action = "exclude_from_production_kpi"
        return base

    static_path = static_set_file_path(game=key.game, language=key.language, set_code=key.set_code)
    static_id = resolve_static_set_id(key.set_code)
    tcgdx_id = resolve_tcgdex_set_id(key.set_code)
    pokewallet_id = resolve_pokewallet_set_id(key.set_code, language=key.language)

    base.diagnostics = {
        "staticSetId": static_id,
        "staticFileExists": static_path is not None,
        "tcgdexSetId": tcgdx_id,
        "pokewalletSetId": pokewallet_id,
    }

    static_obs = _probe_static(key)
    if static_obs is not None:
        base.provider = static_obs.provider
        base.mapping_status = static_obs.mapping_status
        if static_obs.mapping_status == "ambiguous":
            base.reason = "ambiguous_identity"
            base.mapped = True
            base.recommended_action = "resolve_identity_or_route_ebay"
            return base
        if not static_obs.is_usable:
            base.reason = "provider_has_card_but_no_usable_price"
            base.mapped = True
            base.recommended_action = "try_live_provider_or_ebay"
        else:
            base.mapped = True
            base.price_available = True
            base.reason = "static_reference_match"
            if converted_price is not None and prior_cache is not None:
                display = decide_display_price(
                    prior_cache=prior_cache,
                    observation=static_obs,
                    converted_price=converted_price,
                    target_currency=key.currency,
                )
                if display.action in {"pending_verification", "reject_reference"}:
                    base.quarantined = True
                    base.reason = "quarantined_by_safety_policy"
                elif display.action in {"apply_reference", "preserve_verified", "no_change"}:
                    base.bulk_usable = True
            else:
                base.bulk_usable = True
            return base

    if static_path is None:
        if tcgdx_id is None and pokewallet_id is None:
            base.reason = "set_alias_missing"
            base.recommended_action = "add_set_mapping"
            return base
        if static_id and not static_path:
            base.reason = "static_set_file_missing"
            base.recommended_action = "backfill_static_or_use_live_provider"

    tcg_obs = _probe_tcgdex(key, cache)
    if tcg_obs is not None:
        base.provider = tcg_obs.provider
        base.mapping_status = tcg_obs.mapping_status
        if tcg_obs.mapping_status == "ambiguous":
            base.reason = "ambiguous_identity"
            base.mapped = True
            base.recommended_action = "resolve_identity_or_route_ebay"
            return base
        if tcg_obs.is_usable:
            base.mapped = True
            base.price_available = True
            base.reason = "tcgdex_reference_match"
            base.bulk_usable = True
            return base
        base.reason = "provider_has_card_but_no_usable_price"
        base.mapped = True
        return base

    if tcgdx_id and not cache.set_cards.get((key.language[:2], tcgdx_id)):
        # set list empty after preload attempt
        if tcgdx_id:
            cards = cache.preload_set(language=key.language, set_id=tcgdx_id)
            if not cards:
                base.reason = "tcgdex_set_mapping_missing"
                base.recommended_action = "fix_tcgdex_set_alias"
                return base

    lang = (key.language or "").lower()
    if lang in {"ja", "jp"}:
        if pokewallet_id:
            base.reason = "jp_provider_needed"
            base.recommended_action = "use_pokewallet_or_improve_jp_static_match"
            return base
        base.reason = "jp_coverage_gap"
        base.recommended_action = "add_jp_set_mapping"
        return base

    if not tcgdx_id and static_path is None:
        base.reason = "unsupported_market_language"
        base.recommended_action = "ebay_verification_or_new_provider"
        return base

    base.reason = "collector_or_name_mismatch"
    base.recommended_action = "verify_catalogue_identity"
    return base


def aggregate_coverage(results: list[CoverageProbeResult]) -> dict[str, Any]:
    production = [r for r in results if r.reason != "synthetic_test_data"]
    reason_counts: dict[str, int] = {}
    set_gaps: dict[tuple[str, str, str], dict[str, Any]] = {}
    provider_mapped: dict[str, int] = {}
    provider_usable: dict[str, int] = {}

    for row in production:
        reason_counts[row.reason] = reason_counts.get(row.reason, 0) + 1
        if row.mapped and row.provider:
            provider_mapped[row.provider] = provider_mapped.get(row.provider, 0) + 1
            if row.bulk_usable:
                provider_usable[row.provider] = provider_usable.get(row.provider, 0) + 1
        gap_key = (row.set_code, row.language, row.set_name)
        gap = set_gaps.setdefault(
            gap_key,
            {
                "setCode": row.set_code,
                "language": row.language,
                "setName": row.set_name,
                "liveKeys": 0,
                "mapped": 0,
                "usable": 0,
                "unresolved": 0,
                "reasons": {},
            },
        )
        gap["liveKeys"] += 1
        if row.bulk_usable:
            gap["mapped"] += 1
            gap["usable"] += 1
        elif row.mapped:
            gap["mapped"] += 1
        else:
            gap["unresolved"] += 1
        gap["reasons"][row.reason] = gap["reasons"].get(row.reason, 0) + 1

    total = len(production)
    mapped = sum(1 for r in production if r.mapped)
    usable = sum(1 for r in production if r.bulk_usable)
    quarantined = sum(1 for r in production if r.quarantined)
    unresolved = sum(1 for r in production if not r.mapped and not r.quarantined)

    biggest_gaps = sorted(set_gaps.values(), key=lambda item: item["unresolved"], reverse=True)

    return {
        "productionKeys": total,
        "mapped": mapped,
        "mappedPct": round((mapped / total), 4) if total else 0,
        "usable": usable,
        "usablePct": round((usable / total), 4) if total else 0,
        "quarantined": quarantined,
        "unresolved": unresolved,
        "reasonCounts": reason_counts,
        "providerMapped": provider_mapped,
        "providerUsable": provider_usable,
        "biggestGaps": biggest_gaps[:15],
    }
