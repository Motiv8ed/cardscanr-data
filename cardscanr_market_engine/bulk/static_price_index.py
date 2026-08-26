"""Index local static reference price files for bulk refresh."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..config import ROOT
from ..fingerprints import (
    canonical_collector_number,
    collector_numbers_match,
    normalize_collector_number,
    normalize_market_variant,
    normalize_name,
    normalize_text,
)
from .price_semantics import MappingStatus, ReferencePriceObservation
from .set_id_aliases import resolve_static_set_id


CURRENT_PRICE_ROOT = ROOT / "public" / "v1" / "prices" / "current"


def static_language_folder(language: str) -> str:
    lang = normalize_text(language) or "en"
    if lang in {"ja", "japanese"}:
        return "jp"
    return lang


def static_set_file_path(*, game: str, language: str, set_code: str | None) -> Path | None:
    resolved = resolve_static_set_id(set_code, language=language)
    if not resolved:
        return None
    lang = static_language_folder(language)
    game_norm = normalize_text(game) or "pokemon"
    path = CURRENT_PRICE_ROOT / game_norm / lang / f"{resolved}.json"
    return path if path.is_file() else None


_VARIANT_GROUPS: dict[str, set[str]] = {
    "raw": {"raw", "normal", "non_holo", "nonholo"},
    "non_holo": {"raw", "normal", "non_holo", "nonholo"},
    "holo": {"holo", "holographic"},
    "reverse_holo": {"reverse", "reverse_holo", "reverseholo"},
}


def _variant_matches(requested: str, candidate: str) -> bool:
    req = normalize_market_variant(requested)
    cand = normalize_market_variant(candidate)
    if req == cand:
        return True
    return cand in _VARIANT_GROUPS.get(req, {req})


def _pick_best_price_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def _sort_key(row: dict[str, Any]) -> tuple[int, float]:
        source = str(row.get("source") or "").lower()
        source_rank = 0 if "tcgplayer" in source or source == "pokemon_tcg_api" else 1
        price = float(row.get("marketPrice") or 0)
        return (source_rank, -price)

    return sorted(rows, key=_sort_key)[0]


def _resolve_candidate_rows(
    exact_name: list[dict[str, Any]],
    loose_name: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], MappingStatus]:
    if len(exact_name) == 1:
        return exact_name, "exact"
    if len(exact_name) > 1:
        return [_pick_best_price_row(exact_name)], "canonical"
    if len(loose_name) == 1:
        return loose_name, "canonical"
    if len(loose_name) > 1:
        return [_pick_best_price_row(loose_name)], "canonical"
    if len(candidates) == 1:
        return candidates, "canonical"
    if len(candidates) > 1:
        return [_pick_best_price_row(candidates)], "canonical"
    return candidates, "ambiguous"


@dataclass
class _SetPriceIndex:
    set_id: str
    by_collector_variant: dict[tuple[str, str], list[dict[str, Any]]]
    generated_at_utc: str | None


@lru_cache(maxsize=256)
def _load_set_index(path_str: str) -> _SetPriceIndex | None:
    path = Path(path_str)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    prices = payload.get("prices") if isinstance(payload, dict) else None
    if not isinstance(prices, list):
        return None
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in prices:
        if not isinstance(row, dict):
            continue
        collector = normalize_collector_number(row.get("collectorNumber"))
        variant = normalize_market_variant(row.get("variant"))
        if not collector:
            continue
        index.setdefault((collector, variant), []).append(row)
    return _SetPriceIndex(
        set_id=str(payload.get("setId") or path.stem),
        by_collector_variant=index,
        generated_at_utc=str(payload.get("generatedAtUtc") or payload.get("lastSuccessfulPriceUpdateAtUtc") or "") or None,
    )


def lookup_static_reference(
    *,
    game: str,
    language: str,
    set_code: str | None,
    collector_number: str,
    card_name: str,
    normalized_card_name: str,
    variant: str,
) -> ReferencePriceObservation | None:
    path = static_set_file_path(game=game, language=language, set_code=set_code)
    if path is None:
        return None
    index = _load_set_index(str(path))
    if index is None:
        return None
    collector = canonical_collector_number(collector_number)
    target_name = normalize_name(normalized_card_name or card_name)
    req_variant = normalize_market_variant(variant)

    candidates: list[dict[str, Any]] = []
    for (c_num, c_var), rows in index.by_collector_variant.items():
        if not collector_numbers_match(collector_number, c_num):
            continue
        if not _variant_matches(req_variant, c_var):
            continue
        candidates.extend(rows)

    if not candidates:
        return None

    exact_name: list[dict[str, Any]] = []
    loose_name: list[dict[str, Any]] = []
    for row in candidates:
        row_name = normalize_name(row.get("normalizedName") or row.get("name"))
        if row_name == target_name:
            exact_name.append(row)
        elif target_name and (target_name in row_name or row_name in target_name):
            loose_name.append(row)

    chosen, status = _resolve_candidate_rows(exact_name, loose_name, candidates)

    if status == "ambiguous":
        return ReferencePriceObservation(
            provider="static_reference",
            source_market=str(chosen[0].get("market") or chosen[0].get("country") or "reference"),
            source_currency=str(chosen[0].get("sourceCurrency") or chosen[0].get("currency") or "USD").upper(),
            market_price=0.0,
            low_price=None,
            high_price=None,
            confidence="low",
            mapping_status="ambiguous",
            source_record_id=str(chosen[0].get("canonicalId") or ""),
            diagnostics={"candidateCount": len(chosen)},
        )

    row = chosen[0]
    market_price = float(row.get("marketPrice") or 0)
    if market_price <= 0:
        return None
    return ReferencePriceObservation(
        provider="static_reference",
        source_market=str(row.get("market") or row.get("country") or "reference"),
        source_currency=str(row.get("sourceCurrency") or row.get("currency") or "USD").upper(),
        market_price=market_price,
        low_price=_float_or_none(row.get("lowPrice")),
        high_price=_float_or_none(row.get("highPrice")),
        confidence=str(row.get("confidence") or "medium"),
        mapping_status=status,
        source_record_id=str(row.get("canonicalId") or row.get("priceIdentityId") or ""),
        fetched_at_utc=str(row.get("fetchedAtUtc") or index.generated_at_utc or "") or None,
        raw_source=str(row.get("source") or "static_reference"),
        diagnostics={"setFile": str(path), "setId": index.set_id},
    )


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def clear_static_index_cache() -> None:
    _load_set_index.cache_clear()
