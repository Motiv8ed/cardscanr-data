#!/usr/bin/env python3
"""Diagnostics-only Pokewallet Pro price endpoint probe."""

from __future__ import annotations

import argparse
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
CONFIG_PATH = ROOT / "data" / "pokewallet_pro_price_config.json"
REPORT_PATH = ROOT / "public" / "v1" / "diagnostics" / "pokewallet-pro-price-probe-latest.json"
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
    "cardNumber",
    "card_number",
    "number",
    "collectorNumber",
    "collector_number",
}


@dataclass(frozen=True)
class PokewalletSet:
    set_id: str
    set_code: str
    name: str
    language: str
    card_count: int | None
    release_date: str | None


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


def fetch_json(url: str, *, api_key: str, timeout_seconds: int = 20) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
            "User-Agent": "CardScanR-PokeWallet-Pro-Price-Probe/1.0",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        body = response.read().decode("utf-8")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("Pokewallet response was not a JSON object")
    return data


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
        except Exception as exc:  # noqa: BLE001 - diagnostics should degrade safely.
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


def increment_language_counter(container: dict[str, Any], language: str, field: str) -> None:
    value = container.setdefault(language, {"attempted": 0, "succeeded": 0, "failed": 0})
    if isinstance(value, dict):
        value[field] = int(value.get(field) or 0) + 1


def extract_payload_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    for key in ("prices", "data", "results", "cards", "items"):
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
    if "tcgplayer" in record or "tcgplayer" in source_text:
        return "tcgplayer", "USD", "US"
    if "cardmarket" in record or "cardmarket" in source_text:
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
    for container_key in ("price", "prices", "tcgplayer", "cardmarket"):
        container = record.get(container_key)
        if isinstance(container, dict):
            for key in PRICE_FIELD_NAMES:
                numeric = to_float(container.get(key))
                if numeric is not None:
                    found[f"{container_key}.{key}"] = numeric
    return found


def analyze_price_response(
    *,
    payload: dict[str, Any],
    app_language: str,
    set_item: PokewalletSet,
    report: dict[str, Any],
    market_notes: dict[str, Any],
    currencies_seen: set[str],
    sources_seen: set[str],
) -> int:
    records, record_container = extract_payload_records(payload)
    append_sample(
        report["sampleResponseShapes"],
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
        return 0
    if not records:
        append_sample(report["sampleSkipped"], {"reason": "no_price_records_found", "language": app_language, "setCode": set_item.set_code})
        return 0

    useful_count = 0
    for record in records:
        source, currency, market = infer_currency_and_source(record, market_notes)
        price_values = numeric_price_fields(record)
        identifiers = collect_known_values(record, IDENTIFIER_FIELD_NAMES)

        if source:
            sources_seen.add(source)
        if currency:
            currencies_seen.add(currency)

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
                "priceFields": sorted(price_values.keys()),
                "recordKeys": sorted(str(key) for key in record.keys())[:20],
            },
        )

    return useful_count


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
            try:
                payload = fetch_json(url, api_key=api_key)
                report["requestsSucceeded"] += 1
                report["proRequestsSucceeded"] += 1
                increment_language_counter(report["priceResponsesByLanguage"], app_language, "succeeded")
            except Exception as exc:  # noqa: BLE001 - endpoint may require Pro during setup.
                report["requestsFailed"] += 1
                report["proRequestsFailed"] += 1
                increment_language_counter(report["priceResponsesByLanguage"], app_language, "failed")
                detail = safe_error(exc)
                if detail in {"http_401", "http_402", "http_403"}:
                    report["status"] = "pro_required"
                append_sample(report["sampleSkipped"], {"reason": "price_endpoint_failed", "language": app_language, "setCode": set_code, "detail": detail})
                if sleep_seconds:
                    time.sleep(sleep_seconds)
                continue

            useful_count = analyze_price_response(
                payload=payload,
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Pokewallet Pro price endpoint safely.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch sets and select probe targets without calling the Pro price endpoint.")
    parser.add_argument("--enable-pro", action="store_true", help="Allow calls to /prices/:setCode.")
    parser.add_argument("--max-requests", type=int, default=None, help="Maximum Pro price endpoint requests for this run.")
    parser.add_argument("--language", type=str, default=None, help="CardScanR language filter such as en, jp, kr, zh-cn, or zh-tw.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
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
