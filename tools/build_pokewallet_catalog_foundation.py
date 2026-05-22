#!/usr/bin/env python3
"""Build app-friendly Pokewallet provider catalogue foundation files."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / "public" / "v1"
CONFIG_PATH = ROOT / "data" / "pokewallet_catalog_config.json"
OUTPUT_DIR = PUBLIC_DIR / "provider-catalog" / "pokewallet"
CARDS_DIR = OUTPUT_DIR / "cards"
STATUS_PATH = OUTPUT_DIR / "status.json"
CARDS_MANIFEST_PATH = OUTPUT_DIR / "cards-manifest.json"
DIAG_PATH = PUBLIC_DIR / "diagnostics" / "pokewallet-catalog-foundation-latest.json"
INDEX_PATH = PUBLIC_DIR / "index.json"
DEFAULT_FULL_STATE_PATH = ROOT / "data" / "pokewallet_catalog_full_state.json"
BASE_URL = "https://api.pokewallet.io"
SCHEMA_VERSION = "1.0.0"
MAX_SAMPLE_ITEMS = 20


@dataclass(frozen=True)
class ProviderSet:
    set_id: str
    set_code: str
    name: str
    language: str
    app_language: str
    card_count: int | None
    release_date: str | None


@dataclass
class FetchResult:
    ok: bool
    status_code: int | None
    payload: dict[str, Any] | None
    headers: dict[str, str]
    error: str | None = None


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    encoded = rendered.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(rendered)


def payload_without_top_level_fields(payload: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in fields}


def preserve_top_level_timestamps_if_unchanged(path: Path, payload: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    if not path.exists() or not fields:
        return payload
    try:
        existing = load_json(path)
    except Exception:
        return payload
    if payload_without_top_level_fields(existing, fields) == payload_without_top_level_fields(payload, fields):
        merged = dict(payload)
        for field in fields:
            if field in existing:
                merged[field] = existing[field]
        return merged
    return payload


def preserve_generated_at_if_unchanged(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return preserve_top_level_timestamps_if_unchanged(path, payload, {"generatedAtUtc"})


def preserve_updated_at_if_unchanged(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return preserve_top_level_timestamps_if_unchanged(path, payload, {"updatedAtUtc"})


def provider_card_sort_key(card: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        string_value(card.get("cardScanRLanguage")),
        string_value(card.get("providerSetId") or card.get("providerSetCode")),
        normalized_key_part(card.get("cardNumber")),
        normalized_key_part(card.get("name") or card.get("cleanName")),
        string_value(card.get("providerCardId")),
        string_value(card.get("providerCanonicalImageKey")),
    )


def sort_provider_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(cards, key=provider_card_sort_key)


def sort_image_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        samples,
        key=lambda sample: (
            string_value(sample.get("cardScanRLanguage")),
            string_value(sample.get("providerSetId")),
            string_value(sample.get("providerCardId")),
            string_value(sample.get("size")),
            string_value(sample.get("imageEndpoint")),
        ),
    )


def sort_public_set_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda record: (
            string_value(record.get("cardScanRLanguage")),
            string_value(record.get("providerLanguage")),
            string_value(record.get("providerSetId")),
            string_value(record.get("providerSetCode")),
            string_value(record.get("providerSetName")),
        ),
    )


def safe_log(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"))


def safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_error(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, URLError):
        return "url_error"
    return exc.__class__.__name__


def append_sample(container: list[dict[str, Any]], item: dict[str, Any], limit: int = MAX_SAMPLE_ITEMS) -> None:
    if len(container) < limit:
        container.append(item)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel_path(path: Path) -> str:
    return str(path.relative_to(ROOT))


def configured_path(value: Any, default_path: Path) -> Path:
    text = str(value or "").strip()
    if not text:
        return default_path
    path = Path(text)
    return path if path.is_absolute() else ROOT / path


def provider_user_agent() -> str:
    return "CardScanR-PokeWallet-Catalog-Foundation/1.0"


def base_notes() -> list[str]:
    return [
        "Pokewallet provider catalogue foundation for CardScanR matching research.",
        "Only safe provider metadata is stored.",
        "Image references are stored as API endpoints only; image files are not stored in this repository.",
        "Provider metadata is not official CardScanR canonical data yet.",
        "Production catalogue and pricing integration are separate later steps.",
    ]


def base_diag(ts: str, api_key_present: bool, *, mode: str = "catalogue_foundation") -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "provider": "pokewallet",
        "mode": mode,
        "status": "ok",
        "apiKeyPresent": api_key_present,
        "fullCatalogueEnabled": False,
        "requestsAttempted": 0,
        "requestsSucceeded": 0,
        "requestsFailed": 0,
        "setsFetched": 0,
        "setsProcessedThisRun": 0,
        "setsRemainingAfterRun": 0,
        "cardsWrittenThisRun": 0,
        "cardsWrittenByLanguage": {},
        "setFilesWritten": 0,
        "languagesSeen": {},
        "setsSelectedByLanguage": {},
        "cardsFetchedByLanguage": {},
        "imageSamplesChecked": 0,
        "imageSamplesAvailable": 0,
        "sampleCards": [],
        "sampleSkipped": [],
        "blockerReason": "",
        "recommendation": "",
    }


def default_full_state(run_id: str) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAtUtc": now_utc(),
        "mode": "full_catalogue",
        "completedSetKeys": [],
        "failedSetKeys": [],
        "skippedSetKeys": [],
        "lastProcessedSetKey": None,
        "requestsAttemptedTotal": 0,
        "requestsSucceededTotal": 0,
        "requestsFailedTotal": 0,
        "cardsWrittenTotal": 0,
        "languagesCompleted": {},
        "lastRunId": run_id,
    }


def load_full_state(path: Path, run_id: str) -> dict[str, Any]:
    if not path.exists():
        return default_full_state(run_id)
    try:
        data = load_json(path)
    except Exception:
        return default_full_state(run_id)
    state = default_full_state(run_id)
    state.update({key: data.get(key, value) for key, value in state.items()})
    return state


def write_full_state(path: Path, state: dict[str, Any]) -> None:
    payload = copy.deepcopy(state)
    payload["updatedAtUtc"] = now_utc()
    payload = preserve_updated_at_if_unchanged(path, payload)
    write_json(path, payload)


def add_state_item(state: dict[str, Any], field: str, value: str) -> None:
    items = state.setdefault(field, [])
    if isinstance(items, list) and value not in items:
        items.append(value)
        items.sort()


def fetch_json(url: str, *, api_key: str, timeout_seconds: int = 30) -> FetchResult:
    request = Request(
        url,
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
            "User-Agent": provider_user_agent(),
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            headers = {str(key): str(value) for key, value in response.headers.items()}
            body = response.read().decode("utf-8")
            payload = json.loads(body)
        if not isinstance(payload, dict):
            return FetchResult(False, response.status, None, headers, "response_not_object")
        return FetchResult(True, response.status, payload, headers)
    except HTTPError as exc:
        return FetchResult(False, exc.code, None, {str(k): str(v) for k, v in exc.headers.items()}, safe_error(exc))
    except Exception as exc:  # noqa: BLE001
        return FetchResult(False, None, None, {}, safe_error(exc))


def fetch_image_metadata(url: str, *, api_key: str, timeout_seconds: int = 20) -> FetchResult:
    request = Request(
        url,
        headers={
            "X-API-Key": api_key,
            "Accept": "image/*",
            "User-Agent": provider_user_agent(),
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            headers = {str(key): str(value) for key, value in response.headers.items()}
            return FetchResult(True, response.status, None, headers)
    except HTTPError as exc:
        return FetchResult(False, exc.code, None, {str(k): str(v) for k, v in exc.headers.items()}, safe_error(exc))
    except Exception as exc:  # noqa: BLE001
        return FetchResult(False, None, None, {}, safe_error(exc))


def header_value(headers: dict[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def record_request(diag: dict[str, Any], result: FetchResult, state: dict[str, Any] | None = None) -> None:
    diag["requestsAttempted"] += 1
    if state is not None:
        state["requestsAttemptedTotal"] = int(state.get("requestsAttemptedTotal") or 0) + 1
    if result.ok:
        diag["requestsSucceeded"] += 1
        if state is not None:
            state["requestsSucceededTotal"] = int(state.get("requestsSucceededTotal") or 0) + 1
    else:
        diag["requestsFailed"] += 1
        if state is not None:
            state["requestsFailedTotal"] = int(state.get("requestsFailedTotal") or 0) + 1


def list_items(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = list_items(value, *keys)
            if nested:
                return nested
    return []


def card_info(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("card_info")
    return value if isinstance(value, dict) else {}


def map_language(provider_language: str, language_map: dict[str, Any]) -> str:
    return str(language_map.get(provider_language, provider_language)).lower()


def parse_set(raw: dict[str, Any], language_map: dict[str, Any]) -> ProviderSet | None:
    set_id = str(raw.get("set_id") or raw.get("id") or "").strip()
    set_code = str(raw.get("set_code") or raw.get("code") or "").strip()
    name = str(raw.get("name") or "").strip()
    language = str(raw.get("language") or raw.get("lang") or "").strip().lower()
    if not (set_id or set_code or name):
        return None
    return ProviderSet(
        set_id=set_id,
        set_code=set_code,
        name=name,
        language=language,
        app_language=map_language(language, language_map),
        card_count=safe_int(raw.get("card_count") if raw.get("card_count") is not None else raw.get("total_cards")),
        release_date=str(raw.get("release_date")).strip() if raw.get("release_date") else None,
    )


def request_limit(config: dict[str, Any], args: argparse.Namespace | None = None, *, full: bool = False) -> int:
    if args is not None and args.max_requests is not None:
        return max(1, int(args.max_requests))
    if full:
        full_cfg = config.get("fullCatalogue") if isinstance(config.get("fullCatalogue"), dict) else {}
        return max(1, int(full_cfg.get("maxRequestsPerRun") or config.get("maxRequestsPerRun") or 500))
    return max(1, int(config.get("maxRequestsPerRun") or 500))


def sleep_seconds(config: dict[str, Any], *, full: bool = False) -> float:
    if full:
        full_cfg = config.get("fullCatalogue") if isinstance(config.get("fullCatalogue"), dict) else {}
        return max(0.0, float(full_cfg.get("requestSleepSeconds") or config.get("requestSleepSeconds") or 0.0))
    return max(0.0, float(config.get("requestSleepSeconds") or 0.0))


def fetch_sets(
    api_key: str,
    config: dict[str, Any],
    diag: dict[str, Any],
    *,
    max_requests: int,
    state: dict[str, Any] | None = None,
    full: bool = False,
) -> list[ProviderSet]:
    language_map = config.get("languageMap") if isinstance(config.get("languageMap"), dict) else {}
    delay = sleep_seconds(config, full=full)
    page = 1
    per_page = 100
    seen: set[str] = set()
    sets: list[ProviderSet] = []

    while diag["requestsAttempted"] < max_requests:
        result = fetch_json(f"{BASE_URL}/sets?page={page}&limit={per_page}", api_key=api_key)
        record_request(diag, result, state)
        if not result.ok or result.payload is None:
            detail = result.error or "request_failed"
            append_sample(diag["sampleSkipped"], {"reason": "sets_fetch_failed", "page": page, "detail": detail})
            if result.status_code == 429:
                diag["status"] = "rate_limited"
                diag["blockerReason"] = "Pokewallet returned 429 while fetching sets."
            break

        items = list_items(result.payload, "data", "results", "sets")
        if not items:
            break
        added = 0
        for item in items:
            parsed = parse_set(item, language_map)
            if parsed is None:
                continue
            key = parsed.set_id or parsed.set_code or parsed.name
            if key in seen:
                continue
            seen.add(key)
            sets.append(parsed)
            added += 1
        if added == 0 or len(items) < per_page:
            break
        page += 1
        if delay:
            time.sleep(delay)
    return sets


def public_set_record(item: ProviderSet) -> dict[str, Any]:
    return {
        "providerSetId": item.set_id,
        "providerSetCode": item.set_code,
        "providerSetName": item.name,
        "providerLanguage": item.language,
        "cardScanRLanguage": item.app_language,
        "cardCount": item.card_count,
        "releaseDate": item.release_date,
    }


def set_key_for(item: ProviderSet, *, prefer_numeric: bool) -> str:
    if prefer_numeric and item.set_id:
        return item.set_id
    return item.set_code or item.set_id or item.name


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned or "unknown"


def per_set_path(item: ProviderSet, *, prefer_numeric: bool) -> Path:
    return CARDS_DIR / safe_filename(item.app_language) / f"{safe_filename(set_key_for(item, prefer_numeric=prefer_numeric))}.json"


def select_sample_sets(sets: list[ProviderSet], config: dict[str, Any]) -> dict[str, list[ProviderSet]]:
    target_languages = {str(item).lower() for item in config.get("targetLanguages", []) if str(item).strip()}
    limits = config.get("sampleSetsPerLanguage") if isinstance(config.get("sampleSetsPerLanguage"), dict) else {}
    grouped: dict[str, list[ProviderSet]] = {}
    for item in sets:
        if target_languages and item.language not in target_languages:
            continue
        grouped.setdefault(item.app_language, []).append(item)

    selected: dict[str, list[ProviderSet]] = {}
    for language, items in sorted(grouped.items()):
        limit = max(0, int(limits.get(language, 0) or 0))
        if limit <= 0:
            continue
        selected[language] = sorted(items, key=sort_set_key)[:limit]
    return selected


def sort_set_key(item: ProviderSet) -> tuple[int, str, str, str]:
    return (0 if item.release_date else 1, item.release_date or "", item.set_code, item.set_id)


def select_full_sets(
    sets: list[ProviderSet],
    config: dict[str, Any],
    *,
    language_filter: str | None,
    all_languages: bool,
) -> dict[str, list[ProviderSet]]:
    full_cfg = config.get("fullCatalogue") if isinstance(config.get("fullCatalogue"), dict) else {}
    include_provider_languages = {str(item).lower() for item in full_cfg.get("includeLanguages", []) if str(item).strip()}
    grouped: dict[str, list[ProviderSet]] = {}
    for item in sets:
        if include_provider_languages and item.language not in include_provider_languages:
            continue
        if language_filter and item.app_language != language_filter:
            continue
        grouped.setdefault(item.app_language, []).append(item)

    if all_languages or language_filter:
        return {language: sorted(items, key=sort_set_key) for language, items in sorted(grouped.items())}

    limits = config.get("sampleSetsPerLanguage") if isinstance(config.get("sampleSetsPerLanguage"), dict) else {}
    selected: dict[str, list[ProviderSet]] = {}
    for language, items in sorted(grouped.items()):
        limit = max(0, int(limits.get(language, 0) or 0))
        if limit > 0:
            selected[language] = sorted(items, key=sort_set_key)[:limit]
    return selected


def set_detail_cards(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    set_obj = payload.get("set") if isinstance(payload.get("set"), dict) else {}
    cards = payload.get("cards")
    if not isinstance(cards, list):
        cards = payload.get("data")
    return set_obj, [item for item in cards if isinstance(item, dict)] if isinstance(cards, list) else []


def string_value(value: Any) -> str:
    return str(value or "").strip()


def normalized_key_part(value: Any) -> str:
    text = string_value(value).lower()
    cleaned = []
    previous_dash = False
    for char in text:
        if char.isalnum():
            cleaned.append(char)
            previous_dash = False
        elif not previous_dash:
            cleaned.append("-")
            previous_dash = True
    return "".join(cleaned).strip("-") or "unknown"


def variant_values(record: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in ("variant", "variant_type", "variantType", "sub_type_name", "subTypeName", "finish", "condition"):
        value = record.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in found:
            found.append(value.strip())
    for source_key in ("tcgplayer", "cardmarket", "prices"):
        source = record.get(source_key)
        containers: list[Any] = []
        if isinstance(source, dict):
            prices = source.get("prices")
            if isinstance(prices, list):
                containers.extend(prices)
            elif isinstance(prices, dict):
                containers.extend(prices.values())
        elif isinstance(source, list):
            containers.extend(source)
        for item in containers:
            if not isinstance(item, dict):
                continue
            for key in ("variant", "variant_type", "variantType", "sub_type_name", "subTypeName", "finish", "condition"):
                value = item.get(key)
                if isinstance(value, str) and value.strip() and value.strip() not in found:
                    found.append(value.strip())
    return sorted(found)


def has_price_fields(record: dict[str, Any]) -> bool:
    for key in ("price", "prices", "market_price", "marketPrice", "low_price", "lowPrice", "high_price", "highPrice", "tcgplayer", "cardmarket"):
        value = record.get(key)
        if value not in (None, "", [], {}):
            return True
    return False


def provider_card_record(record: dict[str, Any], set_item: ProviderSet, set_obj: dict[str, Any]) -> dict[str, Any]:
    info = card_info(record)
    provider_card_id = string_value(record.get("id") or info.get("id"))
    provider_set_id = string_value(info.get("set_id") or set_obj.get("set_id") or set_item.set_id)
    provider_set_code = string_value(info.get("set_code") or set_obj.get("set_code") or set_item.set_code)
    provider_set_name = string_value(info.get("set_name") or set_obj.get("name") or set_item.name)
    name = string_value(info.get("name") or record.get("name"))
    clean_name = string_value(info.get("clean_name") or record.get("clean_name"))
    number = string_value(info.get("card_number") or record.get("card_number") or record.get("number"))
    rarity = string_value(info.get("rarity") or record.get("rarity"))
    low_endpoint = f"/images/{provider_card_id}?size=low" if provider_card_id else None
    high_endpoint = f"/images/{provider_card_id}?size=high" if provider_card_id else None
    variants = variant_values(record)
    variant = normalized_key_part(variants[0] if variants else "normal")
    identity_basis = {
        "game": "pokemon",
        "language": set_item.app_language,
        "setId": provider_set_code or provider_set_id,
        "collectorNumber": number,
        "normalizedName": normalized_key_part(clean_name or name),
        "variant": variant,
        "basisConfidence": "provider_catalog_candidate",
    }
    cache_key = "|".join(
        [
            identity_basis["game"],
            identity_basis["language"],
            normalized_key_part(identity_basis["setId"]),
            normalized_key_part(identity_basis["collectorNumber"]),
            identity_basis["normalizedName"],
            identity_basis["variant"],
        ]
    )
    return {
        "providerCardId": provider_card_id,
        "providerSetId": provider_set_id,
        "providerSetCode": provider_set_code,
        "providerSetName": provider_set_name,
        "providerLanguage": set_item.language,
        "cardScanRLanguage": set_item.app_language,
        "name": name,
        "cleanName": clean_name,
        "cardNumber": number,
        "rarity": rarity,
        "variants": variants,
        "providerCanonicalImageKey": cache_key,
        "cardScanRImageCacheCandidateKey": cache_key,
        "canonicalImageKey": None,
        "imageCacheKey": cache_key,
        "imageCacheIdentityBasis": identity_basis,
        "imageEndpoint": f"/images/{provider_card_id}" if provider_card_id else None,
        "imageEndpointLow": low_endpoint,
        "imageEndpointHigh": high_endpoint,
        "imageAvailable": None,
        "imageLowAvailable": None,
        "imageHighAvailable": None,
        "imageLastCheckedAtUtc": None,
        "imageCacheStrategy": "cache_once_recheck_on_failure",
        "hasPriceFields": has_price_fields(record),
        "hasTcgplayerFields": isinstance(record.get("tcgplayer"), dict),
        "hasCardmarketFields": isinstance(record.get("cardmarket"), dict),
        "rawKeys": sorted(str(key) for key in record.keys()),
    }


def fetch_set_cards(
    *,
    api_key: str,
    set_item: ProviderSet,
    config: dict[str, Any],
    diag: dict[str, Any],
    max_requests: int,
    state: dict[str, Any] | None = None,
    full: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    delay = sleep_seconds(config, full=full)
    page = 1
    per_page = 200
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    rate_limited = False

    while diag["requestsAttempted"] < max_requests:
        set_key = set_key_for(set_item, prefer_numeric=True)
        if not set_key:
            append_sample(diag["sampleSkipped"], {"reason": "missing_set_id", "setCode": set_item.set_code, "language": set_item.language})
            break
        result = fetch_json(f"{BASE_URL}/sets/{quote(set_key, safe='')}?page={page}&limit={per_page}", api_key=api_key)
        record_request(diag, result, state)
        if not result.ok or result.payload is None:
            detail = result.error or "request_failed"
            append_sample(diag["sampleSkipped"], {"reason": "set_detail_failed", "setId": set_key, "page": page, "detail": detail})
            if result.status_code == 429:
                rate_limited = True
                diag["status"] = "rate_limited"
                diag["blockerReason"] = "Pokewallet returned 429 while fetching set details."
            break
        set_obj, page_cards = set_detail_cards(result.payload)
        if not page_cards:
            break
        added = 0
        for raw_card in page_cards:
            public_card = provider_card_record(raw_card, set_item, set_obj)
            key = public_card["providerCardId"] or f"{public_card['providerSetId']}:{public_card['cardNumber']}:{public_card['name']}"
            if key in seen:
                continue
            seen.add(key)
            cards.append(public_card)
            added += 1
        if added == 0 or len(page_cards) < per_page:
            break
        page += 1
        if delay:
            time.sleep(delay)
    return cards, rate_limited


def check_images(
    *,
    api_key: str,
    cards: list[dict[str, Any]],
    config: dict[str, Any],
    diag: dict[str, Any],
    max_requests: int,
    full: bool = False,
) -> list[dict[str, Any]]:
    cfg = config.get("fullCatalogue") if full and isinstance(config.get("fullCatalogue"), dict) else config
    sample_limit_key = "imageAvailabilitySampleLimit" if full else "imageCheckSampleLimit"
    sample_limit = max(0, int(cfg.get(sample_limit_key) or 0))
    delay = sleep_seconds(config, full=full)
    samples: list[dict[str, Any]] = []
    checked_cards = 0

    for card in cards:
        if checked_cards >= sample_limit or diag["requestsAttempted"] >= max_requests:
            break
        provider_card_id = string_value(card.get("providerCardId"))
        if not provider_card_id:
            continue
        checked_cards += 1
        for size, field in (("low", "imageLowAvailable"), ("high", "imageHighAvailable")):
            if diag["requestsAttempted"] >= max_requests:
                break
            endpoint = f"/images/{quote(provider_card_id, safe='')}?size={size}"
            result = fetch_image_metadata(f"{BASE_URL}{endpoint}", api_key=api_key)
            record_request(diag, result)
            content_type = header_value(result.headers, "Content-Type")
            available = bool(result.ok and str(content_type or "").lower().startswith("image/"))
            card[field] = available
            card["imageAvailable"] = bool(card.get("imageLowAvailable") or card.get("imageHighAvailable"))
            card["imageLastCheckedAtUtc"] = now_utc()
            diag["imageSamplesChecked"] += 1
            if available:
                diag["imageSamplesAvailable"] += 1
            append_sample(
                samples,
                {
                    "providerCardId": provider_card_id,
                    "providerSetId": card.get("providerSetId"),
                    "providerLanguage": card.get("providerLanguage"),
                    "cardScanRLanguage": card.get("cardScanRLanguage"),
                    "size": size,
                    "imageEndpoint": endpoint,
                    "statusCode": result.status_code,
                    "contentType": content_type,
                    "contentLength": safe_int(header_value(result.headers, "Content-Length")),
                    "imageAvailable": available,
                },
                limit=sample_limit * 2 if sample_limit else MAX_SAMPLE_ITEMS,
            )
            if result.status_code == 429:
                append_sample(diag["sampleSkipped"], {"reason": "image_check_rate_limited", "providerCardId": provider_card_id})
                diag["status"] = "rate_limited"
                diag["blockerReason"] = "Pokewallet returned 429 while checking image availability."
                return samples
            if delay:
                time.sleep(delay)
    return samples


def full_summary_defaults() -> dict[str, Any]:
    return {
        "fullCatalogueAvailable": bool(list(CARDS_DIR.glob("*/*.json"))) if CARDS_DIR.exists() else False,
        "fullCatalogueMode": "metadata_only",
        "perSetFilesWritten": len(list(CARDS_DIR.glob("*/*.json"))) if CARDS_DIR.exists() else 0,
        "cardsWrittenByLanguage": {},
        "setsWrittenByLanguage": {},
        "latestFullCatalogueRunAtUtc": None,
        "statePath": str(DEFAULT_FULL_STATE_PATH.relative_to(ROOT)).replace("\\", "/"),
    }


def summarize_existing_full_catalogue() -> dict[str, Any]:
    summary = full_summary_defaults()
    cards_by_language: dict[str, int] = {}
    sets_by_language: dict[str, int] = {}
    latest: str | None = None
    for path in sorted(CARDS_DIR.glob("*/*.json")) if CARDS_DIR.exists() else []:
        payload = load_json(path)
        language = string_value(payload.get("cardScanRLanguage") or path.parent.name)
        cards = payload.get("cards")
        count = len(cards) if isinstance(cards, list) else 0
        cards_by_language[language] = cards_by_language.get(language, 0) + count
        sets_by_language[language] = sets_by_language.get(language, 0) + 1
        ts = payload.get("generatedAtUtc")
        if isinstance(ts, str) and (latest is None or ts > latest):
            latest = ts
    summary["fullCatalogueAvailable"] = bool(sets_by_language)
    summary["perSetFilesWritten"] = sum(sets_by_language.values())
    summary["cardsWrittenByLanguage"] = dict(sorted(cards_by_language.items()))
    summary["setsWrittenByLanguage"] = dict(sorted(sets_by_language.items()))
    summary["latestFullCatalogueRunAtUtc"] = latest
    return summary


def provider_catalog_url(path: Path) -> str:
    return "/" + path.relative_to(PUBLIC_DIR).as_posix()


def provider_catalog_file_records() -> dict[str, list[dict[str, Any]]]:
    records_by_language: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(CARDS_DIR.glob("*/*.json")) if CARDS_DIR.exists() else []:
        payload = load_json(path)
        language = string_value(payload.get("cardScanRLanguage") or path.parent.name)
        cards = payload.get("cards")
        card_count = len(cards) if isinstance(cards, list) else 0
        record = {
            "providerSetId": string_value(payload.get("providerSetId") or path.stem),
            "providerSetCode": string_value(payload.get("providerSetCode")),
            "providerSetName": string_value(payload.get("providerSetName")),
            "cardScanRLanguage": language,
            "cardCount": card_count,
            "url": provider_catalog_url(path),
            "sha256": sha256_file(path),
            "updatedAtUtc": string_value(payload.get("generatedAtUtc")),
        }
        records_by_language.setdefault(language, []).append(record)
    for language, records in records_by_language.items():
        records_by_language[language] = sorted(
            records,
            key=lambda item: (
                string_value(item.get("providerSetId")),
                string_value(item.get("providerSetCode")),
                string_value(item.get("providerSetName")),
            ),
        )
    return dict(sorted(records_by_language.items()))


def write_provider_catalog_app_files(ts: str, *, state_path: Path | None = None) -> list[Path]:
    state_path = state_path or DEFAULT_FULL_STATE_PATH
    state = load_json(state_path) if state_path.exists() else {}
    languages_completed = state.get("languagesCompleted") if isinstance(state.get("languagesCompleted"), dict) else {}
    file_records = provider_catalog_file_records()
    all_languages = sorted(set(file_records) | {str(language) for language in languages_completed})
    languages_status: dict[str, dict[str, Any]] = {}
    manifest_languages: dict[str, dict[str, Any]] = {}
    total_set_files = 0
    total_cards = 0

    for language in all_languages:
        records = file_records.get(language, [])
        card_count = sum(int(record.get("cardCount") or 0) for record in records)
        latest = None
        for record in records:
            updated = record.get("updatedAtUtc")
            if isinstance(updated, str) and updated and (latest is None or updated > latest):
                latest = updated
        complete = bool(languages_completed.get(language, False))
        languages_status[language] = {
            "available": bool(records),
            "setFileCount": len(records),
            "cardCount": card_count,
            "complete": complete,
            "lastUpdatedAtUtc": latest,
        }
        manifest_languages[language] = {"setFiles": records}
        total_set_files += len(records)
        total_cards += card_count

    overall_status = "complete" if total_set_files and all(item["complete"] for item in languages_status.values()) else ("partial" if total_set_files else "not_available")
    notes = [
        "Provider catalogue files are identity metadata only.",
        "Images are references only; binary images are not stored here.",
        "Pricing availability is separate from catalogue availability.",
    ]
    status_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "provider": "pokewallet",
        "game": "pokemon",
        "status": overall_status,
        "binaryImagesStored": False,
        "imageReferencesAvailable": True,
        "imageStorageMode": "provider_reference_only",
        "catalogueType": "provider_metadata",
        "languages": languages_status,
        "notes": notes,
    }
    manifest_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "provider": "pokewallet",
        "game": "pokemon",
        "status": overall_status,
        "imageReferencesAvailable": True,
        "binaryImagesStored": False,
        "imageStorageMode": "provider_reference_only",
        "totalSetFiles": total_set_files,
        "totalCards": total_cards,
        "languages": manifest_languages,
    }
    status_payload = preserve_generated_at_if_unchanged(STATUS_PATH, status_payload)
    manifest_payload = preserve_generated_at_if_unchanged(CARDS_MANIFEST_PATH, manifest_payload)
    write_json(STATUS_PATH, status_payload)
    write_json(CARDS_MANIFEST_PATH, manifest_payload)
    return [STATUS_PATH, CARDS_MANIFEST_PATH]


def empty_provider_files(ts: str, full_summary: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    notes = base_notes()
    full = full_summary or summarize_existing_full_catalogue()
    return {
        "sets-summary.json": {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAtUtc": ts,
            "provider": "pokewallet",
            "game": "pokemon",
            "notes": notes,
            "setsFetched": 0,
            "languagesSeen": {},
            "sets": [],
            **full,
        },
        "languages-summary.json": {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAtUtc": ts,
            "provider": "pokewallet",
            "game": "pokemon",
            "notes": notes,
            "languages": [],
            **full,
        },
        "cards-sample.json": {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAtUtc": ts,
            "provider": "pokewallet",
            "game": "pokemon",
            "notes": notes,
            "cardCount": 0,
            "cards": [],
            **full,
        },
        "image-availability-sample.json": {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAtUtc": ts,
            "provider": "pokewallet",
            "game": "pokemon",
            "notes": notes,
            "imageSamplesChecked": 0,
            "imageSamplesAvailable": 0,
            "samples": [],
            **full,
        },
    }


def map_provider_language_for_summary(provider_language: str, language_map: dict[str, Any]) -> str:
    return str(language_map.get(provider_language, provider_language)).lower()


def write_provider_outputs(
    *,
    ts: str,
    sets: list[ProviderSet],
    selected: dict[str, list[ProviderSet]],
    cards: list[dict[str, Any]],
    image_samples: list[dict[str, Any]],
    diag: dict[str, Any],
    language_map: dict[str, Any],
    full_summary: dict[str, Any] | None = None,
) -> list[Path]:
    payloads = empty_provider_files(ts, full_summary)
    language_counts: dict[str, int] = {}
    provider_language_counts: dict[str, int] = {}
    selected_counts: dict[str, int] = {}
    card_counts: dict[str, int] = {}

    for item in sets:
        language_counts[item.app_language] = language_counts.get(item.app_language, 0) + 1
        provider_language_counts[item.language] = provider_language_counts.get(item.language, 0) + 1
    for language, items in selected.items():
        selected_counts[language] = len(items)
    for card in cards:
        language = string_value(card.get("cardScanRLanguage"))
        card_counts[language] = card_counts.get(language, 0) + 1

    payloads["sets-summary.json"].update(
        {
            "setsFetched": len(sets),
            "languagesSeen": dict(sorted(provider_language_counts.items())),
            "setsSelectedByLanguage": {
                language: sort_public_set_records([public_set_record(item) for item in items])
                for language, items in sorted(selected.items())
            },
            "sets": sort_public_set_records([public_set_record(item) for item in sorted(sets, key=sort_set_key)]),
        }
    )
    payloads["languages-summary.json"]["languages"] = [
        {
            "providerLanguages": sorted(
                language
                for language in provider_language_counts
                if language and map_provider_language_for_summary(language, language_map) == app_language
            ),
            "cardScanRLanguage": app_language,
            "setCount": language_counts.get(app_language, 0),
            "selectedSetCount": selected_counts.get(app_language, 0),
            "cardsFetched": card_counts.get(app_language, 0),
            "cardsWritten": (full_summary or {}).get("cardsWrittenByLanguage", {}).get(app_language, 0),
            "setsWritten": (full_summary or {}).get("setsWrittenByLanguage", {}).get(app_language, 0),
        }
        for app_language in sorted(language_counts)
    ]
    cards = sort_provider_cards(cards)
    sample_cards = cards[:100]
    payloads["cards-sample.json"].update({"cardCount": len(sample_cards), "cards": sample_cards})
    payloads["image-availability-sample.json"].update(
        {
            "imageSamplesChecked": diag["imageSamplesChecked"],
            "imageSamplesAvailable": diag["imageSamplesAvailable"],
            "samples": sort_image_samples(image_samples),
        }
    )

    written: list[Path] = []
    for filename, payload in payloads.items():
        path = OUTPUT_DIR / filename
        payload = preserve_generated_at_if_unchanged(path, payload)
        write_json(path, payload)
        written.append(path)
    return written


def per_set_payload(ts: str, set_item: ProviderSet, cards: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "provider": "pokewallet",
        "game": "pokemon",
        "providerLanguage": set_item.language,
        "cardScanRLanguage": set_item.app_language,
        "providerSetId": set_item.set_id,
        "providerSetCode": set_item.set_code,
        "providerSetName": set_item.name,
        "cardCount": len(cards),
        "imageReferencesOnly": True,
        "cards": cards,
    }


def write_per_set_file(ts: str, set_item: ProviderSet, cards: list[dict[str, Any]], *, prefer_numeric: bool) -> Path:
    path = per_set_path(set_item, prefer_numeric=prefer_numeric)
    payload = per_set_payload(ts, set_item, sort_provider_cards(cards))
    payload = preserve_generated_at_if_unchanged(path, payload)
    write_json(path, payload)
    return path


def index_entry(dataset_id: str, path: Path, dataset_type: str, description: str, ts: str) -> dict[str, Any]:
    entry = {
        "id": dataset_id,
        "url": f"/v1/{path.relative_to(PUBLIC_DIR).as_posix()}",
        "sha256": sha256_file(path),
        "type": dataset_type,
        "description": description,
        "updatedAtUtc": ts,
        "recommendedCacheTtlSeconds": 86400 if dataset_type != "diagnostics" else 900,
        "schemaVersion": SCHEMA_VERSION,
        "game": "pokemon",
    }
    language = path.parent.name if path.parent.parent == CARDS_DIR else None
    if language:
        entry["language"] = language
    return entry


def provider_dataset_entries(ts: str) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {
        "provider_catalog_pokewallet_sets_summary": index_entry(
            "provider_catalog_pokewallet_sets_summary",
            OUTPUT_DIR / "sets-summary.json",
            "provider_catalog",
            "Pokewallet provider catalogue set summary for CardScanR matching research",
            ts,
        ),
        "provider_catalog_pokewallet_languages_summary": index_entry(
            "provider_catalog_pokewallet_languages_summary",
            OUTPUT_DIR / "languages-summary.json",
            "provider_catalog",
            "Pokewallet provider catalogue language coverage summary",
            ts,
        ),
        "provider_catalog_pokewallet_cards_sample": index_entry(
            "provider_catalog_pokewallet_cards_sample",
            OUTPUT_DIR / "cards-sample.json",
            "provider_catalog",
            "Pokewallet provider catalogue card sample with safe metadata only",
            ts,
        ),
        "provider_catalog_pokewallet_image_availability_sample": index_entry(
            "provider_catalog_pokewallet_image_availability_sample",
            OUTPUT_DIR / "image-availability-sample.json",
            "provider_catalog",
            "Pokewallet provider catalogue image endpoint availability sample",
            ts,
        ),
        "provider_catalog_pokewallet_status": index_entry(
            "provider_catalog_pokewallet_status",
            STATUS_PATH,
            "provider_catalog_status",
            "App-facing Pokewallet provider catalogue availability status",
            ts,
        ),
        "provider_catalog_pokewallet_cards_manifest": index_entry(
            "provider_catalog_pokewallet_cards_manifest",
            CARDS_MANIFEST_PATH,
            "provider_catalog_manifest",
            "App-facing Pokewallet provider catalogue cards manifest",
            ts,
        ),
        "diagnostics_pokewallet_catalog_foundation": index_entry(
            "diagnostics_pokewallet_catalog_foundation",
            DIAG_PATH,
            "diagnostics",
            "Pokewallet catalogue foundation build diagnostics",
            ts,
        ),
    }
    if CARDS_DIR.exists():
        for path in sorted(CARDS_DIR.glob("*/*.json")):
            language = path.parent.name
            set_id = path.stem
            dataset_id = f"provider_catalog_pokewallet_cards_{language}_{set_id}"
            entries[dataset_id] = index_entry(
                dataset_id,
                path,
                "provider_catalog_cards",
                f"Pokewallet provider catalogue cards for {language} set {set_id}",
                ts,
            )
    return entries


def update_index(ts: str) -> None:
    index = load_json(INDEX_PATH)
    datasets = index.get("datasets")
    if not isinstance(datasets, list):
        raise ValueError("index.json datasets must be a list")
    previous_by_id = {str(item.get("id")): item for item in datasets if isinstance(item, dict)}
    previous_sorted = [previous_by_id[key] for key in sorted(previous_by_id)]
    by_id = dict(previous_by_id)
    for dataset_id, entry in provider_dataset_entries(ts).items():
        local_path = PUBLIC_DIR / str(entry["url"]).removeprefix("/v1/").lstrip("/")
        if local_path.exists():
            previous = previous_by_id.get(dataset_id)
            if isinstance(previous, dict) and previous.get("sha256") == entry.get("sha256") and previous.get("updatedAtUtc"):
                entry["updatedAtUtc"] = previous["updatedAtUtc"]
            by_id[dataset_id] = entry
    next_sorted = [by_id[key] for key in sorted(by_id)]
    if next_sorted == previous_sorted:
        return
    index["datasets"] = next_sorted
    index["generatedAtUtc"] = ts
    write_json(INDEX_PATH, index)


def language_counts(sets: list[ProviderSet]) -> dict[str, int]:
    return dict(sorted({language: sum(1 for item in sets if item.language == language) for language in {item.language for item in sets}}.items()))


def finish_diag(diag: dict[str, Any]) -> None:
    if diag["status"] == "ok" and diag.get("blockerReason"):
        diag["status"] = "partial"
    if diag["status"] == "ok" and diag.get("setsRemainingAfterRun", 0):
        diag["status"] = "partial"


def run_sample(args: argparse.Namespace, config: dict[str, Any], ts: str) -> dict[str, Any]:
    language_map = config.get("languageMap") if isinstance(config.get("languageMap"), dict) else {}
    api_key = os.getenv(str(config.get("apiKeyEnv") or "POKEWALLET_API_KEY"), "").strip()
    diag = base_diag(ts, bool(api_key), mode="catalogue_foundation")

    if not api_key:
        diag["status"] = "key_missing"
        diag["recommendation"] = "POKEWALLET_API_KEY is not set; provider catalogue foundation could not fetch Pokewallet data."
        write_json(DIAG_PATH, diag)
        return diag

    max_requests = request_limit(config, args)
    sets = fetch_sets(api_key, config, diag, max_requests=max_requests)
    diag["setsFetched"] = len(sets)
    diag["languagesSeen"] = language_counts(sets)
    selected = select_sample_sets(sets, config)
    diag["setsSelectedByLanguage"] = {language: [public_set_record(item) for item in items] for language, items in sorted(selected.items())}

    if args.dry_run:
        diag["status"] = "dry_run"
        diag["recommendation"] = "Dry run fetched Pokewallet set metadata only; no provider catalogue files were changed."
        write_json(DIAG_PATH, diag)
        return diag

    cards: list[dict[str, Any]] = []
    for language, items in sorted(selected.items()):
        for item in items:
            if diag["requestsAttempted"] >= max_requests:
                diag["blockerReason"] = "Request limit reached during sample catalogue build."
                append_sample(diag["sampleSkipped"], {"reason": "request_limit_reached", "language": language})
                break
            set_cards, rate_limited = fetch_set_cards(api_key=api_key, set_item=item, config=config, diag=diag, max_requests=max_requests)
            cards.extend(set_cards)
            diag["cardsFetchedByLanguage"][language] = int(diag["cardsFetchedByLanguage"].get(language) or 0) + len(set_cards)
            for card in set_cards[:3]:
                append_sample(diag["sampleCards"], card)
            if rate_limited:
                break
        if diag["status"] == "rate_limited":
            break

    image_samples: list[dict[str, Any]] = []
    if diag["status"] != "rate_limited" and not bool(config.get("writeImageFiles", False)) and bool(config.get("storeImageReferencesOnly", True)):
        image_samples = check_images(api_key=api_key, cards=cards, config=config, diag=diag, max_requests=max_requests)
    elif bool(config.get("writeImageFiles", False)):
        append_sample(diag["sampleSkipped"], {"reason": "image_check_disabled_by_config"})

    cards = sort_provider_cards(cards)
    full_summary = summarize_existing_full_catalogue()
    written = write_provider_outputs(
        ts=ts,
        sets=sets,
        selected=selected,
        cards=cards,
        image_samples=image_samples,
        diag=diag,
        language_map=language_map,
        full_summary=full_summary,
    )
    diag["recommendation"] = (
        "Pokewallet catalogue foundation wrote provider metadata samples for review; production catalogue integration can be designed next."
        if cards
        else "Pokewallet sets were fetched, but no set-detail cards were collected for the configured sample."
    )
    write_json(DIAG_PATH, diag)
    write_provider_catalog_app_files(ts)
    update_index(ts)
    return diag


def run_reset_full_state(config: dict[str, Any]) -> dict[str, Any]:
    ts = now_utc()
    full_cfg = config.get("fullCatalogue") if isinstance(config.get("fullCatalogue"), dict) else {}
    state_path = configured_path(full_cfg.get("statePath"), DEFAULT_FULL_STATE_PATH)
    state = default_full_state(str(uuid.uuid4()))
    write_full_state(state_path, state)
    diag = base_diag(ts, bool(os.getenv(str(config.get("apiKeyEnv") or "POKEWALLET_API_KEY"), "").strip()), mode="full_catalogue")
    diag["fullCatalogueEnabled"] = bool(full_cfg.get("enabled", False))
    diag["status"] = "dry_run"
    diag["recommendation"] = "Full catalogue state was reset; no provider requests were made."
    write_json(DIAG_PATH, diag)
    return diag


def run_full_catalogue(args: argparse.Namespace, config: dict[str, Any], ts: str) -> dict[str, Any]:
    full_cfg = config.get("fullCatalogue") if isinstance(config.get("fullCatalogue"), dict) else {}
    language_map = config.get("languageMap") if isinstance(config.get("languageMap"), dict) else {}
    api_key = os.getenv(str(config.get("apiKeyEnv") or "POKEWALLET_API_KEY"), "").strip()
    diag = base_diag(ts, bool(api_key), mode="full_catalogue")
    diag["fullCatalogueEnabled"] = bool(full_cfg.get("enabled", False)) or bool(args.full_catalogue)
    state_path = configured_path(full_cfg.get("statePath"), DEFAULT_FULL_STATE_PATH)
    run_id = str(uuid.uuid4())
    state = load_full_state(state_path, run_id)

    if not api_key:
        diag["status"] = "key_missing"
        diag["blockerReason"] = "POKEWALLET_API_KEY is not set."
        diag["recommendation"] = "Set POKEWALLET_API_KEY before running the full Pokewallet catalogue export."
        write_json(DIAG_PATH, diag)
        return diag

    max_requests = request_limit(config, args, full=True)
    prefer_numeric = bool(full_cfg.get("preferNumericSetId", True))
    sets = fetch_sets(api_key, config, diag, max_requests=max_requests, state=state, full=True)
    diag["setsFetched"] = len(sets)
    diag["languagesSeen"] = language_counts(sets)
    if diag["status"] == "rate_limited" and not sets:
        diag["recommendation"] = "Pokewallet returned 429; resume the full catalogue export after the provider limit resets."
        write_full_state(state_path, state)
        write_json(DIAG_PATH, diag)
        update_index(ts)
        return diag
    selected = select_full_sets(
        sets,
        config,
        language_filter=str(args.language).lower() if args.language else None,
        all_languages=bool(args.all_languages),
    )
    diag["setsSelectedByLanguage"] = {language: [public_set_record(item) for item in items[:25]] for language, items in sorted(selected.items())}

    completed = set(state.get("completedSetKeys") if isinstance(state.get("completedSetKeys"), list) else [])
    selected_items: list[tuple[str, ProviderSet]] = []
    for language, items in sorted(selected.items()):
        for item in items:
            key = set_key_for(item, prefer_numeric=prefer_numeric)
            if not key:
                add_state_item(state, "skippedSetKeys", f"missing-key:{item.name}")
                continue
            if args.resume and key in completed:
                continue
            selected_items.append((language, item))

    diag["setsRemainingAfterRun"] = len(selected_items)
    if args.dry_run:
        diag["status"] = "dry_run"
        diag["recommendation"] = "Dry run selected full-catalogue provider sets only; no per-set files were changed."
        write_json(DIAG_PATH, diag)
        write_full_state(state_path, state)
        return diag

    written_paths: list[Path] = []
    all_sample_cards: list[dict[str, Any]] = []
    cards_written_by_language: dict[str, int] = {}
    sets_written_by_language: dict[str, int] = {}

    for language, item in selected_items:
        if diag["requestsAttempted"] >= max_requests:
            diag["blockerReason"] = "Request limit reached during full catalogue export."
            diag["status"] = "partial"
            append_sample(diag["sampleSkipped"], {"reason": "request_limit_reached", "language": language})
            break

        set_key = set_key_for(item, prefer_numeric=prefer_numeric)
        state["lastRunId"] = run_id
        state["lastProcessedSetKey"] = set_key
        cards, rate_limited = fetch_set_cards(
            api_key=api_key,
            set_item=item,
            config=config,
            diag=diag,
            max_requests=max_requests,
            state=state,
            full=True,
        )
        if cards:
            if bool(full_cfg.get("imageAvailabilityCheck", False)) and not bool(full_cfg.get("writeImageFiles", False)):
                check_images(api_key=api_key, cards=cards, config=config, diag=diag, max_requests=max_requests, full=True)
            if bool(full_cfg.get("writePerSetFiles", True)):
                written_paths.append(write_per_set_file(ts, item, cards, prefer_numeric=prefer_numeric))
            diag["setsProcessedThisRun"] += 1
            diag["cardsWrittenThisRun"] += len(cards)
            diag["setFilesWritten"] += 1
            cards_written_by_language[language] = cards_written_by_language.get(language, 0) + len(cards)
            sets_written_by_language[language] = sets_written_by_language.get(language, 0) + 1
            state["cardsWrittenTotal"] = int(state.get("cardsWrittenTotal") or 0) + len(cards)
            add_state_item(state, "completedSetKeys", set_key)
            for card in cards[:3]:
                append_sample(diag["sampleCards"], card)
            all_sample_cards.extend(sort_provider_cards(cards)[:5])
        else:
            add_state_item(state, "failedSetKeys", set_key)
            append_sample(diag["sampleSkipped"], {"reason": "set_detail_no_cards", "setId": set_key, "language": language})

        write_full_state(state_path, state)
        if rate_limited or diag["status"] == "rate_limited":
            break
        if sleep_seconds(config, full=True):
            time.sleep(sleep_seconds(config, full=True))

    diag["cardsWrittenByLanguage"] = dict(sorted(cards_written_by_language.items()))
    diag["cardsFetchedByLanguage"] = dict(sorted(cards_written_by_language.items()))
    diag["setsRemainingAfterRun"] = max(0, len(selected_items) - diag["setsProcessedThisRun"])
    all_sample_cards = sort_provider_cards(all_sample_cards)

    completed_sets = set(state.get("completedSetKeys") if isinstance(state.get("completedSetKeys"), list) else [])
    languages_completed: dict[str, bool] = {}
    for language, items in selected.items():
        keys = [set_key_for(item, prefer_numeric=prefer_numeric) for item in items if set_key_for(item, prefer_numeric=prefer_numeric)]
        languages_completed[language] = bool(keys) and all(key in completed_sets for key in keys)
    state["languagesCompleted"] = languages_completed
    write_full_state(state_path, state)

    full_summary = summarize_existing_full_catalogue()
    full_summary["latestFullCatalogueRunAtUtc"] = ts if written_paths else full_summary.get("latestFullCatalogueRunAtUtc")
    full_summary["statePath"] = str(state_path.relative_to(ROOT)).replace("\\", "/") if state_path.is_relative_to(ROOT) else str(state_path)

    if written_paths:
        write_provider_outputs(
            ts=ts,
            sets=sets,
            selected=selected,
            cards=all_sample_cards,
            image_samples=[],
            diag=diag,
            language_map=language_map,
            full_summary=full_summary,
        )
    else:
        write_provider_outputs(
            ts=ts,
            sets=sets,
            selected=selected,
            cards=[],
            image_samples=[],
            diag=diag,
            language_map=language_map,
            full_summary=full_summary,
        )

    if diag["status"] == "ok" and diag["setsRemainingAfterRun"] > 0:
        diag["status"] = "partial"
        if not diag["blockerReason"]:
            diag["blockerReason"] = "Full catalogue export stopped before all selected sets were processed."
    if diag["status"] == "rate_limited":
        diag["recommendation"] = "Pokewallet returned 429; resume the full catalogue export after the provider limit resets."
    elif diag["setsRemainingAfterRun"] > 0:
        diag["recommendation"] = "Resume the full Pokewallet catalogue export until selected languages are complete."
    else:
        diag["recommendation"] = "Full Pokewallet catalogue export completed for the selected scope."
    write_json(DIAG_PATH, diag)
    write_provider_catalog_app_files(ts, state_path=state_path)
    update_index(ts)
    return diag


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Pokewallet provider catalogue foundation files.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch set metadata and write diagnostics only.")
    parser.add_argument("--full-catalogue", action="store_true", help="Run the resumable full provider catalogue export.")
    parser.add_argument("--language", type=str, default=None, help="CardScanR language filter such as en, jp, kr, zh, zh-cn, or zh-tw.")
    parser.add_argument("--all-languages", action="store_true", help="Include all configured provider languages for full catalogue export.")
    parser.add_argument("--max-requests", type=int, default=None, help="Maximum Pokewallet requests for this run.")
    parser.add_argument("--resume", action="store_true", help="Skip sets already completed in the full catalogue state file.")
    parser.add_argument("--reset-full-catalogue-state", action="store_true", help="Reset full catalogue state and exit.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config = load_json(CONFIG_PATH)
    ts = now_utc()

    if args.reset_full_catalogue_state:
        diag = run_reset_full_state(config)
        safe_log("Reset data/pokewallet_catalog_full_state.json")
        safe_log(f"status={diag['status']} requests={diag['requestsAttempted']}/{diag['requestsSucceeded']}")
        return 0

    if args.full_catalogue:
        diag = run_full_catalogue(args, config, ts)
    else:
        diag = run_sample(args, config, ts)

    safe_log(
        "status={status} setsFetched={setsFetched} requests={requestsAttempted}/{requestsSucceeded} "
        "setsProcessed={setsProcessedThisRun} cardsWritten={cardsWrittenThisRun} setFilesWritten={setFilesWritten}".format(**diag)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
