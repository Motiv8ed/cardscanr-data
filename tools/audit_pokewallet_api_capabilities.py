#!/usr/bin/env python3
"""Audit safe PokeWallet API capabilities without modifying app data caches."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent.parent
REPORT_JSON_PATH = ROOT / "reports" / "pokewallet_api_capability_audit_latest.json"
REPORT_MD_PATH = ROOT / "reports" / "pokewallet_api_capability_audit_latest.md"
CONFIG_PATH = ROOT / "data" / "pokewallet_catalog_config.json"
SETS_SUMMARY_PATH = ROOT / "public" / "v1" / "provider-catalog" / "pokewallet" / "sets-summary.json"
CARDS_SAMPLE_PATH = ROOT / "public" / "v1" / "provider-catalog" / "pokewallet" / "cards-sample.json"

SCHEMA_VERSION = "1.0.0"
BASE_URL = "https://api.pokewallet.io"
TIMEOUT_SECONDS = 20
USER_AGENT = "CardScanR-PokeWallet-Capability-Audit/1.0"
MAX_ERROR_SNIPPET_LENGTH = 180
MAX_SCAN_NODES = 5000
MAX_SAMPLE_ITEMS = 8

PRICE_FIELD_RE = re.compile(r"(price|market|low|high|mid|avg|average|trend|value)", re.IGNORECASE)
CURRENCY_KEYS = {"currency", "currencyCode", "currency_code"}
SOURCE_KEYS = {"source", "provider", "market", "marketplace", "priceSource", "price_source"}
PRO_ENDPOINT_NAMES = {
    "prices_en",
    "prices_jp",
    "prices_en_source_tcg",
    "prices_en_source_cm",
    "card_price_history",
    "sets_trending",
    "analytics_top_cards",
}


@dataclass(frozen=True)
class EndpointSpec:
    name: str
    label: str
    path: str
    query: dict[str, str] | None = None
    requires_auth: bool = True
    expected_pro: bool = False


@dataclass
class FetchResult:
    ok: bool
    status_code: int | None
    headers: dict[str, str]
    payload: Any = None
    error: str | None = None
    error_snippet: str | None = None


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def try_load_json(path: Path) -> dict[str, Any]:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "backslashreplace").decode("ascii"))


def read_configured_api_env_names() -> list[str]:
    names = ["POKEWALLET_API_KEY", "CARDSCANR_POKEWALLET_API_KEY"]
    config = try_load_json(CONFIG_PATH)
    configured = str(config.get("apiKeyEnv") or "").strip()
    if configured:
        names.insert(0, configured)
    result: list[str] = []
    for name in names:
        if name and name not in result:
            result.append(name)
    return result


def resolve_api_key() -> tuple[str, str | None, list[str]]:
    checked = read_configured_api_env_names()
    for env_name in checked:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value, env_name, checked
    return "", None, checked


def sanitize_text(value: str | None, api_key: str = "") -> str | None:
    if value is None:
        return None
    text = value.replace("\r", " ").replace("\n", " ").strip()
    if api_key:
        text = text.replace(api_key, "[redacted]")
    if len(text) > MAX_ERROR_SNIPPET_LENGTH:
        text = text[: MAX_ERROR_SNIPPET_LENGTH - 3] + "..."
    return text


def http_error_name(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, URLError):
        return "url_error"
    return exc.__class__.__name__


def build_url(path: str, query: dict[str, str] | None = None) -> str:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    return url


def response_headers(response: Any) -> dict[str, str]:
    return {str(key): str(value) for key, value in response.headers.items()}


def fetch_json_endpoint(spec: EndpointSpec, *, api_key: str) -> FetchResult:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if spec.requires_auth and api_key:
        headers["X-API-Key"] = api_key
    request = Request(build_url(spec.path, spec.query), headers=headers)
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
            payload = json.loads(body) if body else None
            return FetchResult(True, response.status, response_headers(response), payload=payload)
    except HTTPError as exc:
        snippet = sanitize_text(exc.read(2048).decode("utf-8", errors="replace"), api_key)
        return FetchResult(False, exc.code, response_headers(exc), error=http_error_name(exc), error_snippet=snippet)
    except Exception as exc:  # noqa: BLE001 - audit should degrade to reportable diagnostics.
        return FetchResult(False, None, {}, error=http_error_name(exc), error_snippet=sanitize_text(str(exc), api_key))


def fetch_image_metadata(endpoint: str, *, api_key: str) -> FetchResult:
    headers = {
        "Accept": "image/*",
        "User-Agent": USER_AGENT,
        "Range": "bytes=0-0",
    }
    if api_key:
        headers["X-API-Key"] = api_key
    request = Request(build_url(endpoint), headers=headers)
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            # Read at most a tiny probe chunk so no image is stored or fully buffered by us.
            response.read(1)
            return FetchResult(True, response.status, response_headers(response))
    except HTTPError as exc:
        snippet = sanitize_text(exc.read(256).decode("utf-8", errors="replace"), api_key)
        return FetchResult(False, exc.code, response_headers(exc), error=http_error_name(exc), error_snippet=snippet)
    except Exception as exc:  # noqa: BLE001
        return FetchResult(False, None, {}, error=http_error_name(exc), error_snippet=sanitize_text(str(exc), api_key))


def get_header(headers: dict[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def safe_int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def list_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "results", "sets", "cards", "prices", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def response_shape(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        items = list_items(payload)
        pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
        return {
            "topLevelType": "object",
            "topLevelKeys": sorted(str(key) for key in payload.keys())[:30],
            "itemCount": len(items),
            "sampleItemKeys": sorted(str(key) for key in items[0].keys())[:30] if items and isinstance(items[0], dict) else [],
            "pagination": {
                "page": pagination.get("page"),
                "limit": pagination.get("limit"),
                "total": pagination.get("total"),
                "totalPages": pagination.get("totalPages"),
            }
            if pagination
            else {},
        }
    if isinstance(payload, list):
        return {
            "topLevelType": "list",
            "itemCount": len(payload),
            "sampleItemKeys": sorted(str(key) for key in payload[0].keys())[:30] if payload and isinstance(payload[0], dict) else [],
        }
    return {"topLevelType": type(payload).__name__, "itemCount": 0}


def scan_price_signals(payload: Any) -> dict[str, Any]:
    currencies: set[str] = set()
    sources: set[str] = set()
    price_field_count = 0
    cardmarket_signal_count = 0
    tcgplayer_signal_count = 0
    nodes_seen = 0
    stack: list[Any] = [payload]

    while stack and nodes_seen < MAX_SCAN_NODES:
        node = stack.pop()
        nodes_seen += 1
        if isinstance(node, dict):
            lowered_keys = {str(key).lower() for key in node.keys()}
            if "cardmarket" in lowered_keys:
                cardmarket_signal_count += 1
            if "tcgplayer" in lowered_keys:
                tcgplayer_signal_count += 1
            for key, value in node.items():
                key_str = str(key)
                if key_str in CURRENCY_KEYS and isinstance(value, str) and value:
                    currencies.add(value.upper())
                if key_str in SOURCE_KEYS and isinstance(value, str) and value:
                    sources.add(value)
                if PRICE_FIELD_RE.search(key_str) and isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    price_field_count += 1
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(node[:200])

    return {
        "hasUsablePrices": price_field_count > 0,
        "priceFieldCount": price_field_count,
        "currencies": sorted(currencies),
        "sources": sorted(sources),
        "cardmarketSignalCount": cardmarket_signal_count,
        "tcgplayerSignalCount": tcgplayer_signal_count,
        "scanNodeLimitReached": nodes_seen >= MAX_SCAN_NODES,
    }


def classify_result(spec: EndpointSpec, result: FetchResult) -> str:
    if result.ok:
        return "available"
    if result.status_code == 429:
        return "rate_limited"
    if result.status_code == 404:
        return "not_found"
    if result.status_code in {401, 403}:
        haystack = f"{result.error or ''} {result.error_snippet or ''}".lower()
        if spec.expected_pro or spec.name in PRO_ENDPOINT_NAMES or any(token in haystack for token in ("pro", "trial", "plan", "subscription", "upgrade")):
            return "requires_pro_or_trial"
        return "forbidden_or_auth_failed"
    if result.status_code is None:
        return "request_failed"
    if result.status_code >= 500:
        return "server_error"
    return "unavailable"


def summarize_endpoint(spec: EndpointSpec, result: FetchResult) -> dict[str, Any]:
    price_signals = scan_price_signals(result.payload) if result.ok else {
        "hasUsablePrices": False,
        "priceFieldCount": 0,
        "currencies": [],
        "sources": [],
        "cardmarketSignalCount": 0,
        "tcgplayerSignalCount": 0,
        "scanNodeLimitReached": False,
    }
    return {
        "name": spec.name,
        "label": spec.label,
        "method": "GET",
        "path": spec.path,
        "query": spec.query or {},
        "requiresAuth": spec.requires_auth,
        "expectedPro": spec.expected_pro,
        "statusCode": result.status_code,
        "available": result.ok,
        "availability": classify_result(spec, result),
        "contentType": get_header(result.headers, "Content-Type"),
        "responseShape": response_shape(result.payload) if result.ok else {},
        "priceSignals": price_signals,
        "error": result.error,
        "errorSnippet": result.error_snippet,
    }


def provider_sets() -> list[dict[str, Any]]:
    payload = try_load_json(SETS_SUMMARY_PATH)
    items: list[dict[str, Any]] = []
    raw_sets = payload.get("sets")
    if isinstance(raw_sets, list):
        items.extend(item for item in raw_sets if isinstance(item, dict))
    grouped = payload.get("setsByLanguage")
    if isinstance(grouped, dict):
        for values in grouped.values():
            if isinstance(values, list):
                items.extend(item for item in values if isinstance(item, dict))

    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        key = (str(item.get("cardScanRLanguage") or ""), str(item.get("providerSetId") or item.get("providerSetCode") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def numeric_set_id(item: dict[str, Any]) -> int | None:
    raw = str(item.get("providerSetId") or item.get("set_id") or item.get("id") or "").strip()
    if not raw or not raw.lstrip("-").isdigit():
        return None
    value = int(raw)
    return value if value > 0 else None


def choose_set_sample(language: str, preferred_ids: set[str], preferred_codes: set[str]) -> dict[str, Any] | None:
    candidates = [
        item
        for item in provider_sets()
        if str(item.get("cardScanRLanguage") or "").lower() == language
        and numeric_set_id(item) is not None
        and safe_int(item.get("cardCount")) not in (None, 0)
    ]
    for item in candidates:
        if str(item.get("providerSetId") or "") in preferred_ids:
            return item
    for item in candidates:
        if str(item.get("providerSetCode") or "").upper() in preferred_codes:
            return item
    return sorted(candidates, key=lambda item: numeric_set_id(item) or 0)[0] if candidates else None


def sample_cards() -> list[dict[str, Any]]:
    payload = try_load_json(CARDS_SAMPLE_PATH)
    cards = payload.get("cards")
    if isinstance(cards, list):
        return [item for item in cards if isinstance(item, dict)]
    return []


def choose_card_sample() -> dict[str, Any] | None:
    for card in sample_cards():
        if isinstance(card.get("providerCardId"), str) and card["providerCardId"]:
            return card
    return None


def image_sample_cards(limit: int = 3) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for card in sample_cards():
        if not isinstance(card.get("providerCardId"), str) or not card["providerCardId"]:
            continue
        result.append(card)
        if len(result) >= limit:
            break
    return result


def set_identifier(item: dict[str, Any] | None) -> str:
    if not item:
        return ""
    return str(item.get("providerSetId") or item.get("providerSetCode") or "").strip()


def sample_selection() -> dict[str, Any]:
    en = choose_set_sample("en", {"604"}, {"BS", "BASE1"})
    jp = choose_set_sample("jp", {"23599"}, {"SV2A"})
    card = choose_card_sample()
    return {
        "knownEnNumericSetId": set_identifier(en),
        "knownEnSetCode": en.get("providerSetCode") if en else None,
        "knownEnSetName": en.get("providerSetName") if en else None,
        "knownJpNumericSetId": set_identifier(jp),
        "knownJpSetCode": jp.get("providerSetCode") if jp else None,
        "knownJpSetName": jp.get("providerSetName") if jp else None,
        "sampleProviderCardId": card.get("providerCardId") if card else None,
        "sampleProviderCardName": card.get("name") if card else None,
        "sampleProviderCardSetId": card.get("providerSetId") if card else None,
    }


def endpoint_specs(samples: dict[str, Any]) -> list[EndpointSpec]:
    en_set_id = str(samples.get("knownEnNumericSetId") or "").strip()
    jp_set_id = str(samples.get("knownJpNumericSetId") or "").strip()
    card_id = str(samples.get("sampleProviderCardId") or "").strip()
    specs = [
        EndpointSpec("health", "/health", "/health", requires_auth=False),
    ]
    if not en_set_id or not jp_set_id or not card_id:
        return specs
    specs.extend(
        [
            EndpointSpec("sets", "/sets", "/sets", query={"page": "1", "limit": "10"}),
            EndpointSpec("set_en", "/sets/:setCode EN", f"/sets/{quote(en_set_id, safe='')}", query={"page": "1", "limit": "5"}),
            EndpointSpec("set_jp", "/sets/:setCode JP", f"/sets/{quote(jp_set_id, safe='')}", query={"page": "1", "limit": "5"}),
            EndpointSpec("prices_en", "/prices/:setCode EN", f"/prices/{quote(en_set_id, safe='')}", expected_pro=True),
            EndpointSpec("prices_jp", "/prices/:setCode JP", f"/prices/{quote(jp_set_id, safe='')}", expected_pro=True),
            EndpointSpec(
                "prices_en_source_tcg",
                "/prices/:setCode EN source=tcg",
                f"/prices/{quote(en_set_id, safe='')}",
                query={"source": "tcg"},
                expected_pro=True,
            ),
            EndpointSpec(
                "prices_en_source_cm",
                "/prices/:setCode EN source=cm",
                f"/prices/{quote(en_set_id, safe='')}",
                query={"source": "cm"},
                expected_pro=True,
            ),
            EndpointSpec("card_detail", "/cards/:id", f"/cards/{quote(card_id, safe='')}"),
            EndpointSpec("card_price_history", "/cards/:id/price-history", f"/cards/{quote(card_id, safe='')}/price-history", expected_pro=True),
            EndpointSpec("sets_trending", "/sets/trending", "/sets/trending", query={"limit": "5"}, expected_pro=True),
            EndpointSpec("analytics_top_cards", "/analytics/top-cards", "/analytics/top-cards", query={"limit": "5"}, expected_pro=True),
        ]
    )
    return specs


def parse_set_metadata(record: dict[str, Any]) -> dict[str, Any] | None:
    set_id = record.get("set_id") or record.get("id") or record.get("providerSetId")
    set_code = record.get("set_code") or record.get("code") or record.get("providerSetCode")
    name = record.get("name") or record.get("set_name") or record.get("providerSetName")
    language = record.get("language") or record.get("lang") or record.get("providerLanguage")
    release_date = record.get("release_date") or record.get("releaseDate")
    card_count = record.get("card_count") if record.get("card_count") is not None else record.get("total_cards")
    if card_count is None:
        card_count = record.get("cardCount")
    if not any([set_id, set_code, name]):
        return None
    return {
        "set_id": str(set_id) if set_id is not None else None,
        "set_code": str(set_code) if set_code is not None else None,
        "name": str(name) if name is not None else None,
        "language": str(language) if language is not None else None,
        "release_date": str(release_date) if release_date is not None else None,
        "card_count": safe_int(card_count),
    }


def build_set_metadata_stage(sets_endpoint: dict[str, Any], sets_payload: Any) -> dict[str, Any]:
    api_items = [item for item in list_items(sets_payload) if isinstance(item, dict)]
    api_samples = []
    for item in api_items:
        parsed = parse_set_metadata(item)
        if parsed:
            api_samples.append(parsed)
        if len(api_samples) >= MAX_SAMPLE_ITEMS:
            break

    code_to_ids: dict[str, set[str]] = {}
    numeric_mappings: list[dict[str, Any]] = []
    for item in provider_sets():
        parsed = parse_set_metadata(item)
        if not parsed:
            continue
        code = str(parsed.get("set_code") or "").strip().upper()
        set_id = str(parsed.get("set_id") or "").strip()
        language = str(item.get("cardScanRLanguage") or parsed.get("language") or "").strip()
        if code and set_id:
            code_to_ids.setdefault(f"{language}:{code}", set()).add(set_id)
        if set_id and set_id.lstrip("-").isdigit() and int(set_id) > 0 and len(numeric_mappings) < MAX_SAMPLE_ITEMS:
            numeric_mappings.append({**parsed, "cardscanr_language": language})

    ambiguous = [
        {"mappingKey": key, "setIds": sorted(ids)}
        for key, ids in sorted(code_to_ids.items())
        if len(ids) > 1
    ][:MAX_SAMPLE_ITEMS]

    return {
        "available": sets_endpoint.get("available") is True,
        "sourceEndpoint": "/sets",
        "fieldsCaptured": ["set_id", "set_code", "name", "language", "release_date", "card_count"],
        "apiSampleCount": len(api_samples),
        "apiSamples": api_samples,
        "numericSetIdMappingsSample": numeric_mappings,
        "ambiguousSetCodeMappings": ambiguous,
        "safeMergePolicy": [
            "Match by numeric provider set_id before set_code.",
            "Treat duplicate language/set_code mappings as ambiguous and require manual review.",
            "Only fill missing provider metadata fields; do not overwrite better app catalogue names, release dates, card counts, logos, or symbols.",
            "Keep provider metadata separate from app canonical set metadata until validation approves promotion.",
        ],
    }


def build_set_logo_refresh_plan(samples: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for language, id_key, code_key, name_key in (
        ("en", "knownEnNumericSetId", "knownEnSetCode", "knownEnSetName"),
        ("jp", "knownJpNumericSetId", "knownJpSetCode", "knownJpSetName"),
    ):
        set_id = str(samples.get(id_key) or "").strip()
        if not set_id:
            continue
        candidates.append(
            {
                "language": language,
                "set_id": set_id,
                "set_code": samples.get(code_key),
                "name": samples.get(name_key),
                "endpoint": f"/sets/{set_id}/image",
            }
        )

    return {
        "endpoint": "/sets/:setCode/image",
        "tested": False,
        "reason": "Not fetched by default because this audit limits image-cache probes to 3 low and 3 high card images.",
        "candidateSamples": candidates,
        "safePolicy": [
            "Probe set logos through an explicit allowlist before enabling any cache writes.",
            "Record status code, content type, and size before storing binaries.",
            "Do not overwrite better existing app set logos or symbols without review.",
            "Keep downloaded logo binaries ignored by Git.",
        ],
    }


def build_price_importer_plan(endpoint_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    prices_en = endpoint_results.get("prices_en", {})
    prices_jp = endpoint_results.get("prices_jp", {})
    prices_tcg = endpoint_results.get("prices_en_source_tcg", {})
    prices_cm = endpoint_results.get("prices_en_source_cm", {})
    prices_works = any(
        item.get("available") is True and item.get("priceSignals", {}).get("hasUsablePrices") is True
        for item in (prices_en, prices_tcg, prices_cm)
    )
    jp_signals = prices_jp.get("priceSignals", {})
    if prices_jp.get("available") is True:
        jp_availability = "usable_prices_found" if jp_signals.get("hasUsablePrices") else "no_usable_prices"
    elif prices_jp.get("availability") in {"requires_pro_or_trial", "forbidden_or_auth_failed"}:
        jp_availability = "unknown_plan_limited"
    elif prices_jp:
        jp_availability = str(prices_jp.get("availability") or "unknown")
    else:
        jp_availability = "not_tested"

    cm_signals = prices_cm.get("priceSignals", {})
    cardmarket_useful = bool(prices_cm.get("available") and cm_signals.get("hasUsablePrices"))

    return {
        "pricesEndpointWorks": prices_works,
        "jpPriceAvailability": jp_availability,
        "cardmarketOnlyUseful": cardmarket_useful,
        "tcgplayerUsdUseful": bool(prices_tcg.get("available") and prices_tcg.get("priceSignals", {}).get("hasUsablePrices")),
        "stageDesign": [
            "Add a staged importer that reads /prices/:numericSetId into a temporary diagnostics file first.",
            "Import by numeric Pokewallet set_id when available; use set_code only as a reviewed fallback.",
            "Preserve provider variants and map them into Stage 1 variant/condition fields without collapsing finishes.",
            "Store TCGPlayer USD and CardMarket EUR as separate source/currency records.",
            "Do not convert currencies until a validated conversion system exists.",
            "Do not fabricate missing market, low, high, or history values.",
            "Mark JP current pricing unavailable when the endpoint returns no usable JP price records.",
            "Only promote into public/v1/prices/current after validate_cache and focused count/source/status checks pass.",
        ],
    }


def image_endpoint(endpoint: str | None, provider_card_id: str, size: str) -> str:
    if endpoint:
        return endpoint
    return f"/images/{quote(provider_card_id, safe='')}?size={quote(size, safe='')}"


def run_image_audit(api_key: str, api_key_present: bool) -> dict[str, Any]:
    cards = image_sample_cards(3)
    if not api_key_present:
        return {
            "attempted": False,
            "reason": "api_key_missing",
            "samplesRequested": 0,
            "samplesSucceeded": 0,
            "samples": [],
            "policy": "Only 3 low and 3 high image metadata probes are allowed; no image binaries are written.",
        }

    samples: list[dict[str, Any]] = []
    for card in cards:
        provider_card_id = str(card.get("providerCardId") or "")
        for size in ("low", "high"):
            endpoint = image_endpoint(card.get(f"imageEndpoint{size.capitalize()}"), provider_card_id, size)
            result = fetch_image_metadata(endpoint, api_key=api_key)
            content_length = safe_int(get_header(result.headers, "Content-Length"))
            samples.append(
                {
                    "providerCardId": provider_card_id,
                    "size": size,
                    "endpointTemplate": "/images/:id",
                    "statusCode": result.status_code,
                    "available": result.ok,
                    "availability": "available" if result.ok else ("rate_limited" if result.status_code == 429 else "unavailable"),
                    "contentType": get_header(result.headers, "Content-Type"),
                    "contentLengthBytes": content_length,
                    "error": result.error,
                    "errorSnippet": result.error_snippet,
                }
            )
    return {
        "attempted": True,
        "samplesRequested": len(samples),
        "samplesSucceeded": sum(1 for sample in samples if sample.get("available") is True),
        "samples": samples,
        "policy": "Only 3 low and 3 high image metadata probes are allowed; no image binaries are written.",
    }


def availability_summary(endpoint_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    available = []
    plan_limited = []
    not_found = []
    rate_limited = []
    unavailable = []
    for name, item in sorted(endpoint_results.items()):
        availability = item.get("availability")
        label = item.get("label") or name
        if availability == "available":
            available.append(label)
        elif availability == "requires_pro_or_trial":
            plan_limited.append(label)
        elif availability == "not_found":
            not_found.append(label)
        elif availability == "rate_limited":
            rate_limited.append(label)
        else:
            unavailable.append(label)
    return {
        "availableEndpoints": available,
        "planLimitedEndpoints": plan_limited,
        "notFoundEndpoints": not_found,
        "rateLimitedEndpoints": rate_limited,
        "otherUnavailableEndpoints": unavailable,
    }


def recommendation_for(report: dict[str, Any]) -> str:
    if not report.get("apiKeyPresent"):
        return "Set POKEWALLET_API_KEY or CARDSCANR_POKEWALLET_API_KEY to run authenticated capability checks."
    prices = report.get("priceImporterPlan", {})
    metadata = report.get("setMetadataRefreshStage", {})
    if prices.get("pricesEndpointWorks"):
        return "Build a staged /prices importer behind diagnostics-only output, then validate source/currency/status counts before public promotion."
    if metadata.get("available"):
        return "Use /sets only for provider set metadata enrichment first; keep price status unchanged until /prices returns usable records on the current plan."
    return "Keep current app catalogue, image manifest, and price status unchanged; no safe new pricing path was proven by this audit."


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append
    a("# PokeWallet API Capability Audit")
    a("")
    a(f"- generatedAtUtc: {report['generatedAtUtc']}")
    a(f"- apiKeyPresent: {'yes' if report.get('apiKeyPresent') else 'no'}")
    a(f"- requests: {report.get('requestsSucceeded', 0)} succeeded / {report.get('requestsAttempted', 0)} attempted")
    a(f"- recommendation: {report.get('recommendation', '')}")
    a("")
    a("## Endpoint Availability")
    a("")
    a("| Endpoint | HTTP | Availability | Usable prices | Notes |")
    a("|---|---:|---|---|---|")
    for item in report.get("endpoints", []):
        signals = item.get("priceSignals", {})
        notes = []
        if item.get("expectedPro"):
            notes.append("pro candidate")
        if item.get("error"):
            notes.append(str(item.get("error")))
        a(
            "| {label} | {status} | {availability} | {prices} | {notes} |".format(
                label=item.get("label") or item.get("name"),
                status=item.get("statusCode") if item.get("statusCode") is not None else "n/a",
                availability=item.get("availability"),
                prices="yes" if signals.get("hasUsablePrices") else "no",
                notes=", ".join(notes) or "",
            )
        )
    a("")
    price_plan = report.get("priceImporterPlan", {})
    a("## Price Findings")
    a("")
    a(f"- /prices works: {'yes' if price_plan.get('pricesEndpointWorks') else 'no'}")
    a(f"- JP price availability: {price_plan.get('jpPriceAvailability')}")
    a(f"- CardMarket-only useful: {'yes' if price_plan.get('cardmarketOnlyUseful') else 'no'}")
    a(f"- TCGPlayer USD useful: {'yes' if price_plan.get('tcgplayerUsdUseful') else 'no'}")
    a("")
    metadata = report.get("setMetadataRefreshStage", {})
    a("## Set Metadata Refresh Stage")
    a("")
    a(f"- /sets available: {'yes' if metadata.get('available') else 'no'}")
    fields = metadata.get("fieldsCaptured", [])
    a(f"- fields captured: {', '.join(fields) if fields else 'none'}")
    a(f"- API samples captured: {metadata.get('apiSampleCount', 0)}")
    a(f"- ambiguous set-code mappings found: {len(metadata.get('ambiguousSetCodeMappings', []))}")
    a(f"- numeric set-id mapping samples: {len(metadata.get('numericSetIdMappingsSample', []))}")
    a("")
    image = report.get("imageCacheAudit", {})
    a("## Image Endpoint Audit")
    a("")
    a(f"- attempted: {'yes' if image.get('attempted') else 'no'}")
    a(f"- samples succeeded: {image.get('samplesSucceeded', 0)} / {image.get('samplesRequested', 0)}")
    for sample in image.get("samples", [])[:6]:
        a(
            f"- {sample.get('size')} image: status={sample.get('statusCode')} "
            f"type={sample.get('contentType') or 'n/a'} bytes={sample.get('contentLengthBytes') or 'n/a'}"
        )
    a("")
    logo_plan = report.get("setLogoRefreshPlan", {})
    a("## Set Logo Cache Plan")
    a("")
    a(f"- endpoint: {logo_plan.get('endpoint', '/sets/:setCode/image')}")
    a(f"- fetched in this audit: {'yes' if logo_plan.get('tested') else 'no'}")
    if logo_plan.get("reason"):
        a(f"- reason: {logo_plan.get('reason')}")
    for item in logo_plan.get("candidateSamples", [])[:2]:
        a(f"- candidate {item.get('language')}: {item.get('set_id')} ({item.get('set_code')})")
    a("")
    a("## Integration Plan")
    a("")
    a("Set metadata refresh:")
    for item in report.get("setMetadataRefreshStage", {}).get("safeMergePolicy", []):
        a(f"- {item}")
    a("")
    a("Set logo/image cache:")
    for item in logo_plan.get("safePolicy", []):
        a(f"- {item}")
    a("")
    a("Price importer:")
    for item in price_plan.get("stageDesign", []):
        a(f"- {item}")
    a("")
    return "\n".join(lines)


def run_audit() -> dict[str, Any]:
    api_key, api_key_env_used, api_key_env_names_checked = resolve_api_key()
    api_key_present = bool(api_key)
    samples = sample_selection()
    specs = endpoint_specs(samples)
    endpoint_results: dict[str, dict[str, Any]] = {}
    raw_payloads: dict[str, Any] = {}
    requests_attempted = 0
    requests_succeeded = 0
    requests_failed = 0

    for spec in specs:
        if spec.requires_auth and not api_key_present:
            endpoint_results[spec.name] = {
                "name": spec.name,
                "label": spec.label,
                "method": "GET",
                "path": spec.path,
                "query": spec.query or {},
                "requiresAuth": spec.requires_auth,
                "expectedPro": spec.expected_pro,
                "statusCode": None,
                "available": False,
                "availability": "skipped_api_key_missing",
                "contentType": None,
                "responseShape": {},
                "priceSignals": scan_price_signals(None),
                "error": None,
                "errorSnippet": None,
            }
            continue
        result = fetch_json_endpoint(spec, api_key=api_key)
        requests_attempted += 1
        if result.ok:
            requests_succeeded += 1
            raw_payloads[spec.name] = result.payload
        else:
            requests_failed += 1
        endpoint_results[spec.name] = summarize_endpoint(spec, result)

    image_audit = run_image_audit(api_key, api_key_present)
    if image_audit.get("attempted"):
        requests_attempted += int(image_audit.get("samplesRequested") or 0)
        requests_succeeded += int(image_audit.get("samplesSucceeded") or 0)
        requests_failed += int(image_audit.get("samplesRequested") or 0) - int(image_audit.get("samplesSucceeded") or 0)

    report = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": now_utc(),
        "provider": "pokewallet",
        "status": "completed",
        "apiBaseUrl": BASE_URL,
        "apiKeyPresent": api_key_present,
        "apiKeyEnvNamesChecked": api_key_env_names_checked,
        "apiKeyEnvUsed": api_key_env_used,
        "requestsAttempted": requests_attempted,
        "requestsSucceeded": requests_succeeded,
        "requestsFailed": requests_failed,
        "sampleSelection": samples,
        "endpoints": [endpoint_results[name] for name in endpoint_results],
        "endpointAvailabilitySummary": availability_summary(endpoint_results),
        "setMetadataRefreshStage": build_set_metadata_stage(
            endpoint_results.get("sets", {}),
            raw_payloads.get("sets"),
        ),
        "setLogoRefreshPlan": build_set_logo_refresh_plan(samples),
        "priceImporterPlan": build_price_importer_plan(endpoint_results),
        "imageCacheAudit": image_audit,
        "notes": [
            "No API keys are written to this report.",
            "No generated price files are written by this audit.",
            "No image binaries are written by this audit.",
            "Authenticated endpoint results reflect only the current environment and provider plan at audit time.",
        ],
    }
    report["recommendation"] = recommendation_for(report)
    return report


def main() -> int:
    report = run_audit()
    write_json(REPORT_JSON_PATH, report)
    REPORT_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD_PATH.write_text(render_markdown(report), encoding="utf-8", newline="\n")

    summary = report["endpointAvailabilitySummary"]
    safe_print("PokeWallet API capability audit")
    safe_print(f"  apiKeyPresent: {'yes' if report['apiKeyPresent'] else 'no'}")
    safe_print(f"  requests: {report['requestsSucceeded']} succeeded / {report['requestsAttempted']} attempted")
    safe_print(f"  available endpoints: {len(summary['availableEndpoints'])}")
    safe_print(f"  plan-limited endpoints: {len(summary['planLimitedEndpoints'])}")
    safe_print(f"  /prices works: {'yes' if report['priceImporterPlan']['pricesEndpointWorks'] else 'no'}")
    safe_print(f"  JP prices: {report['priceImporterPlan']['jpPriceAvailability']}")
    safe_print(f"  image samples: {report['imageCacheAudit'].get('samplesSucceeded', 0)} succeeded / {report['imageCacheAudit'].get('samplesRequested', 0)} requested")
    safe_print(f"  wrote: {REPORT_JSON_PATH.relative_to(ROOT)}")
    safe_print(f"  wrote: {REPORT_MD_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
