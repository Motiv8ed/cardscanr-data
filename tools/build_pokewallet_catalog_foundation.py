#!/usr/bin/env python3
"""Build app-friendly Pokewallet provider catalogue foundation files."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
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
DIAG_PATH = PUBLIC_DIR / "diagnostics" / "pokewallet-catalog-foundation-latest.json"
INDEX_PATH = PUBLIC_DIR / "index.json"
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
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


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


def base_notes() -> list[str]:
    return [
        "Pokewallet provider catalogue foundation for CardScanR matching research.",
        "Only safe provider metadata is stored.",
        "Image references are stored as API endpoints only; image files are not stored in this repository.",
        "Production catalogue integration comes later after provider coverage is reviewed.",
    ]


def base_diag(ts: str, api_key_present: bool) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "provider": "pokewallet",
        "mode": "catalogue_foundation",
        "apiKeyPresent": api_key_present,
        "requestsAttempted": 0,
        "requestsSucceeded": 0,
        "requestsFailed": 0,
        "setsFetched": 0,
        "languagesSeen": {},
        "setsSelectedByLanguage": {},
        "cardsFetchedByLanguage": {},
        "imageSamplesChecked": 0,
        "imageSamplesAvailable": 0,
        "sampleCards": [],
        "sampleSkipped": [],
        "recommendation": "",
    }


def fetch_json(url: str, *, api_key: str, timeout_seconds: int = 30) -> FetchResult:
    request = Request(
        url,
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
            "User-Agent": "CardScanR-PokeWallet-Catalog-Foundation/1.0",
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
            "User-Agent": "CardScanR-PokeWallet-Catalog-Foundation/1.0",
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


def record_request(diag: dict[str, Any], result: FetchResult) -> None:
    diag["requestsAttempted"] += 1
    if result.ok:
        diag["requestsSucceeded"] += 1
    else:
        diag["requestsFailed"] += 1


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


def fetch_sets(api_key: str, config: dict[str, Any], diag: dict[str, Any]) -> list[ProviderSet]:
    language_map = config.get("languageMap") if isinstance(config.get("languageMap"), dict) else {}
    max_requests = max(1, int(config.get("maxRequestsPerRun") or 500))
    sleep_seconds = max(0.0, float(config.get("requestSleepSeconds") or 0.0))
    page = 1
    per_page = 100
    seen: set[str] = set()
    sets: list[ProviderSet] = []

    while diag["requestsAttempted"] < max_requests:
        result = fetch_json(f"{BASE_URL}/sets?page={page}&limit={per_page}", api_key=api_key)
        record_request(diag, result)
        if not result.ok or result.payload is None:
            append_sample(diag["sampleSkipped"], {"reason": "sets_fetch_failed", "page": page, "detail": result.error})
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
        if sleep_seconds:
            time.sleep(sleep_seconds)

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


def select_sets(sets: list[ProviderSet], config: dict[str, Any]) -> dict[str, list[ProviderSet]]:
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
        selected[language] = sorted(
            items,
            key=lambda item: (
                0 if item.release_date else 1,
                item.release_date or "",
                item.set_code,
                item.set_id,
            ),
            reverse=True,
        )[:limit]
    return selected


def set_detail_cards(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    set_obj = payload.get("set") if isinstance(payload.get("set"), dict) else {}
    cards = payload.get("cards")
    if not isinstance(cards, list):
        cards = payload.get("data")
    return set_obj, [item for item in cards if isinstance(item, dict)] if isinstance(cards, list) else []


def string_value(value: Any) -> str:
    return str(value or "").strip()


def variant_values(record: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in ("variant", "variant_type", "variantType", "sub_type_name", "subTypeName", "finish", "condition"):
        value = record.get(key)
        if isinstance(value, str) and value.strip() and value not in found:
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
    return found


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
        "variants": variant_values(record),
        "imageEndpoint": f"/images/{provider_card_id}" if provider_card_id else None,
        "imageLowAvailable": None,
        "imageHighAvailable": None,
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
) -> list[dict[str, Any]]:
    max_requests = max(1, int(config.get("maxRequestsPerRun") or 500))
    sleep_seconds = max(0.0, float(config.get("requestSleepSeconds") or 0.0))
    page = 1
    per_page = 200
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()

    while diag["requestsAttempted"] < max_requests:
        set_key = set_item.set_id or set_item.set_code
        if not set_key:
            append_sample(diag["sampleSkipped"], {"reason": "missing_set_id", "setCode": set_item.set_code, "language": set_item.language})
            break
        result = fetch_json(f"{BASE_URL}/sets/{quote(set_key, safe='')}?page={page}&limit={per_page}", api_key=api_key)
        record_request(diag, result)
        if not result.ok or result.payload is None:
            append_sample(diag["sampleSkipped"], {"reason": "set_detail_failed", "setId": set_key, "page": page, "detail": result.error})
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
        if sleep_seconds:
            time.sleep(sleep_seconds)
    return cards


def check_images(
    *,
    api_key: str,
    cards: list[dict[str, Any]],
    config: dict[str, Any],
    diag: dict[str, Any],
) -> list[dict[str, Any]]:
    max_requests = max(1, int(config.get("maxRequestsPerRun") or 500))
    sample_limit = max(0, int(config.get("imageCheckSampleLimit") or 0))
    sleep_seconds = max(0.0, float(config.get("requestSleepSeconds") or 0.0))
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
                return samples
            if sleep_seconds:
                time.sleep(sleep_seconds)
    return samples


def empty_provider_files(ts: str) -> dict[str, dict[str, Any]]:
    notes = base_notes()
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
        },
        "languages-summary.json": {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAtUtc": ts,
            "provider": "pokewallet",
            "game": "pokemon",
            "notes": notes,
            "languages": [],
        },
        "cards-sample.json": {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAtUtc": ts,
            "provider": "pokewallet",
            "game": "pokemon",
            "notes": notes,
            "cardCount": 0,
            "cards": [],
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
        },
    }


def write_provider_outputs(
    *,
    ts: str,
    sets: list[ProviderSet],
    selected: dict[str, list[ProviderSet]],
    cards: list[dict[str, Any]],
    image_samples: list[dict[str, Any]],
    diag: dict[str, Any],
) -> list[Path]:
    payloads = empty_provider_files(ts)
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
                language: [public_set_record(item) for item in items]
                for language, items in sorted(selected.items())
            },
            "sets": [public_set_record(item) for item in sets],
        }
    )
    payloads["languages-summary.json"]["languages"] = [
        {
            "providerLanguages": sorted(language for language, count in provider_language_counts.items() if language and map_provider_language_for_summary(language, diag) == app_language),
            "cardScanRLanguage": app_language,
            "setCount": language_counts.get(app_language, 0),
            "selectedSetCount": selected_counts.get(app_language, 0),
            "cardsFetched": card_counts.get(app_language, 0),
        }
        for app_language in sorted(language_counts)
    ]
    payloads["cards-sample.json"].update({"cardCount": len(cards), "cards": cards})
    payloads["image-availability-sample.json"].update(
        {
            "imageSamplesChecked": diag["imageSamplesChecked"],
            "imageSamplesAvailable": diag["imageSamplesAvailable"],
            "samples": image_samples,
        }
    )

    written: list[Path] = []
    for filename, payload in payloads.items():
        path = OUTPUT_DIR / filename
        write_json(path, payload)
        written.append(path)
    return written


def map_provider_language_for_summary(provider_language: str, diag: dict[str, Any]) -> str:
    mapping = diag.get("_languageMap")
    if isinstance(mapping, dict):
        return str(mapping.get(provider_language, provider_language)).lower()
    return provider_language


def index_entry(dataset_id: str, path: Path, dataset_type: str, description: str, ts: str) -> dict[str, Any]:
    return {
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


def update_index(ts: str, written_paths: list[Path]) -> None:
    index = load_json(INDEX_PATH)
    datasets = index.get("datasets")
    if not isinstance(datasets, list):
        raise ValueError("index.json datasets must be a list")
    entries = {
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
        "diagnostics_pokewallet_catalog_foundation": index_entry(
            "diagnostics_pokewallet_catalog_foundation",
            DIAG_PATH,
            "diagnostics",
            "Pokewallet catalogue foundation build diagnostics",
            ts,
        ),
    }
    by_id = {str(item.get("id")): item for item in datasets if isinstance(item, dict)}
    for key, entry in entries.items():
        if entry["url"] and (entry["id"] == "diagnostics_pokewallet_catalog_foundation" or Path(PUBLIC_DIR / entry["url"].removeprefix("/v1/")).exists()):
            by_id[key] = entry
    index["datasets"] = [by_id[key] for key in sorted(by_id)]
    index["generatedAtUtc"] = ts
    write_json(INDEX_PATH, index)


def main() -> int:
    ts = now_utc()
    config = load_json(CONFIG_PATH)
    language_map = config.get("languageMap") if isinstance(config.get("languageMap"), dict) else {}
    api_key = os.getenv(str(config.get("apiKeyEnv") or "POKEWALLET_API_KEY"), "").strip()
    diag = base_diag(ts, bool(api_key))
    diag["_languageMap"] = language_map

    if not bool(config.get("enabled", True)):
        diag["recommendation"] = "Pokewallet catalogue foundation is disabled."
        diag.pop("_languageMap", None)
        written = write_provider_outputs(ts=ts, sets=[], selected={}, cards=[], image_samples=[], diag=diag)
        write_json(DIAG_PATH, diag)
        update_index(ts, written + [DIAG_PATH])
        safe_log("Pokewallet catalogue foundation disabled; wrote empty provider catalogue shell files.")
        return 0

    if not api_key:
        diag["recommendation"] = "POKEWALLET_API_KEY is not set; provider catalogue foundation could not fetch Pokewallet data."
        diag.pop("_languageMap", None)
        written = write_provider_outputs(ts=ts, sets=[], selected={}, cards=[], image_samples=[], diag=diag)
        write_json(DIAG_PATH, diag)
        update_index(ts, written + [DIAG_PATH])
        safe_log("POKEWALLET_API_KEY missing; wrote diagnostics and empty provider catalogue shell files.")
        return 0

    max_requests = max(1, int(config.get("maxRequestsPerRun") or 500))
    sleep_seconds = max(0.0, float(config.get("requestSleepSeconds") or 0.0))
    sets = fetch_sets(api_key, config, diag)
    diag["setsFetched"] = len(sets)
    diag["languagesSeen"] = dict(sorted({language: sum(1 for item in sets if item.language == language) for language in {item.language for item in sets}}.items()))

    selected = select_sets(sets, config)
    diag["setsSelectedByLanguage"] = {
        language: [public_set_record(item) for item in items]
        for language, items in sorted(selected.items())
    }

    cards: list[dict[str, Any]] = []
    for language, items in sorted(selected.items()):
        for item in items:
            if diag["requestsAttempted"] >= max_requests:
                append_sample(diag["sampleSkipped"], {"reason": "request_limit_reached", "language": language})
                break
            set_cards = fetch_set_cards(api_key=api_key, set_item=item, config=config, diag=diag)
            cards.extend(set_cards)
            diag["cardsFetchedByLanguage"][language] = int(diag["cardsFetchedByLanguage"].get(language) or 0) + len(set_cards)
            for card in set_cards[:3]:
                append_sample(diag["sampleCards"], card)
            if sleep_seconds:
                time.sleep(sleep_seconds)

    image_samples = []
    if not bool(config.get("writeImageFiles", False)) and bool(config.get("storeImageReferencesOnly", True)):
        image_samples = check_images(api_key=api_key, cards=cards, config=config, diag=diag)
    else:
        append_sample(diag["sampleSkipped"], {"reason": "image_check_disabled_by_config"})

    diag["recommendation"] = (
        "Pokewallet catalogue foundation wrote provider metadata samples for review; production catalogue integration can be designed next."
        if cards
        else "Pokewallet sets were fetched, but no set-detail cards were collected for the configured sample."
    )
    diag.pop("_languageMap", None)
    written = write_provider_outputs(ts=ts, sets=sets, selected=selected, cards=cards, image_samples=image_samples, diag=diag)
    write_json(DIAG_PATH, diag)
    update_index(ts, written + [DIAG_PATH])
    safe_log(
        "setsFetched={setsFetched} requests={requestsAttempted}/{requestsSucceeded} "
        "cardsFetched={cardsFetched} imageSamples={imageSamplesChecked}/{imageSamplesAvailable}".format(
            setsFetched=diag["setsFetched"],
            requestsAttempted=diag["requestsAttempted"],
            requestsSucceeded=diag["requestsSucceeded"],
            cardsFetched=sum(int(value) for value in diag["cardsFetchedByLanguage"].values()),
            imageSamplesChecked=diag["imageSamplesChecked"],
            imageSamplesAvailable=diag["imageSamplesAvailable"],
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
