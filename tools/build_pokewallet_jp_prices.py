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
from probe_pokewallet import POKEWALLET_ENDPOINTS, card_info, fetch_json, image_present, list_results, possible_japanese

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

DEFAULT_SEARCH_TEMPLATES = [
    "{name} {setName} {collectorNumber} japanese pokemon",
    "{name} {collectorNumber} japanese pokemon",
    "{name} {setName} pokemon card",
]
DEFAULT_PREFERRED_SET_IDS = ["SV10", "SV11B", "SV11W", "SV9", "SV9a", "S12a", "PMCG1", "E1", "E2"]


@dataclass(frozen=True)
class TargetCard:
    set_id: str
    set_name: str
    collector_number: str
    name: str
    normalized_name: str
    canonical_base_id: str
    image_small: str | None
    image_large: str | None


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


def cleaned_query(text: str) -> str:
    query = re.sub(r"\s+", " ", text or "").strip()
    return re.sub(r"\b(None|null|nil)\b", "", query, flags=re.IGNORECASE).strip()


def to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def parse_currency(record: dict[str, Any]) -> str | None:
    value = record.get("currency")
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text.upper() if len(text) == 3 else text
    return None


def extract_from_price_obj(price_obj: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    market = to_float(
        price_obj.get("market")
        if price_obj.get("market") is not None
        else price_obj.get("marketPrice")
        if price_obj.get("marketPrice") is not None
        else price_obj.get("mid")
        if price_obj.get("mid") is not None
        else price_obj.get("price")
    )
    low = to_float(price_obj.get("low") if price_obj.get("low") is not None else price_obj.get("min"))
    high = to_float(price_obj.get("high") if price_obj.get("high") is not None else price_obj.get("max"))
    return market, low, high


def extract_price(record: dict[str, Any]) -> tuple[float | None, float | None, float | None] | None:
    candidates: list[dict[str, Any]] = []

    for container_key in ["tcgplayer", "cardmarket"]:
        container = record.get(container_key)
        if not isinstance(container, dict):
            continue
        prices = container.get("prices")
        if isinstance(prices, list):
            for item in prices:
                if isinstance(item, dict):
                    candidates.append(item)
        elif isinstance(prices, dict):
            for item in prices.values():
                if isinstance(item, dict):
                    candidates.append(item)

    direct_prices = record.get("prices")
    if isinstance(direct_prices, list):
        for item in direct_prices:
            if isinstance(item, dict):
                candidates.append(item)
    elif isinstance(direct_prices, dict):
        candidates.append(direct_prices)

    if not candidates:
        market = to_float(record.get("market_price") or record.get("marketPrice") or record.get("price"))
        low = to_float(record.get("low"))
        high = to_float(record.get("high"))
        if market is None and low is None and high is None:
            return None
        return market, low, high

    for price_obj in candidates:
        market, low, high = extract_from_price_obj(price_obj)
        if market is not None or low is not None or high is not None:
            return market, low, high

    return None


def similarity_ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def load_target_cards() -> tuple[list[TargetCard], dict[str, str]]:
    cards: list[TargetCard] = []
    set_name_by_id: dict[str, str] = {}

    for path in sorted(JP_CATALOG_CARDS_DIR.glob("*.json")):
        payload = load_json(path)
        set_id = str(payload.get("setId") or path.stem)
        set_name = str(payload.get("setName") or set_id)
        set_name_by_id[set_id] = set_name

        raw_cards = payload.get("cards")
        if not isinstance(raw_cards, list):
            continue

        for card in raw_cards:
            if not isinstance(card, dict):
                continue
            name = str(card.get("name") or card.get("normalizedName") or "").strip()
            set_name_value = str(card.get("setName") or set_name).strip()
            collector = str(card.get("collectorNumber") or "").strip()
            if not name or not set_name_value or not collector:
                continue
            cards.append(
                TargetCard(
                    set_id=str(card.get("setId") or set_id).strip(),
                    set_name=set_name_value,
                    collector_number=collector,
                    name=name,
                    normalized_name=str(card.get("normalizedName") or name).strip(),
                    canonical_base_id=str(card.get("canonicalBaseId") or "").strip(),
                    image_small=card.get("imageSmall") if isinstance(card.get("imageSmall"), str) else None,
                    image_large=card.get("imageLarge") if isinstance(card.get("imageLarge"), str) else None,
                )
            )

    return cards, set_name_by_id


def choose_sample_cards(
    cards: list[TargetCard],
    *,
    preferred_set_ids: list[str],
    sample_limit: int,
) -> tuple[list[TargetCard], list[str]]:
    preferred = {normalize_set_code(item) for item in preferred_set_ids if str(item).strip()}
    ordered: list[TargetCard] = []

    def score(card: TargetCard) -> tuple[int, int]:
        preferred_hit = 0 if normalize_set_code(card.set_id) in preferred else 1
        has_image = 0 if (card.image_small or card.image_large) else 1
        return (preferred_hit, has_image)

    for card in sorted(cards, key=score):
        ordered.append(card)

    seen: set[tuple[str, str, str]] = set()
    selected: list[TargetCard] = []
    for card in ordered:
        key = (normalize_name_key(card.normalized_name), normalize_set_code(card.set_id), normalize_collector(card.collector_number))
        if key in seen:
            continue
        seen.add(key)
        selected.append(card)
        if len(selected) >= sample_limit:
            break

    preferred_used = sorted({card.set_id for card in selected if normalize_set_code(card.set_id) in preferred})
    return selected, preferred_used


def build_catalogue_queries(
    cards: list[TargetCard],
    templates: list[str],
    *,
    max_requests: int,
) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for card in cards:
        for template in templates:
            raw = template.format(
                name=card.name,
                setName=card.set_name,
                collectorNumber=card.collector_number,
                setId=card.set_id,
            )
            query = cleaned_query(raw)
            if not query:
                continue
            dedupe = normalize_text(query)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            queries.append(
                {
                    "query": query,
                    "target": {
                        "setId": card.set_id,
                        "setName": card.set_name,
                        "collectorNumber": card.collector_number,
                        "name": card.name,
                        "normalizedName": card.normalized_name,
                        "canonicalBaseId": card.canonical_base_id,
                    },
                }
            )
            if len(queries) >= max_requests:
                return queries
    return queries


def provider_snapshot(record: dict[str, Any]) -> dict[str, str]:
    info = card_info(record)
    return {
        "name": str(info.get("name") or info.get("clean_name") or record.get("name") or ""),
        "set_name": str(info.get("set_name") or record.get("setName") or record.get("set_name") or ""),
        "set_code": str(info.get("set_code") or info.get("set_id") or record.get("setCode") or record.get("set_code") or ""),
        "number": str(info.get("card_number") or record.get("number") or record.get("card_number") or ""),
        "language": str(record.get("language") or info.get("language") or ""),
    }


def score_match(record: dict[str, Any], target: dict[str, Any]) -> tuple[float, list[str]]:
    provider = provider_snapshot(record)
    signals: list[str] = []
    score = 0.0

    target_name_key = normalize_name_key(str(target.get("normalizedName") or target.get("name") or ""))
    provider_name_key = normalize_name_key(provider["name"])
    if target_name_key and provider_name_key:
        ratio = similarity_ratio(target_name_key, provider_name_key)
        if provider_name_key == target_name_key:
            score += 0.40
            signals.append("name_exact")
        elif ratio >= 0.90:
            score += 0.36
            signals.append("name_fuzzy_high")
        elif ratio >= 0.75:
            score += 0.30
            signals.append("name_fuzzy_medium")

    target_collector = normalize_collector(str(target.get("collectorNumber") or ""))
    provider_collector = normalize_collector(provider["number"])
    if target_collector and provider_collector and target_collector == provider_collector:
        score += 0.25
        signals.append("collector_exact")

    target_set_id = normalize_set_code(str(target.get("setId") or ""))
    provider_set_code = normalize_set_code(provider["set_code"])
    target_set_name_key = normalize_name_key(str(target.get("setName") or ""))
    provider_set_name_key = normalize_name_key(provider["set_name"])

    set_score = 0.0
    if target_set_id and provider_set_code and target_set_id == provider_set_code:
        set_score = max(set_score, 0.25)
        signals.append("set_code_exact")
    if target_set_name_key and provider_set_name_key:
        ratio = similarity_ratio(target_set_name_key, provider_set_name_key)
        if target_set_name_key == provider_set_name_key:
            set_score = max(set_score, 0.25)
            signals.append("set_name_exact")
        elif ratio >= 0.90:
            set_score = max(set_score, 0.20)
            signals.append("set_name_fuzzy_high")
        elif ratio >= 0.75:
            set_score = max(set_score, 0.14)
            signals.append("set_name_fuzzy_medium")
    score += set_score

    if provider["language"].lower() in {"ja", "jp", "japanese"} or possible_japanese(record):
        score += 0.10
        signals.append("language_jp")

    if image_present(record):
        score += 0.03
        signals.append("image_present")

    return min(score, 1.0), signals


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
    if value >= 0.9:
        return "0.90-1.00"
    if value >= 0.8:
        return "0.80-0.89"
    if value >= 0.7:
        return "0.70-0.79"
    if value >= 0.6:
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
            "matchScoreDistribution",
            "skippedNoPrice",
            "skippedLowConfidence",
            "skippedNoCanonicalMatch",
            "skippedNoCurrency",
            "sampleSearchTargets",
            "sampleMatches",
            "sampleSkipped",
        ],
        "notes": [
            "Controlled Pokewallet JP price build diagnostics without secrets or raw payload dumps.",
            "Records partial JP test coverage and confidence-based mapping outcomes.",
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
        set_id = str(payload.get("setId") or path.stem)
        set_name = str(payload.get("setName") or set_id)
        files.append((set_id, set_name, path))
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
        dataset_id = f"prices_current_pokemon_jp_{set_id}"
        by_id[dataset_id] = cache.build_index_dataset_entry(
            dataset_id=dataset_id,
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
    index["datasets"] = sorted(by_id.values(), key=lambda entry: str(entry.get("id")))
    cache.write_json(INDEX_PATH, index)


def update_status_files(*, ts: str, jp_files: list[tuple[str, str, Path]], price_records_written: int, currencies_seen: list[str], had_new_records: bool) -> None:
    prices_status = load_json(PRICES_STATUS_PATH)
    if not isinstance(prices_status.get("languages"), dict):
        prices_status["languages"] = {}

    if had_new_records and jp_files:
        status = "partial"
        notes = [
            "Controlled Pokewallet JP current price test with partial set coverage.",
            "Provider currency is passed through as-is and is not converted.",
            "JP nextExpectedPriceUpdateAtUtc is null until regular JP scheduling exists.",
        ]
        source_currency = currencies_seen[0] if len(currencies_seen) == 1 else "mixed"
        staleness_status = "fresh"
        age_seconds = 0
        current_available = True
        last_success = ts
    else:
        status = "not_available"
        notes = [
            "Controlled Pokewallet JP current price test produced no confident priced matches.",
            "Japanese catalogue exists but JP current prices remain unavailable.",
            "Provider currency is passed through as-is and is not converted.",
        ]
        source_currency = None
        staleness_status = "unavailable"
        age_seconds = None
        current_available = False
        last_success = None

    jp_summary = {
        "game": "pokemon",
        "language": "jp",
        "status": status,
        "currentPriceFilesAvailable": current_available,
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

    prices_status["generatedAtUtc"] = ts
    prices_status["languages"]["jp"] = jp_summary
    cache.write_json(PRICES_STATUS_PATH, prices_status)

    jp_status = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "game": "pokemon",
        "language": "jp",
        "status": status,
        "currentPriceFilesAvailable": current_available,
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

    api_key_env = str(config.get("apiKeyEnv") or "POKEWALLET_API_KEY")
    api_key = os.getenv(api_key_env, "").strip()
    max_requests = max(1, int(config.get("maxRequestsPerRun") or 25))
    sleep_seconds = max(0.0, float(config.get("requestSleepSeconds") or 0.0))
    confidence_threshold = float(config.get("confidenceThreshold") or 0.82)
    mode = str(config.get("mode") or "controlled_test")
    base_url = "https://api.pokewallet.io"

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
        diagnostics["recommendation"] = (
            "POKEWALLET_API_KEY is not set. Real JP controlled test could not run; no JP price records were written."
        )
        cache.write_json(DIAG_PATH, diagnostics)
        update_status_files(ts=ts, jp_files=[], price_records_written=0, currencies_seen=[], had_new_records=False)
        write_api_docs_updates(ts)
        update_index(ts=ts, jp_files=collect_jp_price_files())
        print("POKEWALLET_API_KEY missing; wrote diagnostics and status metadata only.")
        return 0

    all_cards, set_name_by_id = load_target_cards()
    diagnostics["catalogueCardsLoaded"] = len(all_cards)

    use_catalogue_targets = bool(config.get("useCatalogueSampleTargets", True))
    sample_limit = max(1, int(config.get("catalogueSampleLimit") or 25))
    preferred_set_ids = [str(item) for item in (config.get("cataloguePreferredSetIds") or DEFAULT_PREFERRED_SET_IDS)]
    templates = [str(item) for item in (config.get("catalogueSearchTemplates") or DEFAULT_SEARCH_TEMPLATES)]

    selected_cards: list[TargetCard] = []
    preferred_used: list[str] = []
    query_targets: list[dict[str, Any]] = []

    if use_catalogue_targets:
        selected_cards, preferred_used = choose_sample_cards(
            all_cards,
            preferred_set_ids=preferred_set_ids,
            sample_limit=sample_limit,
        )
        diagnostics["catalogueSampleTargetsBuilt"] = len(selected_cards)
        diagnostics["cataloguePreferredSetIdsUsed"] = preferred_used
        query_targets = build_catalogue_queries(selected_cards, templates, max_requests=max_requests)

    if not query_targets:
        query_targets = [item for item in config.get("searchTargets", []) if isinstance(item, dict)]

    diagnostics["catalogueSearchQueriesBuilt"] = len(query_targets)
    for target in query_targets[:12]:
        append_sample(
            diagnostics["sampleSearchTargets"],
            {
                "query": str(target.get("query") or ""),
                "setId": str((target.get("target") or {}).get("setId") or ""),
                "collectorNumber": str((target.get("target") or {}).get("collectorNumber") or ""),
                "name": str((target.get("target") or {}).get("name") or ""),
            },
        )

    seen_canonical: dict[str, dict[str, Any]] = {}
    currency_by_set: dict[str, str] = {}
    currencies_seen: set[str] = set()

    for item in query_targets:
        if diagnostics["requestsAttempted"] >= max_requests:
            break
        query = cleaned_query(str(item.get("query") or ""))
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        if not query:
            continue

        diagnostics["searchTargetsTested"].append(query)
        search_url = f"{base_url}{POKEWALLET_ENDPOINTS['search']['path']}?q={quote(query)}&page=1&limit=8"

        diagnostics["requestsAttempted"] += 1
        try:
            payload = fetch_json(search_url, api_key=api_key)
            diagnostics["requestsSucceeded"] += 1
        except Exception as exc:  # noqa: BLE001
            diagnostics["requestsFailed"] += 1
            append_sample(
                diagnostics["sampleSkipped"],
                {"query": query, "reason": "request_failed", "detail": str(exc)},
            )
            if sleep_seconds:
                time.sleep(sleep_seconds)
            continue

        results = list_results(payload)
        diagnostics["resultsFound"] += len(results)

        for record in results:
            if possible_japanese(record):
                diagnostics["possibleJapaneseResults"] += 1

            if not target:
                diagnostics["unmappedResults"] += 1
                diagnostics["skippedNoCanonicalMatch"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {"query": query, "providerId": str(record.get("id") or ""), "reason": "no_target_card"},
                )
                continue

            score, signals = score_match(record, target)
            diagnostics["matchScoreDistribution"][score_bucket(score)] += 1

            if score < confidence_threshold:
                diagnostics["lowConfidenceMatches"] += 1
                diagnostics["skippedLowConfidence"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {
                        "query": query,
                        "providerId": str(record.get("id") or ""),
                        "reason": "low_confidence",
                        "confidence": round(score, 4),
                        "signals": signals,
                    },
                )
                continue

            set_id = str(target.get("setId") or "")
            if not set_id:
                diagnostics["unmappedResults"] += 1
                diagnostics["skippedNoCanonicalMatch"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {"query": query, "providerId": str(record.get("id") or ""), "reason": "missing_target_set_id"},
                )
                continue

            price_tuple = extract_price(record)
            if price_tuple is None:
                diagnostics["skippedNoPrice"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {"query": query, "providerId": str(record.get("id") or ""), "reason": "no_useful_price"},
                )
                continue

            currency = parse_currency(record)
            if not currency:
                diagnostics["skippedNoCurrency"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {"query": query, "providerId": str(record.get("id") or ""), "reason": "currency_missing"},
                )
                continue

            if set_id in currency_by_set and currency_by_set[set_id] != currency:
                diagnostics["skippedNoCurrency"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {
                        "query": query,
                        "providerId": str(record.get("id") or ""),
                        "reason": "set_currency_mismatch",
                        "setId": set_id,
                    },
                )
                continue

            market, low, high = price_tuple
            if market is None and low is None and high is None:
                diagnostics["skippedNoPrice"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {"query": query, "providerId": str(record.get("id") or ""), "reason": "no_numeric_price"},
                )
                continue

            diagnostics["confidentMatches"] += 1
            currency_by_set[set_id] = currency
            currencies_seen.add(currency)

            record_payload = build_record(
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
            canonical_id = record_payload["canonicalId"]
            existing = seen_canonical.get(canonical_id)
            if existing and float(existing.get("matchConfidence") or 0.0) >= float(record_payload["matchConfidence"]):
                continue

            seen_canonical[canonical_id] = record_payload
            append_sample(
                diagnostics["sampleMatches"],
                {
                    "providerId": str(record.get("id") or ""),
                    "canonicalId": canonical_id,
                    "setId": set_id,
                    "collectorNumber": record_payload["collectorNumber"],
                    "currency": currency,
                    "confidence": record_payload["matchConfidence"],
                    "signals": signals,
                },
            )

        if sleep_seconds:
            time.sleep(sleep_seconds)

    records_by_set: dict[str, list[dict[str, Any]]] = {}
    for payload in seen_canonical.values():
        set_id = str(payload.get("setId") or "")
        records_by_set.setdefault(set_id, []).append(payload)

    written_files: list[tuple[str, str, Path]] = []
    price_records_written = 0
    JP_PRICES_DIR.mkdir(parents=True, exist_ok=True)

    for set_id, records in sorted(records_by_set.items()):
        if not set_id or not records:
            continue
        currency = currency_by_set.get(set_id)
        if not currency:
            continue
        records.sort(key=lambda item: str(item.get("canonicalId") or ""))
        set_name = set_name_by_id.get(set_id, set_id)

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

    diagnostics["priceRecordsWritten"] = price_records_written
    diagnostics["priceFilesWritten"] = len(written_files)
    diagnostics["currenciesSeen"] = sorted(currencies_seen)

    if written_files:
        diagnostics["recommendation"] = (
            "Pokewallet produced confident JP matches with usable prices; continue cautious partial runs before regular refresh."
        )
    else:
        diagnostics["recommendation"] = (
            "Pokewallet did not produce enough confident priced JP matches this run; keep as controlled test only."
        )

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
