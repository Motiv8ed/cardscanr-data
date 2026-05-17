#!/usr/bin/env python3
"""Diagnostics-only Pokewallet Pro price and trial-discovery probes."""

from __future__ import annotations

import argparse
import json
import os
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
CONFIG_PATH = ROOT / "data" / "pokewallet_pro_price_config.json"
REPORT_PATH = ROOT / "public" / "v1" / "diagnostics" / "pokewallet-pro-price-probe-latest.json"
TRIAL_REPORT_PATH = ROOT / "public" / "v1" / "diagnostics" / "pokewallet-pro-trial-discovery-latest.json"
DEFAULT_TRIAL_STATE_PATH = ROOT / "data" / "pokewallet_pro_trial_discovery_state.json"
SCHEMA_VERSION = "1.0.0"
BASE_URL = "https://api.pokewallet.io"
MAX_SAMPLE_ITEMS = 12

PRICE_FIELD_NAMES = {
    "price",
    "marketPrice",
    "market_price",
    "lowPrice",
    "low_price",
    "highPrice",
    "high_price",
    "midPrice",
    "mid_price",
    "avg",
    "average",
    "trend",
    "low",
    "high",
    "change",
    "growth",
    "value",
}
CURRENCY_FIELD_NAMES = {"currency", "currencyCode", "currency_code"}
SOURCE_FIELD_NAMES = {"source", "provider", "market", "marketplace", "priceSource", "price_source"}
IDENTIFIER_FIELD_NAMES = {
    "id",
    "cardId",
    "card_id",
    "pokewalletId",
    "setId",
    "set_id",
    "setCode",
    "set_code",
    "groupId",
    "group_id",
    "cardNumber",
    "card_number",
    "number",
    "collectorNumber",
    "collector_number",
    "name",
}
VARIANT_FIELD_NAMES = {
    "variant",
    "variantType",
    "variant_type",
    "subTypeName",
    "sub_type_name",
    "condition",
    "finish",
}


@dataclass(frozen=True)
class PokewalletSet:
    set_id: str
    set_code: str
    name: str
    language: str
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


def safe_error(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, URLError):
        return "url_error"
    return exc.__class__.__name__


def safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def to_float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def append_sample(container: list[dict[str, Any]], item: dict[str, Any], limit: int = MAX_SAMPLE_ITEMS) -> None:
    if len(container) < limit:
        container.append(item)


def unique_append(container: list[str], value: str | None) -> None:
    if value and value not in container:
        container.append(value)
        container.sort()


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def rel_configured_path(value: Any, default_path: Path) -> Path:
    text = str(value or "").strip()
    if not text:
        return default_path
    path = Path(text)
    return path if path.is_absolute() else ROOT / path


def fetch_json_with_headers(url: str, *, api_key: str, timeout_seconds: int = 20) -> FetchResult:
    request = Request(
        url,
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
            "User-Agent": "CardScanR-PokeWallet-Pro-Price-Probe/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            headers = {str(key): str(value) for key, value in response.headers.items()}
            body = response.read().decode("utf-8")
            data = json.loads(body)
        if not isinstance(data, dict):
            return FetchResult(False, response.status, None, headers, "response_not_object")
        return FetchResult(True, response.status, data, headers)
    except HTTPError as exc:
        return FetchResult(False, exc.code, None, {str(k): str(v) for k, v in exc.headers.items()}, safe_error(exc))
    except Exception as exc:  # noqa: BLE001 - diagnostics should degrade safely.
        return FetchResult(False, None, None, {}, safe_error(exc))


def fetch_json(url: str, *, api_key: str, timeout_seconds: int = 20) -> dict[str, Any]:
    result = fetch_json_with_headers(url, api_key=api_key, timeout_seconds=timeout_seconds)
    if result.ok and result.payload is not None:
        return result.payload
    raise RuntimeError(result.error or "request_failed")


def fetch_image_metadata(url: str, *, api_key: str, timeout_seconds: int = 20) -> FetchResult:
    request = Request(
        url,
        headers={
            "X-API-Key": api_key,
            "Accept": "image/*",
            "User-Agent": "CardScanR-PokeWallet-Pro-Price-Probe/1.0",
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


def get_header(headers: dict[str, str], name: str) -> str | None:
    lower = name.lower()
    for key, value in headers.items():
        if key.lower() == lower:
            return value
    return None


def base_report(*, status: str, api_key_present: bool, mode: str = "pro_price_probe") -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": now_utc(),
        "provider": "pokewallet",
        "mode": mode,
        "status": status,
        "apiKeyPresent": api_key_present,
        "proEndpointUsed": "/prices/:setCode",
        "requestsAttempted": 0,
        "requestsSucceeded": 0,
        "requestsFailed": 0,
        "proRequestsAttempted": 0,
        "proRequestsSucceeded": 0,
        "proRequestsFailed": 0,
        "setsFetched": 0,
        "languagesSeen": {},
        "setsSelectedByLanguage": {},
        "priceResponsesByLanguage": {},
        "priceRecordsFoundByLanguage": {},
        "currenciesSeen": [],
        "sourcesSeen": [],
        "samplePriceRecords": [],
        "sampleResponseShapes": [],
        "sampleSkipped": [],
        "recommendation": "",
    }


def base_trial_report(*, status: str, api_key_present: bool) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": now_utc(),
        "provider": "pokewallet",
        "mode": "pro_trial_discovery",
        "status": status,
        "apiKeyPresent": api_key_present,
        "requestsAttempted": 0,
        "requestsSucceeded": 0,
        "requestsFailed": 0,
        "setsFetched": 0,
        "languagesSeen": {},
        "setsByLanguage": {},
        "sampleSetsByLanguage": {},
        "setsSelectedTotal": 0,
        "setsProcessedThisRun": 0,
        "setsRemainingAfterRun": 0,
        "endpointCoverage": {
            "setDetails": {},
            "prices": {},
            "statistics": {},
            "completionValue": {},
            "trending": {},
            "topCards": {},
            "priceHistory": {},
            "images": {},
        },
        "priceRecordsFoundByLanguage": {},
        "currenciesSeen": [],
        "sourcesSeen": [],
        "imageSamplesChecked": 0,
        "imageSamplesAvailable": 0,
        "priceHistorySamplesChecked": 0,
        "priceHistorySamplesWithData": 0,
        "rateLimit": {
            "limitHour": None,
            "remainingHour": None,
            "limitDay": None,
            "remainingDay": None,
            "trialExpiresAt": None,
            "trialDaysRemaining": None,
        },
        "samplePriceRecords": [],
        "sampleTrendingRecords": [],
        "sampleTopCards": [],
        "sampleImageChecks": [],
        "sampleSkipped": [],
        "recommendation": "",
    }


def default_trial_state(run_id: str) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAtUtc": now_utc(),
        "mode": "trial_discovery",
        "completedSetKeys": [],
        "failedSetKeys": [],
        "skippedSetKeys": [],
        "completedEndpointKeys": [],
        "lastProcessedSetKey": None,
        "requestsAttemptedTotal": 0,
        "requestsSucceededTotal": 0,
        "requestsFailedTotal": 0,
        "priceRecordsFoundTotal": 0,
        "imageSamplesCheckedTotal": 0,
        "priceHistorySamplesCheckedTotal": 0,
        "languagesCompleted": {},
        "lastRunId": run_id,
    }


def load_trial_state(path: Path, run_id: str) -> dict[str, Any]:
    if not path.exists():
        return default_trial_state(run_id)
    try:
        data = load_json(path)
    except Exception:
        return default_trial_state(run_id)
    state = default_trial_state(run_id)
    state.update({key: data.get(key, value) for key, value in state.items()})
    state["lastRunId"] = run_id
    return state


def persist_trial_state(path: Path, state: dict[str, Any]) -> None:
    state["updatedAtUtc"] = now_utc()
    write_json(path, state)


def add_state_item(state: dict[str, Any], field: str, value: str) -> None:
    items = state.setdefault(field, [])
    if isinstance(items, list) and value not in items:
        items.append(value)
        items.sort()


def set_key_for(item: PokewalletSet, *, prefer_numeric: bool) -> str:
    if prefer_numeric and item.set_id:
        return item.set_id
    return item.set_code or item.set_id or item.name


def parse_set_record(raw: dict[str, Any]) -> PokewalletSet | None:
    set_id = str(raw.get("set_id") or raw.get("id") or "").strip()
    set_code = str(raw.get("set_code") or raw.get("code") or "").strip()
    name = str(raw.get("name") or "").strip()
    language = str(raw.get("language") or raw.get("lang") or "").strip().lower()
    card_count = safe_int(raw.get("card_count") if raw.get("card_count") is not None else raw.get("total_cards"))
    release_date_raw = raw.get("release_date")
    release_date = str(release_date_raw).strip() if release_date_raw else None
    if not set_id and not set_code and not name:
        return None
    return PokewalletSet(
        set_id=set_id,
        set_code=set_code,
        name=name,
        language=language,
        card_count=card_count,
        release_date=release_date,
    )


def list_set_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("data", "results", "sets"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def update_rate_limit(report: dict[str, Any], headers: dict[str, str]) -> None:
    rate = report.get("rateLimit")
    if not isinstance(rate, dict):
        return
    mapping = {
        "limitHour": "X-RateLimit-Limit-Hour",
        "remainingHour": "X-RateLimit-Remaining-Hour",
        "limitDay": "X-RateLimit-Limit-Day",
        "remainingDay": "X-RateLimit-Remaining-Day",
        "trialExpiresAt": "X-Trial-Expires-At",
        "trialDaysRemaining": "X-Trial-Days-Remaining",
    }
    for field, header in mapping.items():
        value = get_header(headers, header)
        if value is None:
            continue
        rate[field] = safe_int(value) if field != "trialExpiresAt" else value


def rate_limit_stop_reason(report: dict[str, Any], trial_config: dict[str, Any]) -> str | None:
    rate = report.get("rateLimit")
    if not isinstance(rate, dict):
        return None
    remaining_hour = safe_int(rate.get("remainingHour"))
    remaining_day = safe_int(rate.get("remainingDay"))
    hour_threshold = safe_int(trial_config.get("stopWhenRemainingHourBelow")) or 0
    day_threshold = safe_int(trial_config.get("stopWhenRemainingDayBelow")) or 0
    if remaining_hour is not None and remaining_hour < hour_threshold:
        return "remaining_hour_below_safety_threshold"
    if remaining_day is not None and remaining_day < day_threshold:
        return "remaining_day_below_safety_threshold"
    return None


def record_request_result(
    *,
    report: dict[str, Any],
    state: dict[str, Any] | None,
    result: FetchResult,
    endpoint_name: str | None = None,
    record_count: int = 0,
) -> None:
    report["requestsAttempted"] += 1
    if state is not None:
        state["requestsAttemptedTotal"] = int(state.get("requestsAttemptedTotal") or 0) + 1
    if result.ok:
        report["requestsSucceeded"] += 1
        if state is not None:
            state["requestsSucceededTotal"] = int(state.get("requestsSucceededTotal") or 0) + 1
    else:
        report["requestsFailed"] += 1
        if state is not None:
            state["requestsFailedTotal"] = int(state.get("requestsFailedTotal") or 0) + 1

    if endpoint_name:
        bucket = report["endpointCoverage"].setdefault(endpoint_name, {})
        bucket["attempted"] = int(bucket.get("attempted") or 0) + 1
        bucket["succeeded" if result.ok else "failed"] = int(bucket.get("succeeded" if result.ok else "failed") or 0) + 1
        if record_count:
            bucket["recordsFound"] = int(bucket.get("recordsFound") or 0) + record_count


def fetch_sets(*, api_key: str, report: dict[str, Any], sleep_seconds: float) -> list[PokewalletSet]:
    sets: list[PokewalletSet] = []
    seen: set[str] = set()
    page = 1
    per_page = 100

    while True:
        url = f"{BASE_URL}/sets?page={page}&limit={per_page}"
        report["requestsAttempted"] += 1
        try:
            payload = fetch_json(url, api_key=api_key)
            report["requestsSucceeded"] += 1
        except Exception as exc:  # noqa: BLE001
            report["requestsFailed"] += 1
            append_sample(report["sampleSkipped"], {"reason": "sets_fetch_failed", "page": page, "detail": safe_error(exc)})
            break

        items = list_set_items(payload)
        if not items:
            break
        added_this_page = 0
        for item in items:
            parsed = parse_set_record(item)
            if parsed is None:
                continue
            key = parsed.set_id or parsed.set_code or parsed.name
            if key in seen:
                continue
            seen.add(key)
            sets.append(parsed)
            added_this_page += 1

        if added_this_page == 0 or len(items) < per_page:
            break
        page += 1
        if sleep_seconds:
            time.sleep(sleep_seconds)

    return sets


def fetch_sets_for_trial(
    *,
    api_key: str,
    report: dict[str, Any],
    state: dict[str, Any],
    sleep_seconds: float,
    trial_config: dict[str, Any],
) -> list[PokewalletSet]:
    sets: list[PokewalletSet] = []
    seen: set[str] = set()
    page = 1
    per_page = 100

    while True:
        url = f"{BASE_URL}/sets?page={page}&limit={per_page}"
        result = fetch_json_with_headers(url, api_key=api_key)
        update_rate_limit(report, result.headers)
        record_request_result(report=report, state=state, result=result)
        if not result.ok or result.payload is None:
            detail = result.error or "request_failed"
            if result.status_code == 429:
                report["status"] = "rate_limited"
            append_sample(report["sampleSkipped"], {"reason": "sets_fetch_failed", "page": page, "detail": detail})
            break

        items = list_set_items(result.payload)
        if not items:
            break
        added_this_page = 0
        for item in items:
            parsed = parse_set_record(item)
            if parsed is None:
                continue
            key = parsed.set_id or parsed.set_code or parsed.name
            if key in seen:
                continue
            seen.add(key)
            sets.append(parsed)
            added_this_page += 1

        stop_reason = rate_limit_stop_reason(report, trial_config)
        if stop_reason:
            report["status"] = "stopped_rate_limit_safety"
            append_sample(report["sampleSkipped"], {"reason": stop_reason, "endpoint": "/sets"})
            break
        if added_this_page == 0 or len(items) < per_page:
            break
        page += 1
        if sleep_seconds:
            time.sleep(sleep_seconds)

    persist_trial_state(trial_state_path_from_config(trial_config), state)
    return sets


def map_language(pokewallet_language: str, language_map: dict[str, Any]) -> str:
    return str(language_map.get(pokewallet_language, pokewallet_language)).lower()


def public_set_sample(item: PokewalletSet) -> dict[str, Any]:
    return {
        "setId": item.set_id,
        "setCode": item.set_code,
        "name": item.name,
        "pokewalletLanguage": item.language,
        "cardCount": item.card_count,
        "releaseDate": item.release_date,
    }


def select_probe_sets(
    *,
    sets: list[PokewalletSet],
    config: dict[str, Any],
    language_filter: str | None,
) -> dict[str, list[PokewalletSet]]:
    language_map = config.get("languageMap") if isinstance(config.get("languageMap"), dict) else {}
    target_languages = {str(item).lower() for item in config.get("targetLanguages", []) if str(item).strip()}
    preferred = config.get("preferredProbeSetCodes") if isinstance(config.get("preferredProbeSetCodes"), dict) else {}
    limits = config.get("probeSetLimits") if isinstance(config.get("probeSetLimits"), dict) else {}

    grouped: dict[str, list[PokewalletSet]] = {}
    for item in sets:
        if target_languages and item.language not in target_languages:
            continue
        app_language = map_language(item.language, language_map)
        if language_filter and app_language != language_filter:
            continue
        grouped.setdefault(app_language, []).append(item)

    selected: dict[str, list[PokewalletSet]] = {}
    for app_language, items in sorted(grouped.items()):
        limit = max(0, int(limits.get(app_language, 1) or 0))
        if limit <= 0:
            continue
        preferred_codes = [str(code).lower() for code in preferred.get(app_language, []) if str(code).strip()]
        ordered: list[PokewalletSet] = []
        used: set[str] = set()
        for code in preferred_codes:
            for item in items:
                key = item.set_id or item.set_code or item.name
                if key in used:
                    continue
                if item.set_code.lower() == code or item.set_id.lower() == code:
                    ordered.append(item)
                    used.add(key)
                    break
        for item in items:
            key = item.set_id or item.set_code or item.name
            if key not in used:
                ordered.append(item)
                used.add(key)
        selected[app_language] = ordered[:limit]
    return selected


def select_trial_sets(
    *,
    sets: list[PokewalletSet],
    config: dict[str, Any],
    trial_config: dict[str, Any],
    language_filter: str | None,
    all_languages: bool,
) -> dict[str, list[PokewalletSet]]:
    language_map = config.get("languageMap") if isinstance(config.get("languageMap"), dict) else {}
    include_languages = {str(item).lower() for item in as_list(trial_config.get("includeLanguages")) if str(item).strip()}
    grouped: dict[str, list[PokewalletSet]] = {}
    for item in sets:
        if include_languages and item.language not in include_languages:
            continue
        app_language = map_language(item.language, language_map)
        if language_filter and app_language != language_filter:
            continue
        grouped.setdefault(app_language, []).append(item)

    if all_languages or language_filter:
        return {language: sorted(items, key=lambda item: (item.release_date or "", item.set_code, item.set_id), reverse=True) for language, items in sorted(grouped.items())}

    limits = config.get("probeSetLimits") if isinstance(config.get("probeSetLimits"), dict) else {}
    selected: dict[str, list[PokewalletSet]] = {}
    for language, items in sorted(grouped.items()):
        limit = max(0, int(limits.get(language, 1) or 0))
        selected[language] = sorted(items, key=lambda item: (item.release_date or "", item.set_code, item.set_id), reverse=True)[:limit]
    return selected


def increment_language_counter(container: dict[str, Any], language: str, field: str) -> None:
    value = container.setdefault(language, {"attempted": 0, "succeeded": 0, "failed": 0})
    if isinstance(value, dict):
        value[field] = int(value.get(field) or 0) + 1


def extract_payload_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    for key in ("prices", "data", "results", "cards", "items", "trending", "topCards", "top_cards", "history"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)], key
        if isinstance(value, dict):
            nested_records, nested_key = extract_payload_records(value)
            if nested_records:
                return nested_records, f"{key}.{nested_key}"

    record_like = any(key in payload for key in PRICE_FIELD_NAMES | IDENTIFIER_FIELD_NAMES | {"tcgplayer", "cardmarket"})
    if record_like:
        return [payload], "top_level_record"
    return [], None


def collect_known_values(record: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key in keys:
        if key in record and record.get(key) not in (None, "", [], {}):
            found[key] = record.get(key)
    info = record.get("card_info")
    if isinstance(info, dict):
        for key in keys:
            if key in info and info.get(key) not in (None, "", [], {}):
                found[f"card_info.{key}"] = info.get(key)
    return found


def infer_currency_and_source(record: dict[str, Any], configured_market_notes: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    source_values = collect_known_values(record, SOURCE_FIELD_NAMES)
    explicit_currency = collect_known_values(record, CURRENCY_FIELD_NAMES)

    source_text = " ".join(str(value).lower() for value in source_values.values())
    if "tcgplayer" in record or "tcgplayer" in source_text or "tcg" in source_text:
        return "tcgplayer", "USD", "US"
    if "cardmarket" in record or "cardmarket" in source_text or source_text == "cm":
        return "cardmarket", "EUR", "EU"

    for source_key, details in configured_market_notes.items():
        if str(source_key).lower() in source_text and isinstance(details, dict):
            currency = details.get("currency")
            market = details.get("market")
            return str(source_key), str(currency) if currency else None, str(market) if market else None

    for value in explicit_currency.values():
        text = str(value).strip().upper()
        if len(text) == 3:
            return None, text, None
    return None, None, None


def numeric_price_fields(record: dict[str, Any]) -> dict[str, float]:
    found: dict[str, float] = {}
    for key in PRICE_FIELD_NAMES:
        if key in record:
            numeric = to_float(record.get(key))
            if numeric is not None:
                found[key] = numeric
    for container_key in ("price", "prices", "tcgplayer", "cardmarket", "value", "completionValue"):
        container = record.get(container_key)
        if isinstance(container, dict):
            for key in PRICE_FIELD_NAMES:
                numeric = to_float(container.get(key))
                if numeric is not None:
                    found[f"{container_key}.{key}"] = numeric
    return found


def find_nested_card_ids(value: Any, *, limit: int) -> list[str]:
    found: list[str] = []

    def visit(node: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            for key in ("id", "cardId", "card_id", "pokewalletId", "pokewallet_id"):
                candidate = node.get(key)
                if isinstance(candidate, (str, int)) and str(candidate).strip():
                    text = str(candidate).strip()
                    if text not in found:
                        found.append(text)
                    if len(found) >= limit:
                        return
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


def summarize_records(payload: dict[str, Any]) -> dict[str, Any]:
    records, container = extract_payload_records(payload)
    return {
        "topLevelKeys": sorted(str(key) for key in payload.keys()),
        "recordContainer": container,
        "recordCount": len(records),
        "sampleRecordKeys": sorted(str(key) for key in records[0].keys()) if records else [],
    }


def analyze_price_response(
    *,
    payload: dict[str, Any],
    app_language: str,
    set_item: PokewalletSet,
    report: dict[str, Any],
    market_notes: dict[str, Any],
    currencies_seen: set[str],
    sources_seen: set[str],
    sample_limit: int = MAX_SAMPLE_ITEMS,
) -> tuple[int, list[str]]:
    records, record_container = extract_payload_records(payload)
    append_sample(
        report.setdefault("sampleResponseShapes", []),
        {
            "language": app_language,
            "setCode": set_item.set_code,
            "topLevelKeys": sorted(str(key) for key in payload.keys()),
            "recordContainer": record_container,
            "recordCount": len(records),
            "sampleRecordKeys": sorted(str(key) for key in records[0].keys()) if records else [],
        },
    )

    if record_container is None:
        append_sample(report["sampleSkipped"], {"reason": "response_shape_unknown", "language": app_language, "setCode": set_item.set_code})
        return 0, []
    if not records:
        append_sample(report["sampleSkipped"], {"reason": "no_price_records_found", "language": app_language, "setCode": set_item.set_code})
        return 0, []

    useful_count = 0
    card_ids: list[str] = []
    for record in records:
        source, currency, market = infer_currency_and_source(record, market_notes)
        price_values = numeric_price_fields(record)
        identifiers = collect_known_values(record, IDENTIFIER_FIELD_NAMES)
        variants = collect_known_values(record, VARIANT_FIELD_NAMES)

        if source:
            sources_seen.add(source)
        if currency:
            currencies_seen.add(currency)
        for card_id in find_nested_card_ids(record, limit=sample_limit):
            if card_id not in card_ids:
                card_ids.append(card_id)

        missing_reasons: list[str] = []
        if not currency:
            missing_reasons.append("missing_currency")
        if not identifiers:
            missing_reasons.append("missing_card_identifier")
        if not price_values:
            missing_reasons.append("no_numeric_prices")

        if missing_reasons:
            for reason in missing_reasons:
                append_sample(
                    report["sampleSkipped"],
                    {
                        "reason": reason,
                        "language": app_language,
                        "setCode": set_item.set_code,
                        "recordKeys": sorted(str(key) for key in record.keys())[:20],
                    },
                )
            continue

        useful_count += 1
        append_sample(
            report["samplePriceRecords"],
            {
                "language": app_language,
                "setCode": set_item.set_code,
                "source": source or "pokewallet",
                "market": market,
                "currency": currency,
                "identifierFields": sorted(identifiers.keys()),
                "variantFields": sorted(variants.keys()),
                "priceFields": sorted(price_values.keys()),
                "hasCardNumber": any("card_number" in key or "cardNumber" in key for key in identifiers),
                "hasName": any(key.endswith("name") or key == "name" for key in identifiers),
                "hasSetOrGroupId": any(key in {"setId", "set_id", "card_info.set_id", "groupId", "group_id"} for key in identifiers),
                "recordKeys": sorted(str(key) for key in record.keys())[:20],
            },
            limit=sample_limit,
        )

    return useful_count, card_ids


def recommendation_for(report: dict[str, Any]) -> str:
    status = report.get("status")
    if status == "key_missing":
        return "Set POKEWALLET_API_KEY before running the Pokewallet Pro price probe."
    if status == "dry_run":
        if int(report.get("setsFetched") or 0) == 0 and int(report.get("requestsFailed") or 0) > 0:
            return "Dry run made no Pro price endpoint requests, but Pokewallet /sets did not return set data; retry after the provider rate window resets."
        return "Dry run selected safe probe sets only; no Pro price endpoint requests were made."
    if status == "pro_required":
        return "Pass --enable-pro after activating Pokewallet Pro to test /prices/:setCode."
    if status == "ok" and report.get("samplePriceRecords"):
        return "Pokewallet Pro price endpoint returned parseable guide price records; review diagnostics before building cache support."
    if status == "ok":
        return "Pokewallet Pro endpoint responded, but useful price records were not proven yet; inspect response shape diagnostics."
    return "Pokewallet Pro price probe failed before useful coverage was proven."


def trial_recommendation(report: dict[str, Any]) -> str:
    status = report.get("status")
    if status == "key_missing":
        return "Set POKEWALLET_API_KEY before trial discovery."
    if status == "dry_run":
        return "Dry run fetched safe set metadata and did not call Pro endpoints."
    if status == "pro_required":
        return "Activate Pokewallet Pro and rerun with --enable-pro --trial-discovery."
    if status == "rate_limited":
        return "The provider returned 429; resume after the rate window resets."
    if status == "stopped_rate_limit_safety":
        return "Discovery stopped before exhausting the provider rate window; resume later with --resume."
    if status in {"ok", "partial"}:
        return "Discovery diagnostics are ready to review before production cache integration."
    return "Trial discovery stopped before useful Pro coverage was proven."


def trial_state_path_from_config(trial_config: dict[str, Any]) -> Path:
    return rel_configured_path(trial_config.get("statePath"), DEFAULT_TRIAL_STATE_PATH)


def endpoint_key(endpoint_name: str, identifier: str) -> str:
    return f"{endpoint_name}:{identifier}"


def discovery_json_request(
    *,
    api_key: str,
    url: str,
    report: dict[str, Any],
    state: dict[str, Any],
    trial_config: dict[str, Any],
    endpoint_name: str,
    endpoint_identifier: str,
    state_path: Path,
    sample_context: dict[str, Any],
) -> FetchResult:
    result = fetch_json_with_headers(url, api_key=api_key)
    update_rate_limit(report, result.headers)
    record_request_result(report=report, state=state, result=result, endpoint_name=endpoint_name)

    key = endpoint_key(endpoint_name, endpoint_identifier)
    if result.ok:
        add_state_item(state, "completedEndpointKeys", key)
    else:
        detail = result.error or "request_failed"
        if result.status_code == 429:
            report["status"] = "rate_limited"
        elif result.status_code in {401, 402, 403}:
            report["status"] = "pro_required"
        append_sample(report["sampleSkipped"], {"reason": f"{endpoint_name}_failed", "detail": detail, **sample_context})
    stop_reason = rate_limit_stop_reason(report, trial_config)
    if stop_reason and report["status"] not in {"rate_limited", "pro_required"}:
        report["status"] = "stopped_rate_limit_safety"
        append_sample(report["sampleSkipped"], {"reason": stop_reason, **sample_context})
    persist_trial_state(state_path, state)
    return result


def summarize_non_price_payload(payload: dict[str, Any]) -> dict[str, Any]:
    numeric_fields: list[str] = []
    for key, value in payload.items():
        if to_float(value) is not None:
            numeric_fields.append(str(key))
        elif isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if to_float(nested_value) is not None:
                    numeric_fields.append(f"{key}.{nested_key}")
    summary = summarize_records(payload)
    summary["numericFields"] = sorted(numeric_fields)[:30]
    return summary


def update_set_summaries(report: dict[str, Any], sets: list[PokewalletSet], config: dict[str, Any]) -> None:
    language_map = config.get("languageMap") if isinstance(config.get("languageMap"), dict) else {}
    languages_seen: dict[str, int] = {}
    sets_by_language: dict[str, int] = {}
    samples: dict[str, list[dict[str, Any]]] = {}
    for item in sets:
        languages_seen[item.language or "unknown"] = languages_seen.get(item.language or "unknown", 0) + 1
        app_language = map_language(item.language, language_map)
        sets_by_language[app_language] = sets_by_language.get(app_language, 0) + 1
        samples.setdefault(app_language, [])
        append_sample(samples[app_language], public_set_sample(item), limit=5)
    report["languagesSeen"] = dict(sorted(languages_seen.items()))
    report["setsByLanguage"] = dict(sorted(sets_by_language.items()))
    report["sampleSetsByLanguage"] = {key: value for key, value in sorted(samples.items())}


def update_selected_summary(report: dict[str, Any], selected: dict[str, list[PokewalletSet]], state: dict[str, Any], *, prefer_numeric: bool, resume: bool) -> list[tuple[str, PokewalletSet]]:
    completed = set(as_list(state.get("completedSetKeys"))) if resume else set()
    selected_items: list[tuple[str, PokewalletSet]] = []
    by_language: dict[str, int] = {}
    for language, items in sorted(selected.items()):
        for item in items:
            key = set_key_for(item, prefer_numeric=prefer_numeric)
            if not key:
                add_state_item(state, "skippedSetKeys", f"missing-key:{item.name}")
                append_sample(report["sampleSkipped"], {"reason": "missing_set_key", "language": language, "setCode": item.set_code})
                continue
            if key in completed:
                continue
            selected_items.append((language, item))
            by_language[language] = by_language.get(language, 0) + 1
    report["setsSelectedTotal"] = len(selected_items)
    report["setsRemainingAfterRun"] = len(selected_items)
    report["priceRecordsFoundByLanguage"] = {language: 0 for language in sorted(by_language)}
    return selected_items


def collect_record_metadata(report: dict[str, Any], endpoint_name: str, payload: dict[str, Any]) -> None:
    bucket = report["endpointCoverage"].setdefault(endpoint_name, {})
    summaries = bucket.setdefault("sampleResponseShapes", [])
    if isinstance(summaries, list):
        append_sample(summaries, summarize_records(payload), limit=5)


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    config = load_json(CONFIG_PATH)
    api_key_env = str(config.get("apiKeyEnv") or "POKEWALLET_API_KEY")
    api_key = os.environ.get(api_key_env, "").strip()
    status = "dry_run" if args.dry_run else ("ok" if args.enable_pro else "pro_required")
    report = base_report(status=status, api_key_present=bool(api_key))

    if not api_key:
        report["status"] = "key_missing"
        report["recommendation"] = recommendation_for(report)
        return report

    sleep_seconds = max(0.0, float(config.get("requestSleepSeconds") or 0.0))
    max_pro_requests = args.max_requests if args.max_requests is not None else int(config.get("maxRequestsPerRun") or 25)
    max_pro_requests = max(0, int(max_pro_requests))
    language_filter = str(args.language).lower() if args.language else None

    sets = fetch_sets(api_key=api_key, report=report, sleep_seconds=sleep_seconds)
    report["setsFetched"] = len(sets)

    language_map = config.get("languageMap") if isinstance(config.get("languageMap"), dict) else {}
    languages_seen: dict[str, int] = {}
    for item in sets:
        app_language = map_language(item.language, language_map)
        languages_seen[app_language] = languages_seen.get(app_language, 0) + 1
    report["languagesSeen"] = dict(sorted(languages_seen.items()))

    selected = select_probe_sets(sets=sets, config=config, language_filter=language_filter)
    report["setsSelectedByLanguage"] = {
        language: [public_set_sample(item) for item in items]
        for language, items in sorted(selected.items())
    }
    report["priceResponsesByLanguage"] = {
        language: {"attempted": 0, "succeeded": 0, "failed": 0}
        for language in sorted(selected.keys())
    }
    report["priceRecordsFoundByLanguage"] = {language: 0 for language in sorted(selected.keys())}

    if args.dry_run or not args.enable_pro:
        report["recommendation"] = recommendation_for(report)
        return report

    pro_requests = 0
    currencies_seen: set[str] = set()
    sources_seen: set[str] = set()
    market_notes = config.get("marketNotes") if isinstance(config.get("marketNotes"), dict) else {}
    endpoint_template = str(config.get("proEndpoint") or "/prices/{setCode}")

    for app_language, items in sorted(selected.items()):
        for item in items:
            if pro_requests >= max_pro_requests:
                break
            set_code = item.set_code or item.set_id
            if not set_code:
                append_sample(report["sampleSkipped"], {"reason": "missing_set_code", "language": app_language, "setId": item.set_id})
                continue

            endpoint = endpoint_template.replace("{setCode}", quote(set_code, safe=""))
            url = f"{BASE_URL}{endpoint}"
            report["requestsAttempted"] += 1
            report["proRequestsAttempted"] += 1
            increment_language_counter(report["priceResponsesByLanguage"], app_language, "attempted")
            pro_requests += 1
            result = fetch_json_with_headers(url, api_key=api_key)
            if result.ok and result.payload is not None:
                report["requestsSucceeded"] += 1
                report["proRequestsSucceeded"] += 1
                increment_language_counter(report["priceResponsesByLanguage"], app_language, "succeeded")
            else:
                report["requestsFailed"] += 1
                report["proRequestsFailed"] += 1
                increment_language_counter(report["priceResponsesByLanguage"], app_language, "failed")
                detail = result.error or "request_failed"
                if result.status_code in {401, 402, 403}:
                    report["status"] = "pro_required"
                append_sample(report["sampleSkipped"], {"reason": "price_endpoint_failed", "language": app_language, "setCode": set_code, "detail": detail})
                if sleep_seconds:
                    time.sleep(sleep_seconds)
                continue

            useful_count, _ = analyze_price_response(
                payload=result.payload,
                app_language=app_language,
                set_item=item,
                report=report,
                market_notes=market_notes,
                currencies_seen=currencies_seen,
                sources_seen=sources_seen,
            )
            report["priceRecordsFoundByLanguage"][app_language] = int(report["priceRecordsFoundByLanguage"].get(app_language) or 0) + useful_count
            if sleep_seconds:
                time.sleep(sleep_seconds)
        if pro_requests >= max_pro_requests:
            break

    if report["status"] != "pro_required" and report["requestsFailed"] and not report["requestsSucceeded"]:
        report["status"] = "error"
    report["currenciesSeen"] = sorted(currencies_seen)
    report["sourcesSeen"] = sorted(sources_seen)
    report["recommendation"] = recommendation_for(report)
    return report


def run_trial_discovery(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    config = load_json(CONFIG_PATH)
    trial_config = config.get("trialDiscovery") if isinstance(config.get("trialDiscovery"), dict) else {}
    state_path = trial_state_path_from_config(trial_config)
    run_id = str(uuid.uuid4())

    if args.reset_trial_discovery_state:
        state = default_trial_state(run_id)
        persist_trial_state(state_path, state)
        report = base_trial_report(status="dry_run", api_key_present=bool(os.environ.get(str(config.get("apiKeyEnv") or "POKEWALLET_API_KEY"), "").strip()))
        report["recommendation"] = "Trial discovery state was reset; no provider requests were made."
        return report, state_path

    api_key_env = str(config.get("apiKeyEnv") or "POKEWALLET_API_KEY")
    api_key = os.environ.get(api_key_env, "").strip()
    status = "dry_run" if args.dry_run else ("ok" if args.enable_pro else "pro_required")
    report = base_trial_report(status=status, api_key_present=bool(api_key))
    state = load_trial_state(state_path, run_id)
    persist_trial_state(state_path, state)

    if not api_key:
        report["status"] = "key_missing"
        report["recommendation"] = trial_recommendation(report)
        write_json(TRIAL_REPORT_PATH, report)
        return report, state_path

    sleep_seconds = max(0.0, float(trial_config.get("requestSleepSeconds") or config.get("requestSleepSeconds") or 0.0))
    max_requests = args.max_requests if args.max_requests is not None else int(trial_config.get("maxRequestsPerRun") or config.get("maxRequestsPerRun") or 25)
    max_requests = max(0, int(max_requests))
    prefer_numeric = bool(trial_config.get("preferNumericSetId", True))
    sample_limit = max(1, int(trial_config.get("sampleRecordLimit") or 50))
    language_filter = str(args.language).lower() if args.language else None
    market_notes = config.get("marketNotes") if isinstance(config.get("marketNotes"), dict) else {}

    sets = fetch_sets_for_trial(
        api_key=api_key,
        report=report,
        state=state,
        sleep_seconds=sleep_seconds,
        trial_config=trial_config,
    )
    report["setsFetched"] = len(sets)
    update_set_summaries(report, sets, config)
    selected = select_trial_sets(
        sets=sets,
        config=config,
        trial_config=trial_config,
        language_filter=language_filter,
        all_languages=bool(args.all_languages),
    )
    selected_items = update_selected_summary(report, selected, state, prefer_numeric=prefer_numeric, resume=bool(args.resume))
    persist_trial_state(state_path, state)

    if args.dry_run:
        report["recommendation"] = trial_recommendation(report)
        return report, state_path
    if not args.enable_pro:
        report["status"] = "pro_required"
        report["recommendation"] = trial_recommendation(report)
        return report, state_path

    card_ids: list[dict[str, str]] = []
    currencies_seen: set[str] = set()
    sources_seen: set[str] = set()

    def can_request() -> bool:
        if report["status"] in {"rate_limited", "stopped_rate_limit_safety", "pro_required"}:
            return False
        return int(report["requestsAttempted"] or 0) < max_requests

    def remember_card_ids(ids: list[str], source_endpoint: str) -> None:
        seen = {item["cardId"] for item in card_ids}
        for card_id in ids:
            if card_id not in seen:
                card_ids.append({"cardId": card_id, "sourceEndpoint": source_endpoint})
                seen.add(card_id)

    global_endpoints = []
    if trial_config.get("testTrending", True):
        global_endpoints.extend(
            [
                ("trending", "/sets/trending", "sets-trending"),
                ("trending", "/sets/trending?period=7d", "sets-trending-7d"),
                ("trending", "/sets/trending?period=30d", "sets-trending-30d"),
            ]
        )
    if trial_config.get("testTopCards", True):
        for source in ("tcg", "cm"):
            for metric in ("price", "growth"):
                global_endpoints.append(
                    ("topCards", f"/analytics/top-cards?metric={metric}&source={source}&limit=20", f"top-cards-{metric}-{source}")
                )

    for endpoint_name, endpoint, identifier in global_endpoints:
        if not can_request():
            break
        result = discovery_json_request(
            api_key=api_key,
            url=f"{BASE_URL}{endpoint}",
            report=report,
            state=state,
            trial_config=trial_config,
            endpoint_name=endpoint_name,
            endpoint_identifier=identifier,
            state_path=state_path,
            sample_context={"endpoint": endpoint},
        )
        if result.ok and result.payload:
            collect_record_metadata(report, endpoint_name, result.payload)
            records, _ = extract_payload_records(result.payload)
            remember_card_ids(find_nested_card_ids(records, limit=sample_limit), endpoint_name)
            if endpoint_name == "trending":
                for record in records[:5]:
                    append_sample(
                        report["sampleTrendingRecords"],
                        {
                            "recordKeys": sorted(str(key) for key in record.keys())[:20],
                            "priceFields": sorted(numeric_price_fields(record).keys()),
                            "sourceFields": sorted(collect_known_values(record, SOURCE_FIELD_NAMES).keys()),
                        },
                        limit=sample_limit,
                    )
            elif endpoint_name == "topCards":
                for record in records[:5]:
                    append_sample(
                        report["sampleTopCards"],
                        {
                            "recordKeys": sorted(str(key) for key in record.keys())[:20],
                            "priceFields": sorted(numeric_price_fields(record).keys()),
                            "sourceFields": sorted(collect_known_values(record, SOURCE_FIELD_NAMES).keys()),
                        },
                        limit=sample_limit,
                    )
        if sleep_seconds:
            time.sleep(sleep_seconds)

    for app_language, item in selected_items:
        if not can_request():
            break
        set_key = set_key_for(item, prefer_numeric=prefer_numeric)
        state["lastProcessedSetKey"] = set_key
        processed_any = False

        set_detail_endpoint = f"/sets/{quote(set_key, safe='')}?page=1&limit=200"
        result = discovery_json_request(
            api_key=api_key,
            url=f"{BASE_URL}{set_detail_endpoint}",
            report=report,
            state=state,
            trial_config=trial_config,
            endpoint_name="setDetails",
            endpoint_identifier=set_key,
            state_path=state_path,
            sample_context={"language": app_language, "setKey": set_key},
        )
        if result.ok and result.payload:
            processed_any = True
            collect_record_metadata(report, "setDetails", result.payload)
            remember_card_ids(find_nested_card_ids(result.payload, limit=sample_limit), "setDetails")
        if sleep_seconds:
            time.sleep(sleep_seconds)

        if trial_config.get("testPrices", True):
            for suffix, label in [("", "all"), ("?source=tcg", "tcg"), ("?source=cm", "cm")]:
                if not can_request():
                    break
                endpoint = f"/prices/{quote(set_key, safe='')}{suffix}"
                result = discovery_json_request(
                    api_key=api_key,
                    url=f"{BASE_URL}{endpoint}",
                    report=report,
                    state=state,
                    trial_config=trial_config,
                    endpoint_name="prices",
                    endpoint_identifier=f"{set_key}:{label}",
                    state_path=state_path,
                    sample_context={"language": app_language, "setKey": set_key, "sourceVariant": label},
                )
                if result.ok and result.payload:
                    processed_any = True
                    collect_record_metadata(report, "prices", result.payload)
                    useful_count, ids = analyze_price_response(
                        payload=result.payload,
                        app_language=app_language,
                        set_item=item,
                        report=report,
                        market_notes=market_notes,
                        currencies_seen=currencies_seen,
                        sources_seen=sources_seen,
                        sample_limit=sample_limit,
                    )
                    report["priceRecordsFoundByLanguage"][app_language] = int(report["priceRecordsFoundByLanguage"].get(app_language) or 0) + useful_count
                    state["priceRecordsFoundTotal"] = int(state.get("priceRecordsFoundTotal") or 0) + useful_count
                    remember_card_ids(ids, "prices")
                if sleep_seconds:
                    time.sleep(sleep_seconds)

        if trial_config.get("testSetStatistics", True) and can_request():
            endpoint = f"/sets/{quote(set_key, safe='')}/statistics"
            result = discovery_json_request(
                api_key=api_key,
                url=f"{BASE_URL}{endpoint}",
                report=report,
                state=state,
                trial_config=trial_config,
                endpoint_name="statistics",
                endpoint_identifier=set_key,
                state_path=state_path,
                sample_context={"language": app_language, "setKey": set_key},
            )
            if result.ok and result.payload:
                processed_any = True
                bucket = report["endpointCoverage"].setdefault("statistics", {})
                append_sample(bucket.setdefault("sampleSummaries", []), summarize_non_price_payload(result.payload), limit=5)
            if sleep_seconds:
                time.sleep(sleep_seconds)

        if trial_config.get("testCompletionValue", True) and can_request():
            endpoint = f"/sets/{quote(set_key, safe='')}/completion-value"
            result = discovery_json_request(
                api_key=api_key,
                url=f"{BASE_URL}{endpoint}",
                report=report,
                state=state,
                trial_config=trial_config,
                endpoint_name="completionValue",
                endpoint_identifier=set_key,
                state_path=state_path,
                sample_context={"language": app_language, "setKey": set_key},
            )
            if result.ok and result.payload:
                processed_any = True
                bucket = report["endpointCoverage"].setdefault("completionValue", {})
                append_sample(bucket.setdefault("sampleSummaries", []), summarize_non_price_payload(result.payload), limit=5)
                for source, currency, _market in [infer_currency_and_source(result.payload, market_notes)]:
                    if source:
                        sources_seen.add(source)
                    if currency:
                        currencies_seen.add(currency)
            if sleep_seconds:
                time.sleep(sleep_seconds)

        if processed_any:
            report["setsProcessedThisRun"] = int(report["setsProcessedThisRun"] or 0) + 1
            add_state_item(state, "completedSetKeys", set_key)
        else:
            add_state_item(state, "failedSetKeys", set_key)
        persist_trial_state(state_path, state)

    history_limit = max(0, int(trial_config.get("priceHistorySampleLimit") or 25))
    if trial_config.get("testPriceHistorySamples", True) and history_limit:
        for item in card_ids[:history_limit]:
            if not can_request():
                break
            card_id = item["cardId"]
            endpoint = f"/cards/{quote(card_id, safe='')}/price-history"
            result = discovery_json_request(
                api_key=api_key,
                url=f"{BASE_URL}{endpoint}",
                report=report,
                state=state,
                trial_config=trial_config,
                endpoint_name="priceHistory",
                endpoint_identifier=card_id,
                state_path=state_path,
                sample_context={"cardId": card_id, "sourceEndpoint": item["sourceEndpoint"]},
            )
            report["priceHistorySamplesChecked"] = int(report["priceHistorySamplesChecked"] or 0) + 1
            state["priceHistorySamplesCheckedTotal"] = int(state.get("priceHistorySamplesCheckedTotal") or 0) + 1
            if result.ok and result.payload:
                records, _ = extract_payload_records(result.payload)
                if records:
                    report["priceHistorySamplesWithData"] = int(report["priceHistorySamplesWithData"] or 0) + 1
                bucket = report["endpointCoverage"].setdefault("priceHistory", {})
                append_sample(bucket.setdefault("sampleSummaries", []), summarize_records(result.payload), limit=5)
            persist_trial_state(state_path, state)
            if sleep_seconds:
                time.sleep(sleep_seconds)

    image_limit = max(0, int(trial_config.get("imageSampleLimit") or 25))
    if trial_config.get("testImageSamples", True) and image_limit:
        for item in card_ids[:image_limit]:
            if not can_request():
                break
            card_id = item["cardId"]
            result = fetch_image_metadata(f"{BASE_URL}/images/{quote(card_id, safe='')}?size=low", api_key=api_key)
            update_rate_limit(report, result.headers)
            record_request_result(report=report, state=state, result=result, endpoint_name="images")
            report["imageSamplesChecked"] = int(report["imageSamplesChecked"] or 0) + 1
            state["imageSamplesCheckedTotal"] = int(state.get("imageSamplesCheckedTotal") or 0) + 1
            image_available = bool(result.ok and str(get_header(result.headers, "Content-Type") or "").lower().startswith("image/"))
            if image_available:
                report["imageSamplesAvailable"] = int(report["imageSamplesAvailable"] or 0) + 1
            append_sample(
                report["sampleImageChecks"],
                {
                    "cardId": card_id,
                    "sourceEndpoint": item["sourceEndpoint"],
                    "statusCode": result.status_code,
                    "contentType": get_header(result.headers, "Content-Type"),
                    "contentLength": safe_int(get_header(result.headers, "Content-Length")),
                    "imageAvailable": image_available,
                },
                limit=sample_limit,
            )
            if result.status_code == 429:
                report["status"] = "rate_limited"
            stop_reason = rate_limit_stop_reason(report, trial_config)
            if stop_reason and report["status"] != "rate_limited":
                report["status"] = "stopped_rate_limit_safety"
                append_sample(report["sampleSkipped"], {"reason": stop_reason, "endpoint": "/images/:id"})
            persist_trial_state(state_path, state)
            if sleep_seconds:
                time.sleep(sleep_seconds)

    completed_languages: dict[str, bool] = {}
    completed_sets = set(as_list(state.get("completedSetKeys")))
    for language, items in selected.items():
        keys = [set_key_for(item, prefer_numeric=prefer_numeric) for item in items if set_key_for(item, prefer_numeric=prefer_numeric)]
        completed_languages[language] = bool(keys) and all(key in completed_sets for key in keys)
    state["languagesCompleted"] = completed_languages
    report["setsRemainingAfterRun"] = max(0, report["setsSelectedTotal"] - report["setsProcessedThisRun"])
    state["lastRunId"] = run_id
    persist_trial_state(state_path, state)

    report["currenciesSeen"] = sorted(currencies_seen)
    report["sourcesSeen"] = sorted(sources_seen)
    if report["status"] not in {"rate_limited", "stopped_rate_limit_safety", "pro_required"}:
        report["status"] = "partial" if report["requestsFailed"] else "ok"
    report["recommendation"] = trial_recommendation(report)
    return report, state_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Pokewallet Pro pricing and trial-discovery endpoints safely.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch sets and select probe targets without calling Pro endpoints.")
    parser.add_argument("--enable-pro", action="store_true", help="Allow calls to documented Pro endpoints.")
    parser.add_argument("--max-requests", type=int, default=None, help="Maximum provider requests for this run.")
    parser.add_argument("--language", type=str, default=None, help="CardScanR language filter such as en, jp, kr, zh-cn, or zh-tw.")
    parser.add_argument("--trial-discovery", action="store_true", help="Run the broad diagnostics-only Pro trial discovery mode.")
    parser.add_argument("--all-languages", action="store_true", help="Include all configured target languages in trial discovery.")
    parser.add_argument("--resume", action="store_true", help="Skip sets already completed in the trial discovery state file.")
    parser.add_argument("--reset-trial-discovery-state", action="store_true", help="Reset trial discovery state and exit.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.trial_discovery or args.reset_trial_discovery_state:
        try:
            report, state_path = run_trial_discovery(args)
        except Exception as exc:  # noqa: BLE001
            report = base_trial_report(status="error", api_key_present=bool(os.environ.get("POKEWALLET_API_KEY", "").strip()))
            report["sampleSkipped"].append({"reason": "trial_discovery_failed", "detail": safe_error(exc)})
            report["recommendation"] = trial_recommendation(report)
            state_path = DEFAULT_TRIAL_STATE_PATH
        write_json(TRIAL_REPORT_PATH, report)
        safe_log(f"Wrote {TRIAL_REPORT_PATH.relative_to(ROOT)}")
        safe_log(f"State path: {state_path.relative_to(ROOT)}")
        safe_log(
            "status={status} apiKeyPresent={apiKeyPresent} setsFetched={setsFetched} "
            "requests={requestsAttempted}/{requestsSucceeded} setsProcessed={setsProcessedThisRun}".format(**report)
        )
        return 0

    try:
        report = run_probe(args)
    except Exception as exc:  # noqa: BLE001
        report = base_report(status="error", api_key_present=bool(os.environ.get("POKEWALLET_API_KEY", "").strip()))
        report["sampleSkipped"].append({"reason": "probe_failed", "detail": safe_error(exc)})
        report["recommendation"] = recommendation_for(report)

    write_json(REPORT_PATH, report)
    safe_log(f"Wrote {REPORT_PATH.relative_to(ROOT)}")
    safe_log(
        "status={status} apiKeyPresent={apiKeyPresent} setsFetched={setsFetched} "
        "requests={requestsAttempted}/{requestsSucceeded} proRequests={proRequestsAttempted}/{proRequestsSucceeded}".format(**report)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
