#!/usr/bin/env python3
"""Build controlled-test JP current price cache files from Pokewallet."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote

import build_price_cache as cache
from probe_pokewallet import POKEWALLET_ENDPOINTS, card_info, fetch_json, list_results, possible_japanese

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "data" / "pokewallet_jp_price_config.json"
JP_CATALOG_CARDS_DIR = ROOT / "public" / "v1" / "catalog" / "pokemon" / "jp" / "cards"
JP_PRICES_DIR = ROOT / "public" / "v1" / "prices" / "current" / "pokemon" / "jp"
JP_STATUS_PATH = JP_PRICES_DIR / "status.json"
PRICES_STATUS_PATH = ROOT / "public" / "v1" / "prices" / "status.json"
INDEX_PATH = ROOT / "public" / "v1" / "index.json"
DIAG_PATH = ROOT / "public" / "v1" / "diagnostics" / "pokewallet-jp-price-build-latest.json"
API_MANIFEST_PATH = ROOT / "public" / "v1" / "api-manifest.json"
API_NOTES_PATH = ROOT / "public" / "v1" / "api-notes.json"
SCHEMAS_PATH = ROOT / "public" / "v1" / "schemas.json"
SCHEMA_VERSION = "1.0.0"

DEFAULT_PREFERRED_SET_IDS = ["SV10", "SV11B", "SV11W", "SV9", "SV9a", "S12a", "PMCG1", "E1", "E2"]
DEFAULT_SEARCH_STRATEGY = "pokewallet_set_id_plus_card_number"
DEFAULT_FALLBACK_STRATEGIES = ["pokewallet_set_code_plus_card_number", "name_plus_pokewallet_set_code"]


@dataclass(frozen=True)
class TargetCard:
    set_id: str
    set_name: str
    collector_number: str
    name: str
    normalized_name: str
    canonical_base_id: str


@dataclass(frozen=True)
class CardScanRSetInfo:
    set_id: str
    set_name: str
    language: str
    card_count: int


@dataclass(frozen=True)
class PokewalletSetInfo:
    set_id: str
    set_code: str
    name: str
    language: str
    card_count: int | None
    release_date: str | None


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_name_key(value: str) -> str:
    return re.sub(r"[^\w]+", "", normalize_text(value), flags=re.UNICODE)


def normalize_set_code(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


def normalize_collector(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", normalize_text(value)).upper()


def similarity_ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def cleaned_query(text: str) -> str:
    query = re.sub(r"\s+", " ", text or "").strip()
    return re.sub(r"\b(None|null|nil)\b", "", query, flags=re.IGNORECASE).strip()


def safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_set_record(raw: dict[str, Any]) -> PokewalletSetInfo | None:
    set_id = str(raw.get("set_id") or raw.get("id") or "").strip()
    set_code = str(raw.get("set_code") or raw.get("code") or "").strip()
    name = str(raw.get("name") or "").strip()
    language = str(raw.get("language") or raw.get("lang") or "").strip().lower()
    card_count = safe_int(raw.get("card_count"))
    release_date_raw = raw.get("release_date")
    release_date = str(release_date_raw).strip() if release_date_raw else None
    if not set_id and not set_code and not name:
        return None
    return PokewalletSetInfo(
        set_id=set_id,
        set_code=set_code,
        name=name,
        language=language,
        card_count=card_count,
        release_date=release_date,
    )


def is_japanese_like_set(set_info: PokewalletSetInfo) -> bool:
    if set_info.language in {"jp", "ja", "japanese"}:
        return True
    if any(ord(ch) > 127 for ch in set_info.name):
        return True
    hay = normalize_text(f"{set_info.name} {set_info.set_code}")
    jp_signals = ["japanese", "jp", "ja", "sv", "pmcg", "neo", "adv"]
    return any(token in hay for token in jp_signals)


def fetch_pokewallet_sets(
    *,
    api_key: str,
    diagnostics: dict[str, Any],
    request_limit: int,
    max_sets: int,
) -> list[PokewalletSetInfo]:
    sets: list[PokewalletSetInfo] = []
    seen_ids: set[str] = set()
    page = 1
    per_page = 100

    while diagnostics["requestsAttempted"] < request_limit and len(sets) < max_sets * 4:
        url = f"https://api.pokewallet.io/sets?page={page}&limit={per_page}"
        diagnostics["requestsAttempted"] += 1
        try:
            payload = fetch_json(url, api_key=api_key)
            diagnostics["requestsSucceeded"] += 1
        except Exception as exc:  # noqa: BLE001
            diagnostics["requestsFailed"] += 1
            append_sample(
                diagnostics["sampleSkipped"],
                {"reason": "set_fetch_failed", "page": page, "detail": str(exc)},
            )
            break

        raw_items = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("results")
        items = raw_items if isinstance(raw_items, list) else []
        if not items:
            break

        for item in items:
            if not isinstance(item, dict):
                continue
            parsed = parse_set_record(item)
            if parsed is None:
                continue
            key = parsed.set_id or parsed.set_code or parsed.name
            if key in seen_ids:
                continue
            seen_ids.add(key)
            sets.append(parsed)

        if len(items) < per_page:
            break
        page += 1

    return sets


def load_target_cards() -> tuple[list[TargetCard], dict[str, CardScanRSetInfo]]:
    cards: list[TargetCard] = []
    set_counts: dict[str, int] = {}
    set_names: dict[str, str] = {}

    for path in sorted(JP_CATALOG_CARDS_DIR.glob("*.json")):
        payload = load_json(path)
        set_id = str(payload.get("setId") or path.stem).strip()
        set_name = str(payload.get("setName") or set_id).strip()
        set_names[set_id] = set_name

        raw_cards = payload.get("cards")
        if not isinstance(raw_cards, list):
            continue
        set_counts[set_id] = len(raw_cards)

        for card in raw_cards:
            if not isinstance(card, dict):
                continue
            name = str(card.get("name") or card.get("normalizedName") or "").strip()
            collector = str(card.get("collectorNumber") or "").strip()
            if not name or not collector:
                continue
            cards.append(
                TargetCard(
                    set_id=str(card.get("setId") or set_id).strip(),
                    set_name=str(card.get("setName") or set_name).strip(),
                    collector_number=collector,
                    name=name,
                    normalized_name=str(card.get("normalizedName") or name).strip(),
                    canonical_base_id=str(card.get("canonicalBaseId") or "").strip(),
                )
            )

    set_infos: dict[str, CardScanRSetInfo] = {}
    for set_id, set_name in set_names.items():
        set_infos[set_id] = CardScanRSetInfo(
            set_id=set_id,
            set_name=set_name,
            language="jp",
            card_count=set_counts.get(set_id, 0),
        )

    return cards, set_infos


def set_match_score(cardscanr: CardScanRSetInfo, pokewallet: PokewalletSetInfo) -> tuple[float, list[str]]:
    score = 0.0
    signals: list[str] = []

    cs_set_code = normalize_set_code(cardscanr.set_id)
    pw_set_code = normalize_set_code(pokewallet.set_code)
    if cs_set_code and pw_set_code and cs_set_code == pw_set_code:
        score += 0.55
        signals.append("set_code_exact")

    cs_name = normalize_name_key(cardscanr.set_name)
    pw_name = normalize_name_key(pokewallet.name)
    if cs_name and pw_name:
        if cs_name == pw_name:
            score += 0.35
            signals.append("set_name_exact")
        else:
            ratio = similarity_ratio(cs_name, pw_name)
            if ratio >= 0.90:
                score += 0.30
                signals.append("set_name_fuzzy_high")
            elif ratio >= 0.75:
                score += 0.20
                signals.append("set_name_fuzzy_medium")

    if pokewallet.language in {"ja", "jp", "japanese"}:
        score += 0.06
        signals.append("language_jp")

    if pokewallet.card_count is not None and cardscanr.card_count > 0:
        diff = abs(pokewallet.card_count - cardscanr.card_count)
        if diff == 0:
            score += 0.07
            signals.append("card_count_exact")
        elif diff <= 3:
            score += 0.05
            signals.append("card_count_close")
        elif diff <= 10:
            score += 0.03
            signals.append("card_count_near")

    return min(score, 1.0), signals


def build_set_map(
    *,
    cardscanr_sets: dict[str, CardScanRSetInfo],
    pokewallet_sets: list[PokewalletSetInfo],
    preferred_set_ids: list[str],
    max_sets: int,
    diagnostics: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    preferred_rank = {normalize_set_code(set_id): idx for idx, set_id in enumerate(preferred_set_ids)}
    japanese_like_sets = [item for item in pokewallet_sets if is_japanese_like_set(item)]
    candidate_sets = japanese_like_sets or pokewallet_sets

    diagnostics["pokewalletSetsFetched"] = len(pokewallet_sets)
    diagnostics["pokewalletJapaneseLikeSets"] = len(japanese_like_sets)
    diagnostics["pokewalletSetLanguagesSeen"] = sorted({item.language for item in pokewallet_sets if item.language})

    matches: list[tuple[str, dict[str, Any]]] = []
    unmatched_cardscanr: list[str] = []
    matched_pw_keys: set[str] = set()

    ordered_cardscanr = sorted(
        cardscanr_sets.values(),
        key=lambda item: (preferred_rank.get(normalize_set_code(item.set_id), 999), item.set_id),
    )

    for cs_set in ordered_cardscanr:
        best_score = -1.0
        best_pw: PokewalletSetInfo | None = None
        best_signals: list[str] = []

        for pw_set in candidate_sets:
            score, signals = set_match_score(cs_set, pw_set)
            if score > best_score:
                best_score = score
                best_pw = pw_set
                best_signals = signals

        if best_pw is None or best_score < 0.50:
            unmatched_cardscanr.append(cs_set.set_id)
            continue

        match_payload = {
            "cardscanrSetId": cs_set.set_id,
            "cardscanrSetName": cs_set.set_name,
            "pokewalletSetId": best_pw.set_id,
            "pokewalletSetCode": best_pw.set_code,
            "pokewalletSetName": best_pw.name,
            "pokewalletSetLanguage": best_pw.language,
            "pokewalletCardCount": best_pw.card_count,
            "score": round(best_score, 4),
            "signals": best_signals,
        }
        matches.append((cs_set.set_id, match_payload))
        matched_pw_keys.add(best_pw.set_id or best_pw.set_code or best_pw.name)

    matches = sorted(
        matches,
        key=lambda item: (
            preferred_rank.get(normalize_set_code(item[0]), 999),
            -float(item[1].get("score") or 0.0),
            item[0],
        ),
    )
    if len(matches) > max_sets:
        matches = matches[:max_sets]

    set_map = {set_id: payload for set_id, payload in matches}
    diagnostics["setMatchCandidatesBuilt"] = len(set_map)

    for payload in list(set_map.values())[:10]:
        append_sample(
            diagnostics["sampleSetMatches"],
            {
                "cardscanrSetId": payload["cardscanrSetId"],
                "cardscanrSetName": payload["cardscanrSetName"],
                "pokewalletSetId": payload["pokewalletSetId"],
                "pokewalletSetCode": payload["pokewalletSetCode"],
                "pokewalletSetName": payload["pokewalletSetName"],
                "score": payload["score"],
                "signals": payload["signals"],
            },
        )

    for set_id in unmatched_cardscanr[:10]:
        append_sample(diagnostics["sampleUnmatchedCardScanRSets"], {"setId": set_id})

    unmatched_pw = [
        item
        for item in candidate_sets
        if (item.set_id or item.set_code or item.name) not in matched_pw_keys
    ]
    for item in unmatched_pw[:10]:
        append_sample(
            diagnostics["sampleUnmatchedPokewalletSets"],
            {
                "pokewalletSetId": item.set_id,
                "pokewalletSetCode": item.set_code,
                "name": item.name,
                "language": item.language,
            },
        )

    return set_map


def choose_sample_cards(
    cards: list[TargetCard],
    *,
    allowed_set_ids: set[str],
    sample_limit: int,
) -> list[TargetCard]:
    selected: list[TargetCard] = []
    seen: set[tuple[str, str, str]] = set()

    for card in sorted(cards, key=lambda item: (item.set_id, normalize_collector(item.collector_number), normalize_name_key(item.name))):
        if card.set_id not in allowed_set_ids:
            continue
        key = (normalize_set_code(card.set_id), normalize_collector(card.collector_number), normalize_name_key(card.normalized_name))
        if key in seen:
            continue
        seen.add(key)
        selected.append(card)
        if len(selected) >= sample_limit:
            break

    return selected


def build_query_from_strategy(strategy: str, card: TargetCard, set_match: dict[str, Any]) -> str | None:
    pw_set_id = str(set_match.get("pokewalletSetId") or "").strip()
    pw_set_code = str(set_match.get("pokewalletSetCode") or "").strip()
    collector = str(card.collector_number or "").strip()

    if strategy == "pokewallet_set_id_plus_card_number":
        if not pw_set_id or not collector:
            return None
        return cleaned_query(f"{pw_set_id} {collector}")

    if strategy == "pokewallet_set_code_plus_card_number":
        if not pw_set_code or not collector:
            return None
        return cleaned_query(f"{pw_set_code} {collector}")

    if strategy == "name_plus_pokewallet_set_code":
        if not pw_set_code or not card.name:
            return None
        return cleaned_query(f"{card.name} {pw_set_code}")

    return None


def build_query_targets(
    cards: list[TargetCard],
    set_map: dict[str, dict[str, Any]],
    *,
    primary_strategy: str,
    fallback_strategies: list[str],
    max_queries: int,
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    strategies = [primary_strategy] + [item for item in fallback_strategies if item and item != primary_strategy]

    for card in cards:
        set_match = set_map.get(card.set_id)
        if not set_match:
            continue
        for strategy in strategies:
            query = build_query_from_strategy(strategy, card, set_match)
            if not query:
                continue
            key = normalize_text(query)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                {
                    "query": query,
                    "strategy": strategy,
                    "target": {
                        "setId": card.set_id,
                        "setName": card.set_name,
                        "collectorNumber": card.collector_number,
                        "name": card.name,
                        "normalizedName": card.normalized_name,
                        "canonicalBaseId": card.canonical_base_id,
                        "pokewalletSetId": str(set_match.get("pokewalletSetId") or ""),
                        "pokewalletSetCode": str(set_match.get("pokewalletSetCode") or ""),
                        "pokewalletSetName": str(set_match.get("pokewalletSetName") or ""),
                    },
                }
            )
            if len(targets) >= max_queries:
                return targets
    return targets


def provider_snapshot(record: dict[str, Any]) -> dict[str, str]:
    info = card_info(record)
    return {
        "name": str(info.get("name") or info.get("clean_name") or record.get("name") or ""),
        "set_name": str(info.get("set_name") or record.get("setName") or record.get("set_name") or ""),
        "set_code": str(info.get("set_code") or record.get("setCode") or record.get("set_code") or ""),
        "set_id": str(info.get("set_id") or record.get("set_id") or ""),
        "number": str(info.get("card_number") or record.get("number") or record.get("card_number") or ""),
        "language": str(record.get("language") or info.get("language") or ""),
    }


def score_result(record: dict[str, Any], target: dict[str, Any]) -> tuple[float, list[str], bool]:
    provider = provider_snapshot(record)
    score = 0.0
    signals: list[str] = []

    expected_set_id = normalize_set_code(str(target.get("pokewalletSetId") or ""))
    provider_set_id = normalize_set_code(provider.get("set_id", ""))
    if expected_set_id and provider_set_id and expected_set_id == provider_set_id:
        score += 0.45
        signals.append("pokewallet_set_id_exact")

    expected_set_code = normalize_set_code(str(target.get("pokewalletSetCode") or ""))
    provider_set_code = normalize_set_code(provider.get("set_code", ""))
    if expected_set_code and provider_set_code and expected_set_code == provider_set_code:
        score += 0.20
        signals.append("pokewallet_set_code_exact")

    expected_collector = normalize_collector(str(target.get("collectorNumber") or ""))
    provider_collector = normalize_collector(provider.get("number", ""))
    collector_match = bool(expected_collector and provider_collector and expected_collector == provider_collector)
    if collector_match:
        score += 0.30
        signals.append("collector_exact")

    target_name = normalize_name_key(str(target.get("normalizedName") or target.get("name") or ""))
    provider_name = normalize_name_key(provider.get("name", ""))
    if target_name and provider_name:
        if target_name == provider_name:
            score += 0.15
            signals.append("name_exact")
        else:
            ratio = similarity_ratio(target_name, provider_name)
            if ratio >= 0.90:
                score += 0.12
                signals.append("name_fuzzy_high")
            elif ratio >= 0.75:
                score += 0.08
                signals.append("name_fuzzy_medium")

    language_value = provider.get("language", "").lower()
    if language_value in {"ja", "jp", "japanese"} or possible_japanese(record):
        score += 0.07
        signals.append("language_jp")

    return min(score, 1.0), signals, collector_match


def extract_tcgplayer_prices(record: dict[str, Any]) -> tuple[str, float | None, float | None, float | None] | None:
    source = record.get("tcgplayer")
    if not isinstance(source, dict):
        return None

    price_objects: list[dict[str, Any]] = []
    prices = source.get("prices")
    if isinstance(prices, list):
        for item in prices:
            if isinstance(item, dict):
                price_objects.append(item)
    elif isinstance(prices, dict):
        for item in prices.values():
            if isinstance(item, dict):
                price_objects.append(item)

    for item in price_objects:
        market = to_float(item.get("market_price") if item.get("market_price") is not None else item.get("market"))
        low = to_float(item.get("low_price") if item.get("low_price") is not None else item.get("low"))
        high = to_float(item.get("high_price") if item.get("high_price") is not None else item.get("high"))
        if market is not None or low is not None or high is not None:
            return "USD", market, low, high

    return None


def extract_cardmarket_prices(record: dict[str, Any]) -> tuple[str, float | None, float | None, float | None] | None:
    source = record.get("cardmarket")
    if not isinstance(source, dict):
        return None

    price_objects: list[dict[str, Any]] = []
    prices = source.get("prices")
    if isinstance(prices, list):
        for item in prices:
            if isinstance(item, dict):
                price_objects.append(item)
    elif isinstance(prices, dict):
        for item in prices.values():
            if isinstance(item, dict):
                price_objects.append(item)

    for item in price_objects:
        market = to_float(item.get("avg") if item.get("avg") is not None else item.get("trend"))
        low = to_float(item.get("low"))
        if market is not None or low is not None:
            return "EUR", market, low, None

    return None


def extract_price(record: dict[str, Any]) -> tuple[str, float | None, float | None, float | None] | None:
    tcgplayer = extract_tcgplayer_prices(record)
    if tcgplayer is not None:
        return tcgplayer
    cardmarket = extract_cardmarket_prices(record)
    if cardmarket is not None:
        return cardmarket
    return None


def canonical_id_for(target: dict[str, Any], variant: str = "normal", condition: str = "near_mint") -> str:
    set_id = str(target.get("setId") or "")
    collector = str(target.get("collectorNumber") or "")
    normalized_name = str(target.get("normalizedName") or target.get("name") or "")
    return f"pokemon|jp|{set_id}|{collector}|{normalized_name}|{variant}|{condition}"


def build_record(
    *,
    record: dict[str, Any],
    target: dict[str, Any],
    confidence: float,
    signals: list[str],
    fetched_at_utc: str,
    currency: str,
    market: float | None,
    low: float | None,
    high: float | None,
) -> dict[str, Any]:
    return {
        "canonicalId": canonical_id_for(target),
        "setId": str(target.get("setId") or ""),
        "collectorNumber": str(target.get("collectorNumber") or ""),
        "normalizedName": str(target.get("normalizedName") or target.get("name") or ""),
        "variant": "normal",
        "condition": "near_mint",
        "currency": currency,
        "marketPrice": market,
        "lowPrice": low,
        "highPrice": high,
        "source": "pokewallet",
        "fetchedAtUtc": fetched_at_utc,
        "nextExpectedPriceUpdateAtUtc": None,
        "staleness": {
            "status": "fresh",
            "ageSeconds": 0,
            "freshForSeconds": 86400,
            "staleAfterSeconds": 172800,
        },
        "providerIds": {
            "pokewalletId": str(record.get("id") or ""),
        },
        "matchConfidence": round(confidence, 4),
        "matchSignals": signals,
    }


def append_sample(container: list[dict[str, Any]], item: dict[str, Any], limit: int = 12) -> None:
    if len(container) < limit:
        container.append(item)


def score_bucket(value: float) -> str:
    if value >= 0.90:
        return "0.90-1.00"
    if value >= 0.80:
        return "0.80-0.89"
    if value >= 0.70:
        return "0.70-0.79"
    if value >= 0.60:
        return "0.60-0.69"
    return "0.00-0.59"


def build_diagnostics_base(ts: str, mode: str, api_key_present: bool) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "provider": "pokewallet",
        "mode": mode,
        "apiKeyPresent": api_key_present,
        "requestsAttempted": 0,
        "requestsSucceeded": 0,
        "requestsFailed": 0,
        "searchTargetsTested": [],
        "resultsFound": 0,
        "possibleJapaneseResults": 0,
        "confidentMatches": 0,
        "lowConfidenceMatches": 0,
        "unmappedResults": 0,
        "priceRecordsWritten": 0,
        "priceFilesWritten": 0,
        "currenciesSeen": [],
        "catalogueCardsLoaded": 0,
        "catalogueSampleTargetsBuilt": 0,
        "catalogueSearchQueriesBuilt": 0,
        "cataloguePreferredSetIdsUsed": [],
        "pokewalletSetsFetched": 0,
        "pokewalletJapaneseLikeSets": 0,
        "pokewalletSetLanguagesSeen": [],
        "setMatchCandidatesBuilt": 0,
        "matchScoreDistribution": {
            "0.90-1.00": 0,
            "0.80-0.89": 0,
            "0.70-0.79": 0,
            "0.60-0.69": 0,
            "0.00-0.59": 0,
        },
        "skippedNoPrice": 0,
        "skippedLowConfidence": 0,
        "skippedNoCanonicalMatch": 0,
        "skippedNoCurrency": 0,
        "sampleSetMatches": [],
        "sampleUnmatchedCardScanRSets": [],
        "sampleUnmatchedPokewalletSets": [],
        "sampleSearchTargets": [],
        "sampleMatches": [],
        "sampleSkipped": [],
        "recommendation": "",
    }


def write_api_docs_updates(ts: str) -> None:
    api_manifest = load_json(API_MANIFEST_PATH)
    api_notes = load_json(API_NOTES_PATH)
    schemas = load_json(SCHEMAS_PATH)

    api_manifest["generatedAtUtc"] = ts
    manifest_notes = api_manifest.get("notes")
    if not isinstance(manifest_notes, list):
        manifest_notes = []
    required_manifest_notes = [
        "JP current prices may be present as partial controlled-test coverage sourced from Pokewallet.",
        "Provider currency is passed through as-is and is not converted.",
        "JP nextExpectedPriceUpdateAtUtc may be null until regular JP refresh scheduling exists.",
    ]
    for note in required_manifest_notes:
        if note not in manifest_notes:
            manifest_notes.append(note)
    api_manifest["notes"] = manifest_notes

    endpoints = api_manifest.get("endpoints")
    if not isinstance(endpoints, list):
        endpoints = []
    if not any(isinstance(item, dict) and item.get("id") == "diagnostics_pokewallet_jp_price_build" for item in endpoints):
        endpoints.append(
            {
                "id": "diagnostics_pokewallet_jp_price_build",
                "method": "GET",
                "path": "/diagnostics/pokewallet-jp-price-build-latest.json",
                "description": "Controlled Pokewallet JP current price build diagnostics",
                "authRequired": False,
                "cacheable": True,
            }
        )
    api_manifest["endpoints"] = endpoints

    api_notes["generatedAtUtc"] = ts
    notes = api_notes.get("notes")
    if not isinstance(notes, list):
        notes = []
    required_notes = [
        "JP current prices may be partial and sourced from a controlled Pokewallet test builder.",
        "App should display JP price data only when a matching JP record exists.",
        "If JP record is missing, show Japanese price not available yet.",
        "Provider currency is not converted; app should display provider currency as-is.",
        "JP nextExpectedPriceUpdateAtUtc may be null until regular JP scheduling exists.",
    ]
    for note in required_notes:
        if note not in notes:
            notes.append(note)
    api_notes["notes"] = notes

    schemas["generatedAtUtc"] = ts
    schema_map = schemas.get("schemas")
    if not isinstance(schema_map, dict):
        schema_map = {}
    schema_map["pokewallet_jp_price_build_diagnostics"] = {
        "requiredFields": [
            "schemaVersion",
            "generatedAtUtc",
            "provider",
            "mode",
            "apiKeyPresent",
            "requestsAttempted",
            "requestsSucceeded",
            "requestsFailed",
            "resultsFound",
            "possibleJapaneseResults",
            "confidentMatches",
            "lowConfidenceMatches",
            "unmappedResults",
            "priceRecordsWritten",
            "priceFilesWritten",
            "catalogueCardsLoaded",
            "catalogueSampleTargetsBuilt",
            "catalogueSearchQueriesBuilt",
            "cataloguePreferredSetIdsUsed",
            "pokewalletSetsFetched",
            "pokewalletJapaneseLikeSets",
            "pokewalletSetLanguagesSeen",
            "setMatchCandidatesBuilt",
            "matchScoreDistribution",
            "skippedNoPrice",
            "skippedLowConfidence",
            "skippedNoCanonicalMatch",
            "skippedNoCurrency",
            "sampleSetMatches",
            "sampleUnmatchedCardScanRSets",
            "sampleUnmatchedPokewalletSets",
            "sampleSearchTargets",
            "sampleMatches",
            "sampleSkipped",
            "recommendation",
        ],
        "notes": [
            "Controlled Pokewallet JP price build diagnostics without secrets or raw payload dumps.",
            "Includes set-map probing metrics and confidence-gated write outcomes.",
        ],
    }
    schemas["schemas"] = schema_map

    cache.write_json(API_MANIFEST_PATH, api_manifest)
    cache.write_json(API_NOTES_PATH, api_notes)
    cache.write_json(SCHEMAS_PATH, schemas)


def collect_jp_price_files() -> list[tuple[str, str, Path]]:
    files: list[tuple[str, str, Path]] = []
    if not JP_PRICES_DIR.exists():
        return files
    for path in sorted(JP_PRICES_DIR.glob("*.json")):
        if path.name == "status.json":
            continue
        payload = load_json(path)
        files.append((str(payload.get("setId") or path.stem), str(payload.get("setName") or path.stem), path))
    return files


def update_index(*, ts: str, jp_files: list[tuple[str, str, Path]]) -> None:
    index = load_json(INDEX_PATH)
    datasets = index.get("datasets")
    if not isinstance(datasets, list):
        datasets = []

    by_id: dict[str, dict[str, Any]] = {}
    for dataset in datasets:
        if isinstance(dataset, dict) and dataset.get("id"):
            by_id[str(dataset["id"])] = dataset

    by_id["prices_status"] = cache.build_index_dataset_entry(
        dataset_id="prices_status",
        file_path=PRICES_STATUS_PATH,
        dataset_type="price_status",
        description="CardScanR app-facing UTC price freshness/status summary",
        ts=ts,
        ttl_seconds=cache.DEFAULT_CACHE_TTL_SECONDS,
        game="pokemon",
    )
    by_id["prices_current_pokemon_jp_status"] = cache.build_index_dataset_entry(
        dataset_id="prices_current_pokemon_jp_status",
        file_path=JP_STATUS_PATH,
        dataset_type="price_current_status",
        description="CardScanR app-facing UTC price freshness/status for Pokemon JP",
        ts=ts,
        ttl_seconds=cache.DEFAULT_CACHE_TTL_SECONDS,
        game="pokemon",
        language="jp",
    )

    for set_id, set_name, path in jp_files:
        by_id[f"prices_current_pokemon_jp_{set_id}"] = cache.build_index_dataset_entry(
            dataset_id=f"prices_current_pokemon_jp_{set_id}",
            file_path=path,
            dataset_type="price_current",
            description=f"Pokemon TCG JP controlled Pokewallet current prices for {set_name}",
            ts=ts,
            ttl_seconds=cache.PRICE_CACHE_TTL_SECONDS,
            game="pokemon",
            language="jp",
        )

    by_id["diagnostics_pokewallet_jp_price_build"] = cache.build_index_dataset_entry(
        dataset_id="diagnostics_pokewallet_jp_price_build",
        file_path=DIAG_PATH,
        dataset_type="diagnostics",
        description="Controlled Pokewallet JP current price build diagnostics",
        ts=ts,
        ttl_seconds=cache.DIAGNOSTICS_CACHE_TTL_SECONDS,
    )

    by_id["api_manifest"] = cache.build_index_dataset_entry(
        dataset_id="api_manifest",
        file_path=API_MANIFEST_PATH,
        dataset_type="api_manifest",
        description="CardScanR internal data API manifest",
        ts=ts,
        ttl_seconds=cache.DEFAULT_CACHE_TTL_SECONDS,
    )
    by_id["api_notes"] = cache.build_index_dataset_entry(
        dataset_id="api_notes",
        file_path=API_NOTES_PATH,
        dataset_type="api_notes",
        description="CardScanR internal app data notes",
        ts=ts,
        ttl_seconds=cache.DEFAULT_CACHE_TTL_SECONDS,
    )
    by_id["schemas"] = cache.build_index_dataset_entry(
        dataset_id="schemas",
        file_path=SCHEMAS_PATH,
        dataset_type="schemas",
        description="CardScanR cache schema documentation",
        ts=ts,
        ttl_seconds=cache.DEFAULT_CACHE_TTL_SECONDS,
    )

    index["generatedAtUtc"] = ts
    index["datasets"] = sorted(by_id.values(), key=lambda item: str(item.get("id")))
    cache.write_json(INDEX_PATH, index)


def update_status_files(*, ts: str, jp_files: list[tuple[str, str, Path]], price_records_written: int, currencies_seen: list[str], had_new_records: bool) -> None:
    prices_status = load_json(PRICES_STATUS_PATH)
    if not isinstance(prices_status.get("languages"), dict):
        prices_status["languages"] = {}

    if had_new_records and jp_files:
        status = "partial"
        source_currency = currencies_seen[0] if len(currencies_seen) == 1 else "mixed"
        staleness_status = "fresh"
        age_seconds = 0
        notes = [
            "Controlled Pokewallet JP current price test with partial set coverage.",
            "Provider currency is passed through as-is and is not converted.",
            "JP nextExpectedPriceUpdateAtUtc is null until regular JP scheduling exists.",
        ]
        last_success = ts
    else:
        status = "not_available"
        source_currency = None
        staleness_status = "unavailable"
        age_seconds = None
        notes = [
            "Controlled Pokewallet JP current price test produced no confident priced matches.",
            "Japanese catalogue exists but JP current prices remain unavailable.",
            "Provider currency is passed through as-is and is not converted.",
        ]
        last_success = None

    prices_status["generatedAtUtc"] = ts
    prices_status["languages"]["jp"] = {
        "game": "pokemon",
        "language": "jp",
        "status": status,
        "currentPriceFilesAvailable": bool(had_new_records and jp_files),
        "currentPriceSetFileCount": len(jp_files) if had_new_records else 0,
        "currentPriceRecordCount": int(price_records_written),
        "lastSuccessfulPriceUpdateAtUtc": last_success,
        "nextExpectedPriceUpdateAtUtc": None,
        "staleness": {
            "status": staleness_status,
            "ageSeconds": age_seconds,
            "freshForSeconds": 86400,
            "staleAfterSeconds": 172800,
        },
        "sourceSummary": {
            "primarySource": "pokewallet",
            "currency": source_currency,
            "isLivePricing": False,
        },
        "notes": notes,
    }
    cache.write_json(PRICES_STATUS_PATH, prices_status)

    jp_status = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "game": "pokemon",
        "language": "jp",
        "status": status,
        "currentPriceFilesAvailable": bool(had_new_records and jp_files),
        "currentPriceSetFileCount": len(jp_files) if had_new_records else 0,
        "currentPriceRecordCount": int(price_records_written),
        "lastSuccessfulPriceUpdateAtUtc": last_success,
        "lastSuccessfulPushAtUtc": None,
        "lastBatchSetIds": [set_id for set_id, _set_name, _path in jp_files] if had_new_records else [],
        "lastBatchSize": len(jp_files) if had_new_records else 0,
        "lastBatchStartedAtUtc": ts if had_new_records else None,
        "lastBatchFinishedAtUtc": ts if had_new_records else None,
        "lastBatchDurationSeconds": 0 if had_new_records else None,
        "nextExpectedPriceUpdateAtUtc": None,
        "expectedUpdateIntervalMinutes": None,
        "fullRotationEstimatedHours": None,
        "currency": source_currency,
        "isLivePricing": False,
        "staleness": {
            "status": staleness_status,
            "ageSeconds": age_seconds,
            "freshForSeconds": 86400,
            "staleAfterSeconds": 172800,
        },
        "notes": notes,
    }
    cache.write_json(JP_STATUS_PATH, jp_status)


def main() -> int:
    ts = now_utc()
    config = load_json(CONFIG_PATH)

    api_key = os.getenv(str(config.get("apiKeyEnv") or "POKEWALLET_API_KEY"), "").strip()
    max_requests = max(1, int(config.get("maxRequestsPerRun") or 40))
    sleep_seconds = max(0.0, float(config.get("requestSleepSeconds") or 0.35))
    confidence_threshold = float(config.get("confidenceThreshold") or 0.82)
    mode = str(config.get("mode") or "controlled_test")

    diagnostics = build_diagnostics_base(ts, mode, bool(api_key))

    if not bool(config.get("enabled", True)):
        diagnostics["recommendation"] = "Pokewallet JP controlled test is disabled in data/pokewallet_jp_price_config.json."
        cache.write_json(DIAG_PATH, diagnostics)
        update_status_files(ts=ts, jp_files=[], price_records_written=0, currencies_seen=[], had_new_records=False)
        write_api_docs_updates(ts)
        update_index(ts=ts, jp_files=collect_jp_price_files())
        print("Controlled test disabled; wrote diagnostics and status metadata only.")
        return 0

    if not api_key:
        diagnostics["recommendation"] = "POKEWALLET_API_KEY is not set. Real JP controlled test could not run; no JP price records were written."
        cache.write_json(DIAG_PATH, diagnostics)
        update_status_files(ts=ts, jp_files=[], price_records_written=0, currencies_seen=[], had_new_records=False)
        write_api_docs_updates(ts)
        update_index(ts=ts, jp_files=collect_jp_price_files())
        print("POKEWALLET_API_KEY missing; wrote diagnostics and status metadata only.")
        return 0

    all_cards, cardscanr_sets = load_target_cards()
    diagnostics["catalogueCardsLoaded"] = len(all_cards)

    preferred_set_ids = [str(item) for item in (config.get("cataloguePreferredSetIds") or DEFAULT_PREFERRED_SET_IDS)]
    sample_limit = max(1, int(config.get("catalogueSampleLimit") or 25))

    use_set_map = bool(config.get("usePokewalletSetMap", True))
    set_map_max_sets = max(1, int(config.get("setMapMaxSets") or 25))
    search_strategy = str(config.get("searchStrategy") or DEFAULT_SEARCH_STRATEGY)
    fallback_strategies = [str(item) for item in (config.get("fallbackSearchStrategies") or DEFAULT_FALLBACK_STRATEGIES)]

    set_map: dict[str, dict[str, Any]] = {}
    if use_set_map:
        pokewallet_sets = fetch_pokewallet_sets(
            api_key=api_key,
            diagnostics=diagnostics,
            request_limit=max_requests,
            max_sets=set_map_max_sets,
        )
        set_map = build_set_map(
            cardscanr_sets=cardscanr_sets,
            pokewallet_sets=pokewallet_sets,
            preferred_set_ids=preferred_set_ids,
            max_sets=set_map_max_sets,
            diagnostics=diagnostics,
        )

    diagnostics["cataloguePreferredSetIdsUsed"] = [set_id for set_id in preferred_set_ids if set_id in set_map]

    selected_cards = choose_sample_cards(
        all_cards,
        allowed_set_ids=set(set_map.keys()),
        sample_limit=sample_limit,
    )
    diagnostics["catalogueSampleTargetsBuilt"] = len(selected_cards)

    query_budget = max(0, max_requests - diagnostics["requestsAttempted"])
    query_targets = build_query_targets(
        selected_cards,
        set_map,
        primary_strategy=search_strategy,
        fallback_strategies=fallback_strategies,
        max_queries=query_budget,
    )
    diagnostics["catalogueSearchQueriesBuilt"] = len(query_targets)

    for item in query_targets[:12]:
        append_sample(
            diagnostics["sampleSearchTargets"],
            {
                "query": item.get("query"),
                "strategy": item.get("strategy"),
                "setId": item.get("target", {}).get("setId"),
                "collectorNumber": item.get("target", {}).get("collectorNumber"),
                "name": item.get("target", {}).get("name"),
                "pokewalletSetId": item.get("target", {}).get("pokewalletSetId"),
            },
        )

    seen_canonical: dict[str, dict[str, Any]] = {}
    currencies_seen: set[str] = set()
    currency_by_set: dict[str, str] = {}

    for item in query_targets:
        if diagnostics["requestsAttempted"] >= max_requests:
            break

        query = cleaned_query(str(item.get("query") or ""))
        if not query:
            continue

        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        diagnostics["searchTargetsTested"].append(query)

        url = f"https://api.pokewallet.io{POKEWALLET_ENDPOINTS['search']['path']}?q={quote(query)}&page=1&limit=8"
        diagnostics["requestsAttempted"] += 1
        try:
            payload = fetch_json(url, api_key=api_key)
            diagnostics["requestsSucceeded"] += 1
        except Exception as exc:  # noqa: BLE001
            diagnostics["requestsFailed"] += 1
            append_sample(
                diagnostics["sampleSkipped"],
                {"query": query, "strategy": item.get("strategy"), "reason": "request_failed", "detail": str(exc)},
            )
            if sleep_seconds:
                time.sleep(sleep_seconds)
            continue

        results = list_results(payload)
        diagnostics["resultsFound"] += len(results)

        for record in results:
            if possible_japanese(record):
                diagnostics["possibleJapaneseResults"] += 1

            score, signals, collector_match = score_result(record, target)
            diagnostics["matchScoreDistribution"][score_bucket(score)] += 1

            if not collector_match:
                diagnostics["skippedNoCanonicalMatch"] += 1
                diagnostics["unmappedResults"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {"query": query, "strategy": item.get("strategy"), "providerId": str(record.get("id") or ""), "reason": "collector_number_mismatch"},
                )
                continue

            price_data = extract_price(record)
            if price_data is None:
                diagnostics["skippedNoPrice"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {"query": query, "strategy": item.get("strategy"), "providerId": str(record.get("id") or ""), "reason": "no_useful_price"},
                )
                continue
            currency, market, low, high = price_data

            if score < confidence_threshold:
                diagnostics["lowConfidenceMatches"] += 1
                diagnostics["skippedLowConfidence"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {
                        "query": query,
                        "strategy": item.get("strategy"),
                        "providerId": str(record.get("id") or ""),
                        "reason": "low_confidence",
                        "confidence": round(score, 4),
                        "signals": signals,
                    },
                )
                continue

            set_id = str(target.get("setId") or "")
            if not set_id:
                diagnostics["skippedNoCanonicalMatch"] += 1
                diagnostics["unmappedResults"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {"query": query, "strategy": item.get("strategy"), "providerId": str(record.get("id") or ""), "reason": "missing_target_set"},
                )
                continue

            if set_id in currency_by_set and currency_by_set[set_id] != currency:
                diagnostics["skippedNoCurrency"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {
                        "query": query,
                        "strategy": item.get("strategy"),
                        "providerId": str(record.get("id") or ""),
                        "reason": "set_currency_mismatch",
                        "setId": set_id,
                    },
                )
                continue

            diagnostics["confidentMatches"] += 1
            currencies_seen.add(currency)
            currency_by_set[set_id] = currency

            payload_record = build_record(
                record=record,
                target=target,
                confidence=score,
                signals=signals,
                fetched_at_utc=ts,
                currency=currency,
                market=market,
                low=low,
                high=high,
            )
            canonical_id = payload_record["canonicalId"]
            existing = seen_canonical.get(canonical_id)
            if existing and float(existing.get("matchConfidence") or 0.0) >= payload_record["matchConfidence"]:
                continue

            seen_canonical[canonical_id] = payload_record
            append_sample(
                diagnostics["sampleMatches"],
                {
                    "providerId": str(record.get("id") or ""),
                    "setId": set_id,
                    "collectorNumber": payload_record["collectorNumber"],
                    "currency": currency,
                    "confidence": payload_record["matchConfidence"],
                    "signals": signals,
                },
            )

        if sleep_seconds:
            time.sleep(sleep_seconds)

    records_by_set: dict[str, list[dict[str, Any]]] = {}
    for record in seen_canonical.values():
        set_id = str(record.get("setId") or "")
        records_by_set.setdefault(set_id, []).append(record)

    written_files: list[tuple[str, str, Path]] = []
    price_records_written = 0
    JP_PRICES_DIR.mkdir(parents=True, exist_ok=True)

    for set_id, records in sorted(records_by_set.items()):
        if not set_id or not records:
            continue
        records.sort(key=lambda item: str(item.get("canonicalId") or ""))
        set_name = cardscanr_sets.get(set_id).set_name if set_id in cardscanr_sets else set_id
        currency = currency_by_set.get(set_id)
        if not currency:
            continue

        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAtUtc": ts,
            "game": "pokemon",
            "language": "jp",
            "setId": set_id,
            "setName": set_name,
            "source": "pokewallet",
            "currency": currency,
            "status": "partial",
            "priceCount": len(records),
            "lastSuccessfulPriceUpdateAtUtc": ts,
            "nextExpectedPriceUpdateAtUtc": None,
            "expectedUpdateIntervalMinutes": None,
            "isLivePricing": False,
            "staleness": {
                "status": "fresh",
                "ageSeconds": 0,
                "freshForSeconds": 86400,
                "staleAfterSeconds": 172800,
            },
            "prices": records,
        }
        path = JP_PRICES_DIR / f"{set_id}.json"
        cache.write_json(path, payload)
        written_files.append((set_id, set_name, path))
        price_records_written += len(records)

    diagnostics["priceRecordsWritten"] = int(price_records_written)
    diagnostics["priceFilesWritten"] = len(written_files)
    diagnostics["currenciesSeen"] = sorted(currencies_seen)

    if written_files:
        diagnostics["recommendation"] = "Pokewallet set-map probing produced confident priced JP matches; continue cautious partial runs."
    elif not set_map:
        diagnostics["recommendation"] = "No Pokewallet set matches were found for CardScanR JP sets in this controlled run."
    elif diagnostics["resultsFound"] == 0:
        diagnostics["recommendation"] = "Set matches existed, but Pokewallet search returned zero card results for generated set-id queries."
    elif diagnostics["skippedNoPrice"] > 0:
        diagnostics["recommendation"] = "Results were returned but lacked useful numeric prices for controlled JP write criteria."
    elif diagnostics["skippedLowConfidence"] > 0:
        diagnostics["recommendation"] = "Results were returned but confidence stayed below threshold for safe JP writes."
    else:
        diagnostics["recommendation"] = "Pokewallet did not produce enough confident priced JP matches this run; keep as controlled test only."

    cache.write_json(DIAG_PATH, diagnostics)
    all_jp_files = collect_jp_price_files()
    update_status_files(
        ts=ts,
        jp_files=all_jp_files,
        price_records_written=price_records_written,
        currencies_seen=sorted(currencies_seen),
        had_new_records=bool(written_files),
    )
    write_api_docs_updates(ts)
    update_index(ts=ts, jp_files=all_jp_files)

    print(
        "requestsAttempted={requestsAttempted} requestsSucceeded={requestsSucceeded} "
        "resultsFound={resultsFound} possibleJapaneseResults={possibleJapaneseResults} "
        "confidentMatches={confidentMatches} lowConfidenceMatches={lowConfidenceMatches} "
        "priceRecordsWritten={priceRecordsWritten} priceFilesWritten={priceFilesWritten}".format(**diagnostics)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
