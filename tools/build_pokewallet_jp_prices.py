#!/usr/bin/env python3
"""Build controlled-test JP current price cache files from Pokewallet."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import build_price_cache as cache
from probe_pokewallet import (
    POKEWALLET_ENDPOINTS,
    card_info,
    fetch_json,
    list_results,
    possible_japanese,
)

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
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def normalize_name_key(value: str) -> str:
    text = normalize_text(value)
    return re.sub(r"[^\w]+", "", text, flags=re.UNICODE)


def normalize_set_code(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


def normalize_collector(value: str) -> str:
    cleaned = normalize_text(str(value or "")).replace(" ", "")
    return cleaned.upper()


def to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def parse_currency(record: dict[str, Any]) -> str | None:
    value = record.get("currency")
    if isinstance(value, str) and len(value.strip()) == 3:
        return value.strip().upper()

    tcgplayer = record.get("tcgplayer")
    if isinstance(tcgplayer, dict) and tcgplayer.get("prices"):
        return "USD"
    cardmarket = record.get("cardmarket")
    if isinstance(cardmarket, dict) and cardmarket.get("prices"):
        return "EUR"
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
    low = to_float(price_obj.get("low"))
    high = to_float(price_obj.get("high"))
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


def load_jp_catalog_lookup() -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[tuple[str, str], list[dict[str, Any]]], dict[str, str]]:
    by_set_number: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_set_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    set_name_by_id: dict[str, str] = {}

    for path in sorted(JP_CATALOG_CARDS_DIR.glob("*.json")):
        payload = load_json(path)
        cards = payload.get("cards")
        if not isinstance(cards, list):
            continue
        set_id = str(payload.get("setId") or path.stem)
        set_name = str(payload.get("setName") or set_id)
        set_name_by_id[set_id] = set_name

        for card in cards:
            if not isinstance(card, dict):
                continue
            card_set_id = str(card.get("setId") or set_id)
            collector = str(card.get("collectorNumber") or "")
            name = str(card.get("name") or card.get("normalizedName") or "")
            by_set_number[(normalize_set_code(card_set_id), normalize_collector(collector))].append(card)
            by_set_name[(normalize_set_code(card_set_id), normalize_name_key(name))].append(card)

    return by_set_number, by_set_name, set_name_by_id


def build_match_score(
    *,
    record: dict[str, Any],
    catalog_card: dict[str, Any],
    provider_set_code: str,
    provider_set_name: str,
    provider_number: str,
    provider_name: str,
) -> tuple[float, list[str]]:
    score = 0.0
    signals: list[str] = []

    card_set_id = str(catalog_card.get("setId") or "")
    card_set_name = str(catalog_card.get("setName") or "")
    card_number = str(catalog_card.get("collectorNumber") or "")
    card_name = str(catalog_card.get("name") or catalog_card.get("normalizedName") or "")

    if provider_set_code and normalize_set_code(card_set_id) == normalize_set_code(provider_set_code):
        score += 0.55
        signals.append("set_code_exact")

    if provider_number and normalize_collector(card_number) == normalize_collector(provider_number):
        score += 0.30
        signals.append("collector_number_exact")

    if provider_set_name and normalize_name_key(provider_set_name) == normalize_name_key(card_set_name):
        score += 0.10
        signals.append("set_name_exact")

    if provider_name and normalize_name_key(provider_name) == normalize_name_key(card_name):
        score += 0.10
        signals.append("name_exact")

    if isinstance(record.get("language"), str) and record.get("language", "").lower() in {"ja", "jp", "japanese"}:
        score += 0.02
        signals.append("language_signal")

    return min(score, 1.0), signals


def map_record_to_catalog(
    record: dict[str, Any],
    by_set_number: dict[tuple[str, str], list[dict[str, Any]]],
    by_set_name: dict[tuple[str, str], list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, float, list[str]]:
    info = card_info(record)
    provider_set_code = str(info.get("set_code") or record.get("setCode") or record.get("set_code") or "")
    provider_set_name = str(info.get("set_name") or record.get("setName") or record.get("set_name") or "")
    provider_number = str(info.get("card_number") or record.get("number") or record.get("card_number") or "")
    provider_name = str(info.get("name") or info.get("clean_name") or record.get("name") or "")

    candidates: list[dict[str, Any]] = []

    if provider_set_code and provider_number:
        candidates.extend(
            by_set_number.get((normalize_set_code(provider_set_code), normalize_collector(provider_number)), [])
        )

    if provider_set_code and provider_name:
        candidates.extend(by_set_name.get((normalize_set_code(provider_set_code), normalize_name_key(provider_name)), []))

    if not candidates:
        return None, 0.0, ["no_catalog_candidate"]

    best_card = None
    best_score = -1.0
    best_signals: list[str] = []
    for card in candidates:
        score, signals = build_match_score(
            record=record,
            catalog_card=card,
            provider_set_code=provider_set_code,
            provider_set_name=provider_set_name,
            provider_number=provider_number,
            provider_name=provider_name,
        )
        if score > best_score:
            best_card = card
            best_score = score
            best_signals = signals

    return best_card, best_score, best_signals


def canonical_id_for(card: dict[str, Any], variant: str = "normal", condition: str = "near_mint") -> str:
    set_id = str(card.get("setId") or "")
    collector = str(card.get("collectorNumber") or "")
    normalized_name = str(card.get("normalizedName") or card.get("name") or "")
    return f"pokemon|jp|{set_id}|{collector}|{normalized_name}|{variant}|{condition}"


def build_record(
    *,
    record: dict[str, Any],
    catalog_card: dict[str, Any],
    confidence: float,
    signals: list[str],
    fetched_at_utc: str,
    currency: str,
    market: float | None,
    low: float | None,
    high: float | None,
) -> dict[str, Any]:
    return {
        "canonicalId": canonical_id_for(catalog_card),
        "setId": str(catalog_card.get("setId") or ""),
        "collectorNumber": str(catalog_card.get("collectorNumber") or ""),
        "normalizedName": str(catalog_card.get("normalizedName") or catalog_card.get("name") or ""),
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
        "matchConfidence": round(confidence, 3),
        "matchSignals": signals,
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
    has_diag = any(isinstance(item, dict) and item.get("id") == "diagnostics_pokewallet_jp_price_build" for item in endpoints)
    if not has_diag:
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


def update_index(
    *,
    ts: str,
    jp_files: list[tuple[str, str, Path]],
) -> None:
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


def update_status_files(
    *,
    ts: str,
    diagnostics: dict[str, Any],
    jp_files: list[tuple[str, str, Path]],
    price_records_written: int,
    currencies_seen: list[str],
) -> None:
    prices_status = load_json(PRICES_STATUS_PATH)
    if not isinstance(prices_status.get("languages"), dict):
        prices_status["languages"] = {}

    if jp_files:
        status = "partial"
        file_count = len(jp_files)
        source_currency = currencies_seen[0] if len(currencies_seen) == 1 else "mixed"
        notes = [
            "Controlled Pokewallet JP current price test with partial set coverage.",
            "Provider currency is passed through as-is and is not converted.",
            "JP nextExpectedPriceUpdateAtUtc is null until regular JP scheduling exists.",
        ]
    else:
        status = "not_available"
        file_count = 0
        source_currency = None
        notes = [
            "Controlled Pokewallet JP current price test produced no confident priced matches.",
            "Japanese catalogue exists but JP current prices remain unavailable.",
            "Provider currency is passed through as-is and is not converted.",
        ]

    jp_summary = {
        "game": "pokemon",
        "language": "jp",
        "status": status,
        "currentPriceFilesAvailable": bool(jp_files),
        "currentPriceSetFileCount": file_count,
        "currentPriceRecordCount": int(price_records_written),
        "lastSuccessfulPriceUpdateAtUtc": ts if jp_files else None,
        "nextExpectedPriceUpdateAtUtc": None,
        "staleness": {
            "status": "fresh" if jp_files else "unavailable",
            "ageSeconds": 0 if jp_files else None,
            "freshForSeconds": 86400,
            "staleAfterSeconds": 172800,
        },
        "sourceSummary": {
            "primarySource": "pokewallet" if jp_files else "pokewallet",
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
        "currentPriceFilesAvailable": bool(jp_files),
        "currentPriceSetFileCount": file_count,
        "currentPriceRecordCount": int(price_records_written),
        "lastSuccessfulPriceUpdateAtUtc": ts if jp_files else None,
        "lastSuccessfulPushAtUtc": None,
        "lastBatchSetIds": [set_id for set_id, _set_name, _path in jp_files],
        "lastBatchSize": file_count,
        "lastBatchStartedAtUtc": ts if jp_files else None,
        "lastBatchFinishedAtUtc": ts if jp_files else None,
        "lastBatchDurationSeconds": 0 if jp_files else None,
        "nextExpectedPriceUpdateAtUtc": None,
        "expectedUpdateIntervalMinutes": None,
        "fullRotationEstimatedHours": None,
        "currency": source_currency,
        "isLivePricing": False,
        "staleness": {
            "status": "fresh" if jp_files else "unavailable",
            "ageSeconds": 0 if jp_files else None,
            "freshForSeconds": 86400,
            "staleAfterSeconds": 172800,
        },
        "notes": notes,
    }
    cache.write_json(JP_STATUS_PATH, jp_status)


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
        "sampleMatches": [],
        "sampleSkipped": [],
        "recommendation": "",
    }


def append_sample(container: list[dict[str, Any]], item: dict[str, Any], limit: int = 12) -> None:
    if len(container) < limit:
        container.append(item)


def main() -> int:
    ts = now_utc()
    config = load_json(CONFIG_PATH)

    api_key_env = str(config.get("apiKeyEnv") or "POKEWALLET_API_KEY")
    api_key = os.getenv(api_key_env, "").strip()
    base_url = "https://api.pokewallet.io"
    max_requests = max(1, int(config.get("maxRequestsPerRun") or 1))
    sleep_seconds = max(0.0, float(config.get("requestSleepSeconds") or 0.0))
    confidence_threshold = float(config.get("confidenceThreshold") or 0.82)
    mode = str(config.get("mode") or "controlled_test")
    search_targets = [item for item in config.get("searchTargets", []) if isinstance(item, dict)]

    diagnostics = build_diagnostics_base(ts, mode, bool(api_key))

    if not bool(config.get("enabled", True)):
        diagnostics["recommendation"] = "Pokewallet JP controlled test is disabled in data/pokewallet_jp_price_config.json."
        cache.write_json(DIAG_PATH, diagnostics)
        update_status_files(
            ts=ts,
            diagnostics=diagnostics,
            jp_files=[],
            price_records_written=0,
            currencies_seen=[],
        )
        write_api_docs_updates(ts)
        update_index(ts=ts, jp_files=[])
        print("Controlled test disabled; wrote diagnostics and status metadata only.")
        return 0

    if not api_key:
        diagnostics["recommendation"] = (
            "POKEWALLET_API_KEY is not set. Real JP controlled test could not run; no JP price records were written."
        )
        cache.write_json(DIAG_PATH, diagnostics)
        update_status_files(
            ts=ts,
            diagnostics=diagnostics,
            jp_files=[],
            price_records_written=0,
            currencies_seen=[],
        )
        write_api_docs_updates(ts)
        update_index(ts=ts, jp_files=[])
        print("POKEWALLET_API_KEY missing; wrote diagnostics and status metadata only.")
        return 0

    by_set_number, by_set_name, set_name_by_id = load_jp_catalog_lookup()

    records_by_set: dict[str, list[dict[str, Any]]] = defaultdict(list)
    currency_by_set: dict[str, str] = {}
    seen_canonical: dict[str, dict[str, Any]] = {}
    currencies_seen: set[str] = set()

    for target in search_targets:
        if diagnostics["requestsAttempted"] >= max_requests:
            break
        query = str(target.get("query") or "").strip()
        if not query:
            continue

        diagnostics["searchTargetsTested"].append(query)
        search_url = (
            f"{base_url}{POKEWALLET_ENDPOINTS['search']['path']}?q={quote(query)}&page=1&limit=8"
        )

        diagnostics["requestsAttempted"] += 1
        try:
            payload = fetch_json(search_url, api_key=api_key)
            diagnostics["requestsSucceeded"] += 1
        except Exception as exc:  # noqa: BLE001
            diagnostics["requestsFailed"] += 1
            append_sample(
                diagnostics["sampleSkipped"],
                {
                    "query": query,
                    "reason": "request_failed",
                    "detail": str(exc),
                },
            )
            if sleep_seconds:
                time.sleep(sleep_seconds)
            continue

        results = list_results(payload)
        diagnostics["resultsFound"] += len(results)

        for result in results:
            if possible_japanese(result):
                diagnostics["possibleJapaneseResults"] += 1

            matched_card, confidence, signals = map_record_to_catalog(result, by_set_number, by_set_name)
            if matched_card is None:
                diagnostics["unmappedResults"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {
                        "query": query,
                        "providerId": str(result.get("id") or ""),
                        "reason": "unmapped",
                    },
                )
                continue

            if confidence < confidence_threshold:
                diagnostics["lowConfidenceMatches"] += 1
                append_sample(
                    diagnostics["sampleSkipped"],
                    {
                        "query": query,
                        "providerId": str(result.get("id") or ""),
                        "reason": "low_confidence",
                        "confidence": round(confidence, 3),
                        "signals": signals,
                    },
                )
                continue

            price_tuple = extract_price(result)
            if price_tuple is None:
                append_sample(
                    diagnostics["sampleSkipped"],
                    {
                        "query": query,
                        "providerId": str(result.get("id") or ""),
                        "reason": "no_useful_price",
                    },
                )
                continue

            currency = parse_currency(result)
            if currency is None:
                append_sample(
                    diagnostics["sampleSkipped"],
                    {
                        "query": query,
                        "providerId": str(result.get("id") or ""),
                        "reason": "currency_missing",
                    },
                )
                continue

            market, low, high = price_tuple
            if market is None and low is None and high is None:
                append_sample(
                    diagnostics["sampleSkipped"],
                    {
                        "query": query,
                        "providerId": str(result.get("id") or ""),
                        "reason": "no_numeric_price",
                    },
                )
                continue

            diagnostics["confidentMatches"] += 1
            set_id = str(matched_card.get("setId") or "")
            if not set_id:
                diagnostics["unmappedResults"] += 1
                continue

            if set_id in currency_by_set and currency_by_set[set_id] != currency:
                append_sample(
                    diagnostics["sampleSkipped"],
                    {
                        "query": query,
                        "providerId": str(result.get("id") or ""),
                        "reason": "set_currency_mismatch",
                        "setId": set_id,
                    },
                )
                continue
            currency_by_set[set_id] = currency

            provider_id = str(result.get("id") or "")
            record_payload = build_record(
                record=result,
                catalog_card=matched_card,
                confidence=confidence,
                signals=signals,
                fetched_at_utc=ts,
                currency=currency,
                market=market,
                low=low,
                high=high,
            )
            canonical_id = record_payload["canonicalId"]
            existing = seen_canonical.get(canonical_id)
            if existing and float(existing.get("matchConfidence") or 0.0) >= record_payload["matchConfidence"]:
                continue

            seen_canonical[canonical_id] = record_payload
            currencies_seen.add(currency)
            append_sample(
                diagnostics["sampleMatches"],
                {
                    "providerId": provider_id,
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

    records_by_set = defaultdict(list)
    for record in seen_canonical.values():
        records_by_set[str(record.get("setId") or "")].append(record)

    written_files: list[tuple[str, str, Path]] = []
    price_records_written = 0

    JP_PRICES_DIR.mkdir(parents=True, exist_ok=True)

    for set_id, records in sorted(records_by_set.items()):
        if not set_id or not records:
            continue
        records.sort(key=lambda item: str(item.get("canonicalId") or ""))
        set_name = set_name_by_id.get(set_id, set_id)
        currency = currency_by_set.get(set_id)
        if currency is None:
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
        diagnostics["recommendation"] = (
            "Pokewallet produced confident JP matches with usable prices; continue cautious partial runs before regular refresh."
        )
    else:
        diagnostics["recommendation"] = (
            "Pokewallet did not produce enough confident priced JP matches this run; keep as controlled test only."
        )

    cache.write_json(DIAG_PATH, diagnostics)
    update_status_files(
        ts=ts,
        diagnostics=diagnostics,
        jp_files=written_files,
        price_records_written=price_records_written,
        currencies_seen=sorted(currencies_seen),
    )
    write_api_docs_updates(ts)
    update_index(ts=ts, jp_files=written_files)

    print(
        "requestsAttempted={requestsAttempted} requestsSucceeded={requestsSucceeded} "
        "resultsFound={resultsFound} possibleJapaneseResults={possibleJapaneseResults} "
        "confidentMatches={confidentMatches} priceRecordsWritten={priceRecordsWritten} "
        "priceFilesWritten={priceFilesWritten}".format(**diagnostics)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
