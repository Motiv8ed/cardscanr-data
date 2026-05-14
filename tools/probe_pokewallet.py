#!/usr/bin/env python3
"""Safely probe PokéWallet coverage without modifying catalogue or price caches."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "data" / "provider_probe_config.json"
REPORT_PATH = ROOT / "public" / "v1" / "diagnostics" / "pokewallet-probe-latest.json"
SCHEMA_VERSION = "1.0.0"

# Confirmed from PokéWallet API docs:
# - GET /search?q=...&page=1&limit=...
# - GET /cards/:id
# Pro endpoints such as /cards/:id/price-history and /prices/:setCode are intentionally excluded.
POKEWALLET_ENDPOINTS = {
    "search": {"path": "/search", "confirmed": True},
    "card_detail": {"path": "/cards/{id}", "confirmed": True},
}

MAX_SAMPLE_RESULTS = 10
MAX_DETAIL_REQUESTS = 5


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


def base_report(*, status: str, api_key_present: bool) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": now_utc(),
        "provider": "pokewallet",
        "status": status,
        "apiKeyPresent": api_key_present,
        "requestsAttempted": 0,
        "requestsSucceeded": 0,
        "requestsFailed": 0,
        "searchTermsTested": [],
        "totalResultsFound": 0,
        "possibleJapaneseResults": 0,
        "priceResultsFound": 0,
        "sampleResults": [],
        "coverageSignals": {
            "hasJapaneseCards": False,
            "hasPrices": False,
            "hasImages": False,
            "hasSetCodes": False,
            "canMapToCanonicalId": False,
        },
        "recommendation": "",
    }


def provider_config() -> dict[str, Any]:
    config = load_json(CONFIG_PATH)
    providers = config.get("providers")
    if not isinstance(providers, dict):
        raise ValueError("provider_probe_config.json providers must be an object")
    pokewallet = providers.get("pokewallet")
    if not isinstance(pokewallet, dict):
        raise ValueError("provider_probe_config.json must define providers.pokewallet")
    return pokewallet


def endpoints_confirmed() -> bool:
    return all(bool(item.get("confirmed")) and item.get("path") for item in POKEWALLET_ENDPOINTS.values())


def fetch_json(url: str, *, api_key: str, timeout_seconds: int = 20) -> dict[str, Any]:
    request = Request(url, headers={"X-API-Key": api_key, "Accept": "application/json", "User-Agent": "CardScanR-PokeWallet-Probe/1.0"})
    with urlopen(request, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("PokéWallet response was not a JSON object")
    return data


def list_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    results = payload.get("results")
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def result_total(payload: dict[str, Any], results: list[dict[str, Any]]) -> int:
    for container_key, value_key in (("pagination", "total"), ("metadata", "total_count")):
        container = payload.get(container_key)
        if isinstance(container, dict):
            value = container.get(value_key)
            if isinstance(value, int) and value >= 0:
                return value
    return len(results)


def card_info(record: dict[str, Any]) -> dict[str, Any]:
    info = record.get("card_info")
    return info if isinstance(info, dict) else {}


def prices_present(record: dict[str, Any]) -> bool:
    for key in ("tcgplayer", "cardmarket"):
        source = record.get(key)
        if isinstance(source, dict) and isinstance(source.get("prices"), list) and source.get("prices"):
            return True
    for key in ("prices", "price", "market_price", "marketPrice"):
        value = record.get(key)
        if value not in (None, [], {}):
            return True
    return False


def currency_for(record: dict[str, Any]) -> str | None:
    if isinstance(record.get("currency"), str):
        return str(record["currency"])
    if isinstance(record.get("tcgplayer"), dict) and record["tcgplayer"].get("prices"):
        return "USD"
    if isinstance(record.get("cardmarket"), dict) and record["cardmarket"].get("prices"):
        return "EUR"
    return None


def image_present(record: dict[str, Any]) -> bool:
    info = card_info(record)
    for source in (record, info):
        for key in ("image", "images", "imageUrl", "image_url", "imageSmall", "imageLarge"):
            value = source.get(key)
            if value not in (None, "", [], {}):
                return True
    return False


def possible_japanese(record: dict[str, Any]) -> bool:
    info = card_info(record)
    language = str(record.get("language") or info.get("language") or "").lower()
    if language in {"ja", "jp", "japanese"}:
        return True
    haystack = " ".join(
        str(value or "")
        for value in (
            info.get("name"),
            info.get("clean_name"),
            info.get("set_name"),
            info.get("set_code"),
            record.get("id"),
        )
    ).lower()
    jp_signals = (
        "japanese",
        "jp",
        "mega symphonia",
        "battle partners",
        "pmcg",
        "adv",
        "sv10",
        "sv11",
    )
    if any(signal in haystack for signal in jp_signals):
        return True
    cardmarket = record.get("cardmarket")
    tcgplayer = record.get("tcgplayer")
    return isinstance(cardmarket, dict) and not isinstance(tcgplayer, dict)


def can_map_to_canonical_id(record: dict[str, Any]) -> bool:
    info = card_info(record)
    return bool(info.get("set_code") and info.get("card_number"))


def sample_result(record: dict[str, Any]) -> dict[str, Any]:
    info = card_info(record)
    price_found = prices_present(record)
    currency = currency_for(record)
    return {
        "providerId": str(record.get("id") or ""),
        "name": info.get("name") or info.get("clean_name") or record.get("name"),
        "setName": info.get("set_name") or record.get("setName") or record.get("set_name"),
        "setCode": info.get("set_code") or record.get("setCode") or record.get("set_code"),
        "number": info.get("card_number") or record.get("number") or record.get("card_number"),
        "language": record.get("language") or info.get("language"),
        "imagePresent": image_present(record),
        "pricePresent": price_found,
        "currency": currency,
        "rawKeys": sorted(str(key) for key in record.keys()),
    }


def merge_record(search_record: dict[str, Any], detail_record: dict[str, Any] | None) -> dict[str, Any]:
    if not detail_record:
        return search_record
    merged = dict(search_record)
    for key, value in detail_record.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def update_signals(report: dict[str, Any]) -> None:
    samples = report["sampleResults"]
    report["coverageSignals"] = {
        "hasJapaneseCards": report["possibleJapaneseResults"] > 0,
        "hasPrices": report["priceResultsFound"] > 0,
        "hasImages": any(bool(item.get("imagePresent")) for item in samples),
        "hasSetCodes": any(bool(item.get("setCode")) for item in samples),
        "canMapToCanonicalId": any(bool(item.get("setCode") and item.get("number")) for item in samples),
    }


def recommendation_for(report: dict[str, Any]) -> str:
    if report["status"] == "key_missing":
        return "Set POKEWALLET_API_KEY to run the safe provider probe. No catalogue or price files were changed."
    if report["status"] == "endpoint_mapping_required":
        return "Confirm PokéWallet endpoint mappings before making authenticated API requests."
    if report["status"] == "error":
        return "Probe failed before useful coverage signals were collected. Check the error field and retry later."
    signals = report["coverageSignals"]
    if signals["hasJapaneseCards"] and signals["hasPrices"] and signals["canMapToCanonicalId"]:
        return "PokéWallet looks promising for a follow-up JP gap-fill experiment, but this probe did not modify the main cache."
    if signals["hasJapaneseCards"]:
        return "PokéWallet returned possible Japanese card signals, but pricing or canonical mapping needs more review before integration."
    return "This probe did not prove PokéWallet can fill Japanese catalogue/pricing gaps."


def run_probe() -> dict[str, Any]:
    config = provider_config()
    api_key_env = str(config.get("apiKeyEnv") or "POKEWALLET_API_KEY")
    api_key = os.environ.get(api_key_env, "").strip()
    if not bool(config.get("enabled", True)):
        report = base_report(status="disabled", api_key_present=bool(api_key))
        report["recommendation"] = "PokéWallet probe is disabled in data/provider_probe_config.json."
        return report
    if bool(config.get("requiresApiKey", True)) and not api_key:
        report = base_report(status="key_missing", api_key_present=False)
        report["recommendation"] = recommendation_for(report)
        return report
    if not endpoints_confirmed():
        report = base_report(status="endpoint_mapping_required", api_key_present=bool(api_key))
        report["recommendation"] = recommendation_for(report)
        return report

    base_url = str(config.get("baseUrl") or "https://api.pokewallet.io").rstrip("/")
    terms = [str(term) for term in config.get("probeSearchTerms", []) if str(term).strip()]
    max_requests = max(0, int(config.get("maxRequests") or 0))
    sleep_seconds = max(0.0, float(config.get("requestSleepSeconds") or 0.0))
    report = base_report(status="ok", api_key_present=True)
    samples_by_id: dict[str, dict[str, Any]] = {}
    detail_records: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for term in terms:
        if report["requestsAttempted"] >= max_requests:
            break
        url = f"{base_url}{POKEWALLET_ENDPOINTS['search']['path']}?q={quote(term)}&page=1&limit=5"
        report["requestsAttempted"] += 1
        try:
            payload = fetch_json(url, api_key=api_key)
            report["requestsSucceeded"] += 1
            report["searchTermsTested"].append(term)
            results = list_results(payload)
            report["totalResultsFound"] += result_total(payload, results)
            for record in results:
                provider_id = str(record.get("id") or "")
                if provider_id and provider_id not in samples_by_id:
                    samples_by_id[provider_id] = record
        except Exception as exc:  # noqa: BLE001 - probe must degrade into diagnostics.
            report["requestsFailed"] += 1
            errors.append(f"search {term}: {exc}")
            report["status"] = "partial" if report["requestsSucceeded"] else "error"
        if sleep_seconds:
            time.sleep(sleep_seconds)

    for provider_id in list(samples_by_id.keys())[:MAX_DETAIL_REQUESTS]:
        if report["requestsAttempted"] >= max_requests:
            break
        detail_path = POKEWALLET_ENDPOINTS["card_detail"]["path"].format(id=quote(provider_id, safe=""))
        url = f"{base_url}{detail_path}"
        report["requestsAttempted"] += 1
        try:
            detail_records[provider_id] = fetch_json(url, api_key=api_key)
            report["requestsSucceeded"] += 1
        except Exception as exc:  # noqa: BLE001
            report["requestsFailed"] += 1
            errors.append(f"card {provider_id}: {exc}")
            report["status"] = "partial" if report["requestsSucceeded"] else "error"
        if sleep_seconds:
            time.sleep(sleep_seconds)

    sampled_records = [
        merge_record(record, detail_records.get(provider_id))
        for provider_id, record in list(samples_by_id.items())[:MAX_SAMPLE_RESULTS]
    ]
    report["sampleResults"] = [sample_result(record) for record in sampled_records]
    report["possibleJapaneseResults"] = sum(1 for record in sampled_records if possible_japanese(record))
    report["priceResultsFound"] = sum(1 for record in sampled_records if prices_present(record))
    if report["requestsFailed"] and report["status"] == "ok":
        report["status"] = "partial"
    if errors:
        report["errors"] = errors[:10]
    update_signals(report)
    report["recommendation"] = recommendation_for(report)
    return report


def main() -> int:
    try:
        report = run_probe()
    except Exception as exc:  # noqa: BLE001
        report = base_report(status="error", api_key_present=bool(os.environ.get("POKEWALLET_API_KEY", "").strip()))
        report["error"] = str(exc)
        report["recommendation"] = recommendation_for(report)
    write_json(REPORT_PATH, report)
    print(f"Wrote {REPORT_PATH.relative_to(ROOT)}")
    print(
        "status={status} apiKeyPresent={apiKeyPresent} requests={requestsAttempted}/{requestsSucceeded} "
        "totalResultsFound={totalResultsFound} possibleJapaneseResults={possibleJapaneseResults} priceResultsFound={priceResultsFound}".format(
            **report
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
