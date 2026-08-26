"""Live TCGdex reference pricing fallback."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..fingerprints import (
    canonical_collector_number,
    collector_numbers_match,
    normalize_name,
    normalize_text,
)
from .price_semantics import MappingStatus, ReferencePriceObservation
from .set_id_aliases import resolve_tcgdex_set_id


def tcgdex_language(language: str) -> str:
    lang = normalize_text(language)
    if lang in {"ja", "jp", "japanese"}:
        return "ja"
    if lang in {"en", "it", "fr", "de", "es", "unknown"}:
        return "en"
    return lang[:2] if lang else "en"


@dataclass
class TcgdexRunCache:
    set_cards: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    card_details: dict[str, dict[str, Any]] = field(default_factory=dict)

    def preload_set(self, *, language: str, set_id: str) -> list[dict[str, Any]]:
        key = (tcgdex_language(language), set_id)
        cached = self.set_cards.get(key)
        if cached is not None:
            return cached
        # TCGdex v2 exposes set cards on GET /sets/{id}, not /sets/{id}/cards.
        set_url = f"https://api.tcgdex.net/v2/{key[0]}/sets/{set_id}"
        cards: list[dict[str, Any]] = []
        try:
            payload = _http_get_json(set_url)
            if isinstance(payload, dict):
                raw_cards = payload.get("cards")
                if isinstance(raw_cards, list):
                    cards = [row for row in raw_cards if isinstance(row, dict)]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            cards = []
        self.set_cards[key] = cards
        return cards

    def preload_for_set_code(self, *, language: str, set_code: str | None) -> list[dict[str, Any]]:
        set_id = resolve_tcgdex_set_id(set_code)
        if not set_id:
            return []
        return self.preload_set(language=language, set_id=set_id)

    def get_card_detail(self, *, language: str, card_id: str) -> dict[str, Any] | None:
        lang = tcgdex_language(language)
        key = f"{lang}:{card_id}"
        if key in self.card_details:
            return self.card_details[key]
        detail_url = f"https://api.tcgdex.net/v2/{lang}/cards/{card_id}"
        try:
            card_data = _http_get_json(detail_url)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            card_data = None
        if not isinstance(card_data, dict):
            return None
        self.card_details[key] = card_data
        return card_data


def _http_get_json(url: str, *, timeout: int = 15) -> Any:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "CardScanR-BulkPricing/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _extract_variant_price(pricing_root: dict[str, Any], variant: str) -> tuple[float | None, str | None]:
    variant_key = normalize_text(variant).replace(" ", "_")
    alias = {
        "raw": "normal",
        "non_holo": "normal",
        "reverse_holo": "reverse",
    }.get(variant_key, variant_key)
    for market_key in ("tcgplayer", "cardmarket"):
        market = pricing_root.get(market_key)
        if not isinstance(market, dict):
            continue
        prices = market.get("prices") if isinstance(market.get("prices"), dict) else market
        if not isinstance(prices, dict):
            continue
        for key in (alias, variant_key, "normal", "holofoil", "reverseHolofoil", "reverse"):
            entry = prices.get(key)
            if not isinstance(entry, dict):
                continue
            market_price = entry.get("marketPrice") or entry.get("avg") or entry.get("low")
            if market_price is None:
                continue
            try:
                value = float(market_price)
            except (TypeError, ValueError):
                continue
            if value > 0:
                currency = "USD" if market_key == "tcgplayer" else "EUR"
                provider = "tcgdex_tcgplayer" if market_key == "tcgplayer" else "tcgdex_cardmarket"
                return value, provider
    return None, None


def lookup_tcgdex_reference(
    *,
    language: str,
    set_code: str | None,
    collector_number: str,
    card_name: str,
    normalized_card_name: str,
    variant: str,
    cache: TcgdexRunCache | None = None,
) -> ReferencePriceObservation | None:
    set_id = resolve_tcgdex_set_id(set_code)
    if not set_id:
        return None
    run_cache = cache or TcgdexRunCache()
    cards = run_cache.preload_set(language=language, set_id=set_id)
    if not cards:
        return None

    target_name = normalize_name(normalized_card_name or card_name)
    strict: list[str] = []
    loose: list[str] = []
    for candidate in cards:
        if not isinstance(candidate, dict):
            continue
        if not collector_numbers_match(collector_number, candidate.get("localId")):
            continue
        card_id = str(candidate.get("id") or "")
        if not card_id:
            continue
        cand_name = normalize_name(candidate.get("name"))
        if cand_name == target_name:
            strict.append(card_id)
        elif target_name and (target_name in cand_name or cand_name in target_name):
            loose.append(card_id)

    if len(strict) == 1:
        card_id = strict[0]
        status: MappingStatus = "exact"
    elif len(strict) > 1:
        return ReferencePriceObservation(
            provider="tcgdex_reference",
            source_market="reference",
            source_currency="USD",
            market_price=0.0,
            low_price=None,
            high_price=None,
            confidence="low",
            mapping_status="ambiguous",
            diagnostics={"candidateIds": strict, "tcgdexSetId": set_id},
        )
    elif len(loose) == 1:
        card_id = loose[0]
        status = "canonical"
    else:
        return None

    card_data = run_cache.get_card_detail(language=language, card_id=card_id)
    if not isinstance(card_data, dict):
        return None
    pricing_root = card_data.get("pricing")
    if not isinstance(pricing_root, dict):
        return None
    market_price, provider = _extract_variant_price(pricing_root, variant)
    if market_price is None or provider is None:
        return None
    currency = "USD" if provider.endswith("tcgplayer") else "EUR"
    return ReferencePriceObservation(
        provider=provider,
        source_market="reference",
        source_currency=currency,
        market_price=market_price,
        low_price=None,
        high_price=None,
        confidence="medium",
        mapping_status=status,
        source_record_id=card_id,
        diagnostics={"tcgdexCardId": card_id, "tcgdexSetId": set_id},
    )
