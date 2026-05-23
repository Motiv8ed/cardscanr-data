#!/usr/bin/env python3
"""
build_price_cache.py

Builds static tracked-card price cache files under public/v1/prices/ from
cards listed in data/cards_to_track.json.

Live pricing strategy:
- Use TCGdex first via documented language-aware card endpoint:
  /v2/{language}/cards/{cardId}
- If tcgdexCardId is missing, do a safe lookup within set cards and only
  proceed on a confident single match.
- Fall back to manual_seed when no confident live match is available.

Also writes tracked-card daily history snapshots under:
public/v1/history/daily/{yyyy-mm-dd}/{game}/{language}/tracked.json
"""

import hashlib
import json
import os
import re
import shutil
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PUBLIC_DIR = ROOT / "public" / "v1"
PRICES_DIR = PUBLIC_DIR / "prices"
CURRENT_PRICES_EN_DIR = PRICES_DIR / "current" / "pokemon" / "en"
CURRENT_PRICES_JP_DIR = PRICES_DIR / "current" / "pokemon" / "jp"
PRICES_STATUS_PATH = PRICES_DIR / "status.json"
EN_CURRENT_STATUS_PATH = CURRENT_PRICES_EN_DIR / "status.json"
JP_CURRENT_STATUS_PATH = CURRENT_PRICES_JP_DIR / "status.json"
DIAGNOSTICS_DIR = PUBLIC_DIR / "diagnostics"
HISTORY_ROOT_DIR = PUBLIC_DIR / "history"
HISTORY_DIR = HISTORY_ROOT_DIR / "daily"
TRACKED_CARDS_PATH = HISTORY_ROOT_DIR / "tracked-cards.json"
CATALOG_DIR = PUBLIC_DIR / "catalog"
CATALOG_CONFIG_PATH = DATA_DIR / "catalog_config.json"
API_MANIFEST_PATH = PUBLIC_DIR / "api-manifest.json"
API_NOTES_PATH = PUBLIC_DIR / "api-notes.json"
SCHEMAS_PATH = PUBLIC_DIR / "schemas.json"
APP_CONFIG_PATH = PUBLIC_DIR / "app-config.json"
INDEX_PATH = PUBLIC_DIR / "index.json"
DIAG_PATH = DIAGNOSTICS_DIR / "latest-build.json"
CARDS_PATH = DATA_DIR / "cards_to_track.json"
SUPPORTED_LANGUAGES_PATH = PUBLIC_DIR / "supported-languages.json"
SUPPORTED_MARKETS_PATH = PUBLIC_DIR / "supported-markets.json"
SUPPORTED_LANGUAGES_CONFIG_PATH = DATA_DIR / "supported_languages_config.json"
SUPPORTED_MARKETS_CONFIG_PATH = DATA_DIR / "supported_markets_config.json"
BASE_URL = "https://cardscanr-cache.pages.dev/v1"
DEFAULT_CACHE_TTL_SECONDS = 86400
PRICE_CACHE_TTL_SECONDS = 43200
DIAGNOSTICS_CACHE_TTL_SECONDS = 900
HISTORY_CACHE_TTL_SECONDS = 86400
CATALOG_CACHE_TTL_SECONDS = 86400
POKEMON_TCG_API_BASE = "https://api.pokemontcg.io/v2"
POKEWALLET_API_BASE = "https://api.pokewallet.io"
SOURCE_ID_POKEMON_TCG_API = "pokemon_tcg_api"
SOURCE_ID_TCGDEX = "tcgdex"
SOURCE_ID_TCGDEX_TCGPLAYER = "tcgdex_tcgplayer"
SOURCE_ID_TCGDEX_CARDMARKET = "tcgdex_cardmarket"
SOURCE_ID_POKEWALLET = "pokewallet"
SOURCE_ID_EBAY_SOLD_MANUAL = "ebay_sold_manual"
SOURCE_ID_MANUAL = "manual"
SOURCE_ID_MANUAL_SEED = "manual_seed"
SOURCE_ID_UNAVAILABLE = "unavailable"
# Mirrors app-facing alias compatibility in /v1/supported-sources.json.
SOURCE_ID_ALIASES = {
    SOURCE_ID_POKEMON_TCG_API: ["pokemonTcgApi"],
    SOURCE_ID_EBAY_SOLD_MANUAL: ["ebaySoldListingsManual"],
}
APP_SUPPORTED_SOURCE_IDS = [
    SOURCE_ID_POKEMON_TCG_API,
    SOURCE_ID_TCGDEX,
    SOURCE_ID_TCGDEX_TCGPLAYER,
    SOURCE_ID_TCGDEX_CARDMARKET,
    SOURCE_ID_POKEWALLET,
    SOURCE_ID_EBAY_SOLD_MANUAL,
    SOURCE_ID_MANUAL,
    SOURCE_ID_MANUAL_SEED,
    SOURCE_ID_UNAVAILABLE,
]
CURRENT_PRICE_SOURCE = SOURCE_ID_POKEMON_TCG_API
CURRENT_PRICE_CURRENCY = "USD"
CURRENT_PRICE_VARIANTS = [
    ("normal", "normal"),
    ("holofoil", "holo"),
    ("reverseHolofoil", "reverse"),
    ("1stEditionHolofoil", "first_edition_holo"),
    ("1stEditionNormal", "first_edition_normal"),
]
TMP_BUILD_ROOT = ROOT / ".cache_build_tmp"
CURRENT_PRICE_REQUEST_CAP_ENV = "CARDSCANR_CURRENT_PRICE_REQUEST_CAP"
POKEWALLET_CURRENT_PRICE_REQUEST_CAP_ENV = "CARDSCANR_POKEWALLET_CURRENT_PRICE_REQUEST_CAP"
POKEMON_TCG_CURRENT_PRICE_REQUEST_CAP_ENV = "CARDSCANR_POKEMON_TCG_CURRENT_PRICE_REQUEST_CAP"
CURRENT_PRICE_TRANSIENT_RETRY_COUNT_ENV = "CARDSCANR_CURRENT_PRICE_TRANSIENT_RETRY_COUNT"
POKEWALLET_API_KEY_ENV = "CARDSCANR_POKEWALLET_API_KEY"
POKEWALLET_API_KEY_ALIAS_ENV = "POKEWALLET_API_KEY"
POKEWALLET_SET_SUMMARY_PATH = PUBLIC_DIR / "provider-catalog" / "pokewallet" / "sets-summary.json"
POKEWALLET_PRICE_PROVIDER_PRIORITY_ENV = "CARDSCANR_PRICE_PROVIDER_PRIORITY"
POKEWALLET_USE_PRICES_ENV = "CARDSCANR_USE_POKEWALLET_PRICES"
POKEWALLET_REQUIRE_PRICES_ENV = "CARDSCANR_REQUIRE_POKEWALLET_PRICES"
CURRENT_PRICE_SET_ID_ENV = "CARDSCANR_CURRENT_PRICE_SET_ID"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
SCHEMA_VERSION = "1.0.0"

LANGUAGE_TO_TCGDEX = {
    "en": "en",
    "jp": "ja",
    "ja": "ja",
}


class ProviderRateLimitError(RuntimeError):
    """Raised when a provider reports request quota/rate-limit exhaustion."""

    def __init__(self, provider: str, status_code: int | None = None, detail: str = "") -> None:
        self.provider = provider
        self.status_code = status_code
        self.detail = detail
        message = f"{provider} rate limit encountered"
        if status_code is not None:
            message += f" (status={status_code})"
        if detail:
            message += f": {detail}"
        super().__init__(message)


REQUEST_TRACKER: dict[str, int] = {
    "attempted": 0,
    "succeeded": 0,
    "failed": 0,
    "rateLimited": 0,
}
PROVIDER_REQUEST_TRACKER: dict[str, dict[str, int]] = {}
CURRENT_PRICE_REQUEST_CAP = 0
CURRENT_PRICE_PROVIDER_REQUEST_CAPS: dict[str, int] = {}


class RequestCapReachedError(RuntimeError):
    """Raised when the current-price request cap has been reached."""

    def __init__(self, message: str, provider: str | None = None) -> None:
        self.provider = provider
        super().__init__(message)


def resolve_current_price_request_cap() -> int:
    return parse_positive_int_env(CURRENT_PRICE_REQUEST_CAP_ENV)


def parse_non_negative_int_env_if_present(name: str) -> int | None:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return None
    try:
        value = int(raw_value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer when set") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def resolve_provider_current_price_request_caps() -> dict[str, int]:
    caps: dict[str, int] = {}
    pokewallet_cap = parse_non_negative_int_env_if_present(POKEWALLET_CURRENT_PRICE_REQUEST_CAP_ENV)
    pokemon_tcg_cap = parse_non_negative_int_env_if_present(POKEMON_TCG_CURRENT_PRICE_REQUEST_CAP_ENV)
    if pokewallet_cap is not None:
        caps[SOURCE_ID_POKEWALLET] = pokewallet_cap
    if pokemon_tcg_cap is not None:
        caps[SOURCE_ID_POKEMON_TCG_API] = pokemon_tcg_cap
    return caps


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def parse_positive_int_env(name: str) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return 0
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer when set") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def parse_bool_env(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    return raw_value.lower() in {"1", "true", "yes", "y", "on"}


def parse_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def resolve_pokewallet_api_key() -> str:
    for env_name in (POKEWALLET_API_KEY_ENV, POKEWALLET_API_KEY_ALIAS_ENV):
        raw_value = os.getenv(env_name, "").strip()
        if raw_value:
            return raw_value
    return ""


def normalize_lookup_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_catalog_name(str(value or "")))


POKEWALLET_SET_CODE_OVERRIDES: dict[str, dict[str, str]] = {
    # PokemonTCG app set id -> known Pokewallet provider set metadata pattern.
    "bwp": {
        "providerSetNameContains": "black and white promos",
    },
}


POKEWALLET_SET_NAME_ALIASES: dict[str, list[str]] = {
    "bw black star promos": [
        "black and white promos",
        "black white promos",
        "bw promos",
        "black star promos",
    ],
}


def parse_catalog_release_date(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_pokewallet_release_date(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%dth %B, %Y", "%dst %B, %Y", "%dnd %B, %Y", "%drd %B, %Y"):
        try:
            cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text)
            return datetime.strptime(cleaned, "%d %B, %Y")
        except ValueError:
            continue
    return None


def load_pokewallet_set_code_map() -> dict[str, list[dict[str, object]]]:
    if not POKEWALLET_SET_SUMMARY_PATH.exists():
        return {}
    payload = load_json(POKEWALLET_SET_SUMMARY_PATH)
    entries: list[dict[str, object]] = []
    source_sets = payload.get("sets") if isinstance(payload, dict) else None
    if not isinstance(source_sets, list):
        source_sets = []
    for item in source_sets:
        if not isinstance(item, dict):
            continue
        provider_set_code = str(item.get("providerSetCode") or "").strip()
        provider_set_name = str(item.get("providerSetName") or "").strip()
        provider_set_id = str(item.get("providerSetId") or "").strip()
        if not provider_set_code and not provider_set_name and not provider_set_id:
            continue
        entries.append(
            {
                "providerSetCode": provider_set_code,
                "providerSetName": provider_set_name,
                "providerSetId": provider_set_id,
                "cardCount": int(item.get("cardCount") or 0),
                "releaseDate": str(item.get("releaseDate") or "").strip(),
                "updatedAtUtc": str(item.get("updatedAtUtc") or ""),
                "language": str(item.get("cardScanRLanguage") or "").strip().lower(),
                "providerLanguage": str(item.get("providerLanguage") or "").strip().lower(),
                "lookupName": normalize_lookup_text(provider_set_name),
                "lookupCode": normalize_lookup_text(provider_set_code),
            }
        )
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in entries:
        lookup_name = str(item.get("lookupName") or "")
        if not lookup_name:
            continue
        grouped.setdefault(lookup_name, []).append(item)
    return grouped


def flatten_pokewallet_set_entries(set_map: dict[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    deduped: dict[str, dict[str, object]] = {}
    for candidates in set_map.values():
        for item in candidates:
            if not isinstance(item, dict):
                continue
            dedupe_key = "|".join(
                [
                    str(item.get("providerSetId") or ""),
                    str(item.get("providerSetCode") or ""),
                    str(item.get("providerSetName") or ""),
                    str(item.get("language") or ""),
                ]
            )
            deduped[dedupe_key] = item
    return list(deduped.values())


def build_pokewallet_match_diagnostics(
    set_id: str,
    set_name: str,
    reason: str,
    candidates: list[dict[str, object]],
) -> list[str]:
    lines = [
        f"PokeWallet match diagnostics for set {set_id}: reason={reason}",
        f"- app setId={set_id}",
        f"- app setName={set_name}",
    ]
    if not candidates:
        lines.append("- top candidates: none")
        return lines
    lines.append("- top candidates:")
    for candidate in candidates[:5]:
        lines.append(
            "  - "
            f"code={candidate.get('providerSetCode')}, "
            f"id={candidate.get('providerSetId')}, "
            f"name={candidate.get('providerSetName')}, "
            f"language={candidate.get('language')}, "
            f"cardCount={candidate.get('cardCount')}, "
            f"releaseDate={candidate.get('releaseDate')}"
        )
    return lines


def resolve_pokewallet_set_match(set_data: dict, set_map: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    set_id = str(set_data.get("id") or "").strip()
    set_name = str(set_data.get("name") or "").strip()
    set_language = str(set_data.get("language") or "en").strip().lower() or "en"
    if not set_name and not set_id:
        return {"matchedCode": None, "matchedSetId": None, "reason": "missing_set_identity", "candidates": []}

    all_entries = []
    for item in flatten_pokewallet_set_entries(set_map):
        candidate_language = str(item.get("language") or "").strip().lower()
        if candidate_language and candidate_language != set_language:
            continue
        all_entries.append(item)
    if not all_entries:
        return {"matchedCode": None, "matchedSetId": None, "reason": "no_language_entries", "candidates": []}

    set_id_upper = set_id.upper()
    ptcgo_code = str(set_data.get("ptcgoCode") or "").strip().upper()
    exact_codes = [code for code in {set_id_upper, ptcgo_code} if code]

    exact_code_matches = [
        item for item in all_entries if str(item.get("providerSetCode") or "").strip().upper() in exact_codes
    ]
    if len(exact_code_matches) == 1:
        return {
            "matchedCode": str(exact_code_matches[0].get("providerSetCode") or "").strip(),
            "matchedSetId": str(exact_code_matches[0].get("providerSetId") or "").strip() or None,
            "reason": "exact_code_match",
            "candidates": exact_code_matches,
        }
    if len(exact_code_matches) > 1:
        return {
            "matchedCode": None,
            "matchedSetId": None,
            "reason": "ambiguous_exact_code_match",
            "candidates": exact_code_matches,
        }

    override = POKEWALLET_SET_CODE_OVERRIDES.get(set_id.lower())
    if override:
        by_id = str(override.get("providerSetId") or "").strip()
        if by_id:
            id_matches = [item for item in all_entries if str(item.get("providerSetId") or "").strip() == by_id]
            if len(id_matches) == 1:
                return {
                    "matchedCode": str(id_matches[0].get("providerSetCode") or "").strip(),
                    "matchedSetId": str(id_matches[0].get("providerSetId") or "").strip() or None,
                    "reason": "override_provider_set_id",
                    "candidates": id_matches,
                }
        name_contains = normalize_lookup_text(override.get("providerSetNameContains"))
        if name_contains:
            override_name_matches = [
                item
                for item in all_entries
                if name_contains in normalize_lookup_text(item.get("providerSetName"))
            ]
            if len(override_name_matches) == 1:
                return {
                    "matchedCode": str(override_name_matches[0].get("providerSetCode") or "").strip(),
                    "matchedSetId": str(override_name_matches[0].get("providerSetId") or "").strip() or None,
                    "reason": "override_name_match",
                    "candidates": override_name_matches,
                }
            if len(override_name_matches) > 1:
                return {
                    "matchedCode": None,
                    "matchedSetId": None,
                    "reason": "ambiguous_override_name_match",
                    "candidates": override_name_matches,
                }

    lookup_names = {normalize_lookup_text(set_name)}
    for alias in POKEWALLET_SET_NAME_ALIASES.get(set_name.strip().lower(), []):
        lookup_names.add(normalize_lookup_text(alias))

    name_matches = [
        item for item in all_entries if normalize_lookup_text(item.get("providerSetName")) in lookup_names
    ]
    if len(name_matches) == 1:
        return {
            "matchedCode": str(name_matches[0].get("providerSetCode") or "").strip(),
            "matchedSetId": str(name_matches[0].get("providerSetId") or "").strip() or None,
            "reason": "normalized_name_match",
            "candidates": name_matches,
        }

    app_printed_total = safe_int(set_data.get("printedTotal"), default=-1)
    app_release_dt = parse_catalog_release_date(set_data.get("releaseDate"))

    scored: list[tuple[int, int, int, dict[str, object]]] = []
    for item in all_entries:
        candidate_name_norm = normalize_lookup_text(item.get("providerSetName"))
        score = 0
        if candidate_name_norm in lookup_names:
            score += 10

        candidate_count = safe_int(item.get("cardCount"), default=-1)
        if app_printed_total >= 0 and candidate_count >= 0:
            diff = abs(app_printed_total - candidate_count)
            if diff == 0:
                score += 5
            elif diff <= 3:
                score += 3
            elif diff <= 8:
                score += 1

        candidate_release_dt = parse_pokewallet_release_date(item.get("releaseDate"))
        if app_release_dt is not None and candidate_release_dt is not None:
            release_diff_days = abs((app_release_dt - candidate_release_dt).days)
            if release_diff_days <= 120:
                score += 2
            elif release_diff_days <= 365:
                score += 1
        else:
            release_diff_days = 10_000

        scored.append((score, -abs(app_printed_total - candidate_count) if app_printed_total >= 0 and candidate_count >= 0 else -9999, -release_diff_days, item))

    scored.sort(reverse=True, key=lambda row: (row[0], row[1], row[2], str(row[3].get("providerSetCode") or "")))
    top_candidates = [row[3] for row in scored[:5] if row[0] > 0]
    if not top_candidates:
        return {"matchedCode": None, "matchedSetId": None, "reason": "no_candidate_match", "candidates": []}

    top_score = scored[0][0]
    equally_top = [row[3] for row in scored if row[0] == top_score and top_score > 0]
    if len(equally_top) == 1:
        return {
            "matchedCode": str(equally_top[0].get("providerSetCode") or "").strip(),
            "matchedSetId": str(equally_top[0].get("providerSetId") or "").strip() or None,
            "reason": "scored_match",
            "candidates": top_candidates,
        }

    return {
        "matchedCode": None,
        "matchedSetId": None,
        "reason": "ambiguous_scored_match",
        "candidates": top_candidates,
    }


def resolve_pokewallet_set_code(set_data: dict, set_map: dict[str, list[dict[str, object]]]) -> str | None:
    match = resolve_pokewallet_set_match(set_data, set_map)
    matched_code = str(match.get("matchedCode") or "").strip()
    return matched_code or None


def is_rate_limited_response(response: requests.Response, detail_text: str = "") -> bool:
    if response.status_code == 429:
        return True
    if response.status_code in {401, 403, 400} and is_rate_limit_detail_text(detail_text):
        return True
    return False


def pokewallet_get_detailed(endpoint: str, api_key: str, params: dict | None = None) -> tuple[dict, int]:
    if remaining_current_price_requests(SOURCE_ID_POKEWALLET) is not None and remaining_current_price_requests(SOURCE_ID_POKEWALLET) <= 0:
        raise RequestCapReachedError(
            f"current price request cap reached before requesting PokeWallet {endpoint}",
            provider=SOURCE_ID_POKEWALLET,
        )
    response = requests.get(
        f"{POKEWALLET_API_BASE}/{endpoint.lstrip('/')}",
        params=params or {},
        headers={"X-API-Key": api_key},
        timeout=30,
    )
    payload: object = {}
    detail_text = ""
    try:
        payload = response.json()
        detail_text = extract_error_detail(payload)
    except ValueError:
        detail_text = (response.text or "").strip()

    if is_rate_limited_response(response, detail_text):
        mark_request_attempt(success=False, rate_limited=True, provider=SOURCE_ID_POKEWALLET)
        raise ProviderRateLimitError(
            provider=SOURCE_ID_POKEWALLET,
            status_code=response.status_code,
            detail=detail_text or "provider reported a request limit",
        )

    if response.status_code >= 400:
        mark_request_attempt(success=False, provider=SOURCE_ID_POKEWALLET)
        response.raise_for_status()

    mark_request_attempt(success=True, provider=SOURCE_ID_POKEWALLET)
    if not isinstance(payload, dict):
        raise ValueError(f"PokéWallet API returned non-object payload for {endpoint}")
    return payload, int(response.status_code)


def pokewallet_get(endpoint: str, api_key: str, params: dict | None = None) -> dict:
    payload, _status_code = pokewallet_get_detailed(endpoint, api_key, params=params)
    return payload


def reset_request_tracker() -> None:
    REQUEST_TRACKER["attempted"] = 0
    REQUEST_TRACKER["succeeded"] = 0
    REQUEST_TRACKER["failed"] = 0
    REQUEST_TRACKER["rateLimited"] = 0
    PROVIDER_REQUEST_TRACKER.clear()


def provider_request_count(provider: str, key: str = "attempted") -> int:
    counts = PROVIDER_REQUEST_TRACKER.get(provider)
    if not isinstance(counts, dict):
        return 0
    return int(counts.get(key, 0) or 0)


def current_price_request_cap_for_provider(provider: str | None = None) -> int:
    if provider and provider in CURRENT_PRICE_PROVIDER_REQUEST_CAPS:
        return int(CURRENT_PRICE_PROVIDER_REQUEST_CAPS.get(provider) or 0)
    return int(CURRENT_PRICE_REQUEST_CAP or 0)


def remaining_current_price_requests(provider: str | None = None) -> int | None:
    if provider and provider in CURRENT_PRICE_PROVIDER_REQUEST_CAPS:
        cap = max(0, int(CURRENT_PRICE_PROVIDER_REQUEST_CAPS.get(provider) or 0))
        used = provider_request_count(provider, "attempted")
        return max(0, cap - int(used))
    cap = current_price_request_cap_for_provider(provider)
    if cap <= 0:
        return None
    used = int(REQUEST_TRACKER.get("attempted", 0))
    return max(0, cap - int(used))


def provider_request_counts_summary() -> dict[str, int]:
    return {
        provider: int(counts.get("attempted", 0) or 0)
        for provider, counts in sorted(PROVIDER_REQUEST_TRACKER.items())
        if isinstance(counts, dict) and int(counts.get("attempted", 0) or 0) > 0
    }


def provider_request_metric(provider: str, key: str) -> int:
    counts = PROVIDER_REQUEST_TRACKER.get(provider, {})
    return int(counts.get(key, 0) or 0) if isinstance(counts, dict) else 0


def mark_request_attempt(success: bool, rate_limited: bool = False, provider: str | None = None) -> None:
    REQUEST_TRACKER["attempted"] += 1
    if success:
        REQUEST_TRACKER["succeeded"] += 1
    else:
        REQUEST_TRACKER["failed"] += 1
    if rate_limited:
        REQUEST_TRACKER["rateLimited"] += 1
    if provider:
        provider_counts = PROVIDER_REQUEST_TRACKER.setdefault(
            provider,
            {"attempted": 0, "succeeded": 0, "failed": 0, "rateLimited": 0},
        )
        provider_counts["attempted"] += 1
        if success:
            provider_counts["succeeded"] += 1
        else:
            provider_counts["failed"] += 1
        if rate_limited:
            provider_counts["rateLimited"] += 1


def is_transient_request_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            requests.Timeout,
            requests.ConnectionError,
            requests.ReadTimeout,
            requests.ConnectTimeout,
        ),
    )


def record_untracked_transient_request_failure(exc: BaseException, provider: str | None = None) -> None:
    if is_transient_request_error(exc):
        mark_request_attempt(success=False, provider=provider)


def transient_failure_reason(provider: str, exc: BaseException) -> str:
    return f"{provider}_{exc.__class__.__name__}"


def retry_transient_request(callable_obj, *, provider: str, retries: int, sleep_seconds: float):
    attempts = max(1, retries + 1)
    last_exc: requests.RequestException | None = None
    for attempt in range(1, attempts + 1):
        if remaining_current_price_requests(provider) is not None and remaining_current_price_requests(provider) <= 0:
            raise RequestCapReachedError(f"current price request cap reached before retrying {provider}", provider=provider)
        try:
            return callable_obj()
        except requests.RequestException as exc:
            if not is_transient_request_error(exc):
                raise
            record_untracked_transient_request_failure(exc, provider=provider)
            last_exc = exc
            if attempt >= attempts:
                raise
            if remaining_current_price_requests(provider) is not None and remaining_current_price_requests(provider) <= 0:
                raise RequestCapReachedError(f"current price request cap reached after {provider} transient failure", provider=provider) from exc
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("unreachable retry state")


def is_rate_limit_detail_text(text: str) -> bool:
    value = (text or "").strip().lower()
    if not value:
        return False
    signals = [
        "rate limit",
        "too many requests",
        "over limit",
        "over-limit",
        "quota exceeded",
        "daily limit",
        "request limit",
        "limit exceeded",
    ]
    return any(signal in value for signal in signals)


def extract_error_detail(payload: object) -> str:
    if isinstance(payload, dict):
        candidate_keys = ["error", "errors", "message", "detail", "status", "title"]
        for key in candidate_keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        return item.strip()
                    if isinstance(item, dict):
                        for nested in ["message", "detail", "title", "error"]:
                            nested_value = item.get(nested)
                            if isinstance(nested_value, str) and nested_value.strip():
                                return nested_value.strip()
    return ""


def reset_tmp_build_root(tmp_root: Path) -> None:
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)


def cleanup_tmp_build_root(tmp_root: Path) -> None:
    if not tmp_root.exists():
        return
    try:
        shutil.rmtree(tmp_root)
    except OSError:
        # Best-effort cleanup only; build correctness does not depend on this.
        pass


def prepare_empty_dir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def publish_staged_directory(staged_dir: Path, target_dir: Path) -> None:
    if not staged_dir.exists() or not staged_dir.is_dir():
        raise RuntimeError(f"Staged directory does not exist: {staged_dir}")

    backup_dir = target_dir.with_name(f"{target_dir.name}.bak")
    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    target_parent = target_dir.parent
    target_parent.mkdir(parents=True, exist_ok=True)

    if target_dir.exists():
        target_dir.rename(backup_dir)

    try:
        staged_dir.rename(target_dir)
    except Exception:
        if backup_dir.exists() and not target_dir.exists():
            backup_dir.rename(target_dir)
        raise
    finally:
        if backup_dir.exists():
            shutil.rmtree(backup_dir)


def log_line(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        # Keep builds resilient on Windows cp1252 consoles.
        print(message.encode("ascii", "backslashreplace").decode("ascii"))


def load_json(path: Path) -> object:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_catalog_config() -> dict:
    defaults = {
        "fullCatalogueEnabled": True,
        "buildEnglishFromPokemonTcgApi": True,
        "buildCurrentPricesFromPokemonTcgApi": True,
        "buildJapaneseFromTcgdex": True,
        "japaneseCatalogueFetchStrategy": "tcgdex_set_by_set",
        "jpMaxSetsPerRun": 9999,
        "jpMaxPagesPerSet": 50,
        "jpContinueOnSetError": True,
        "jpCatalogueRequestSleepSeconds": 0.15,
        "scheduledJapaneseCatalogueEnabled": False,
        "rebuildFullCatalogueOnScheduled": False,
        "englishCatalogueFetchStrategy": "set_by_set",
        "maxSetsPerRun": 9999,
        "maxPagesPerSet": 50,
        "continueOnSetError": True,
        "catalogueRequestSleepSeconds": 0.15,
        "pageSize": 250,
        "maxPagesPerRun": 1000,
        "localUpdaterEnabled": True,
        "localUpdaterDefaultBatchSize": 10,
        "localUpdaterRefreshStrategy": "rotating_set_batch",
        "scheduledCurrentPriceBatchEnabled": True,
        "scheduledCurrentPriceBatchSize": 10,
        "scheduledCurrentPriceRefreshStrategy": "rotating_set_batch",
        "scheduledCurrentPriceStatePath": "data/scheduled_price_refresh_state.json",
    }
    if not CATALOG_CONFIG_PATH.exists():
        return defaults

    config = load_json(CATALOG_CONFIG_PATH)
    if not isinstance(config, dict):
        return defaults
    return {**defaults, **config}


def normalize_price_snapshot(price: dict, ts: str) -> dict:
    return {
        "currency": price.get("currency"),
        "marketPrice": price.get("marketPrice"),
        "lowPrice": price.get("lowPrice"),
        "highPrice": price.get("highPrice"),
        "source": price.get("source"),
        "fetchedAtUtc": price.get("fetchedAtUtc", ts),
    }


def update_tracked_cards_history(
    ts: str,
    cards_by_id: dict[str, dict],
    latest_prices_by_id: dict[str, dict],
) -> tuple[dict, int, int]:
    existing_cards = {}
    if TRACKED_CARDS_PATH.exists():
        existing_payload = load_json(TRACKED_CARDS_PATH)
        if isinstance(existing_payload, dict):
            for record in existing_payload.get("cards", []):
                if isinstance(record, dict) and record.get("canonicalId"):
                    existing_cards[str(record["canonicalId"])] = record

    tracked_cards = []
    first_tracked_created_count = 0
    tracked_cards_updated_count = 0

    for canonical_id in sorted(cards_by_id.keys()):
        card = cards_by_id[canonical_id]
        latest_price = normalize_price_snapshot(latest_prices_by_id[canonical_id], ts)
        existing = existing_cards.get(canonical_id)

        if existing:
            first_tracked_at = existing.get("firstTrackedAtUtc") or ts
            first_tracked_price = existing.get("firstTrackedPrice") or latest_price
            snapshot_count = int(existing.get("trackingStats", {}).get("snapshotCount", 0)) + 1
            tracked_cards_updated_count += 1
        else:
            first_tracked_at = ts
            first_tracked_price = latest_price
            snapshot_count = 1
            first_tracked_created_count += 1

        first_market = to_float(first_tracked_price.get("marketPrice"))
        latest_market = to_float(latest_price.get("marketPrice"))
        prior_highest = to_float((existing or {}).get("trackingStats", {}).get("highestSinceTracked"))
        prior_lowest = to_float((existing or {}).get("trackingStats", {}).get("lowestSinceTracked"))

        if latest_market is None:
            highest_since_tracked = prior_highest if prior_highest is not None else first_market
            lowest_since_tracked = prior_lowest if prior_lowest is not None else first_market
        else:
            highest_candidates = [v for v in [prior_highest, first_market, latest_market] if v is not None]
            lowest_candidates = [v for v in [prior_lowest, first_market, latest_market] if v is not None]
            highest_since_tracked = max(highest_candidates) if highest_candidates else None
            lowest_since_tracked = min(lowest_candidates) if lowest_candidates else None

        change_since_first = None
        change_percent_since_first = None
        if first_market is not None and latest_market is not None:
            change_since_first = round(latest_market - first_market, 2)
            if first_market != 0:
                change_percent_since_first = round((change_since_first / first_market) * 100, 2)

        tracked_cards.append(
            {
                "canonicalId": canonical_id,
                "game": card.get("game"),
                "language": card.get("language"),
                "setId": card.get("setId"),
                "collectorNumber": card.get("collectorNumber"),
                "normalizedName": card.get("normalizedName"),
                "variant": card.get("variant"),
                "condition": card.get("condition"),
                "firstTrackedAtUtc": first_tracked_at,
                "lastTrackedAtUtc": ts,
                "firstTrackedPrice": first_tracked_price,
                "latestPrice": latest_price,
                "trackingStats": {
                    "snapshotCount": snapshot_count,
                    "highestSinceTracked": highest_since_tracked,
                    "lowestSinceTracked": lowest_since_tracked,
                    "changeSinceFirstTracked": change_since_first,
                    "changePercentSinceFirstTracked": change_percent_since_first,
                },
            }
        )

    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "cards": tracked_cards,
    }
    return payload, first_tracked_created_count, tracked_cards_updated_count


def tcgdex_language(code: str) -> str:
    return LANGUAGE_TO_TCGDEX.get(str(code).lower(), "en")


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def normalize_catalog_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    return re.sub(r"_+", "_", normalized)


def normalize_catalog_name_multilingual(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    normalized = re.sub(r"[^\w]+", "_", normalized, flags=re.UNICODE).strip("_")
    normalized = re.sub(r"_+", "_", normalized)
    return normalized or "unknown"


def normalize_number(value: str) -> str:
    cleaned = str(value).strip().lstrip("0")
    return cleaned or "0"


def to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def utc_iso_from_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def compute_staleness(last_update_utc: str | None, now_utc: str) -> tuple[str, int | None]:
    last_dt = parse_utc_timestamp(last_update_utc)
    now_dt = parse_utc_timestamp(now_utc)
    if last_dt is None or now_dt is None:
        return "unavailable", None

    age_seconds = max(0, int((now_dt - last_dt).total_seconds()))
    if age_seconds <= 86400:
        return "fresh", age_seconds
    if age_seconds <= 259200:
        return "stale", age_seconds
    return "very_stale", age_seconds


def compute_staleness_with_windows(
    last_update_utc: str | None,
    now_utc: str,
    fresh_for_seconds: int,
    stale_after_seconds: int,
) -> tuple[str, int | None]:
    last_dt = parse_utc_timestamp(last_update_utc)
    now_dt = parse_utc_timestamp(now_utc)
    if last_dt is None or now_dt is None:
        return "unavailable", None

    age_seconds = max(0, int((now_dt - last_dt).total_seconds()))
    if age_seconds <= fresh_for_seconds:
        return "fresh", age_seconds
    if age_seconds <= stale_after_seconds:
        return "stale", age_seconds
    return "very_stale", age_seconds


def expected_next_refresh_utc(last_update_utc: str | None, full_rotation_hours: int) -> str | None:
    if full_rotation_hours <= 0:
        return None
    last_dt = parse_utc_timestamp(last_update_utc)
    if last_dt is None:
        return None
    return utc_iso_from_datetime(last_dt + timedelta(hours=full_rotation_hours))


def ensure_price_record_freshness(
    record: dict,
    *,
    now_utc: str,
    set_next_expected_utc: str | None,
    fallback_fetched_utc: str | None,
    fresh_for_seconds: int,
    stale_after_seconds: int,
) -> dict:
    fetched_at_utc = record.get("fetchedAtUtc") or fallback_fetched_utc
    if fetched_at_utc is None:
        fetched_at_utc = now_utc
    fetched_at_utc = str(fetched_at_utc)
    record["fetchedAtUtc"] = fetched_at_utc

    record["nextExpectedPriceUpdateAtUtc"] = set_next_expected_utc
    status, age_seconds = compute_staleness_with_windows(
        fetched_at_utc,
        now_utc,
        fresh_for_seconds,
        stale_after_seconds,
    )
    record["staleness"] = {
        "status": status,
        "ageSeconds": age_seconds,
        "freshForSeconds": fresh_for_seconds,
        "staleAfterSeconds": stale_after_seconds,
    }
    return record


def enrich_en_current_set_payload(
    payload: dict,
    *,
    now_utc: str,
    expected_update_interval_minutes: int,
    full_rotation_hours: int,
    force_last_successful_update_utc: str | None = None,
    force_next_expected_update_utc: str | None = None,
) -> dict:
    prices = payload.get("prices")
    if not isinstance(prices, list):
        prices = []
        payload["prices"] = prices

    last_update_utc = force_last_successful_update_utc
    if last_update_utc is None:
        existing_last = payload.get("lastSuccessfulPriceUpdateAtUtc")
        if isinstance(existing_last, str) and existing_last:
            last_update_utc = existing_last
    if last_update_utc is None:
        candidate_values = []
        generated_at = payload.get("generatedAtUtc")
        if isinstance(generated_at, str) and generated_at:
            candidate_values.append(generated_at)
        for entry in prices:
            if not isinstance(entry, dict):
                continue
            fetched_at = entry.get("fetchedAtUtc")
            if isinstance(fetched_at, str) and fetched_at:
                candidate_values.append(fetched_at)

        latest_dt: datetime | None = None
        latest_value: str | None = None
        for value in candidate_values:
            parsed = parse_utc_timestamp(value)
            if parsed is None:
                continue
            if latest_dt is None or parsed > latest_dt:
                latest_dt = parsed
                latest_value = value
        last_update_utc = latest_value or now_utc

    set_next_expected_utc = force_next_expected_update_utc
    if set_next_expected_utc is None:
        existing_next = payload.get("nextExpectedPriceUpdateAtUtc")
        if isinstance(existing_next, str) and existing_next:
            set_next_expected_utc = existing_next
    if set_next_expected_utc is None:
        set_next_expected_utc = expected_next_refresh_utc(last_update_utc, full_rotation_hours)

    fresh_for_seconds = 86400
    stale_after_seconds = 172800
    set_staleness_status, set_age_seconds = compute_staleness_with_windows(
        last_update_utc,
        now_utc,
        fresh_for_seconds,
        stale_after_seconds,
    )

    payload["schemaVersion"] = SCHEMA_VERSION
    payload["generatedAtUtc"] = now_utc
    payload["status"] = "ok" if set_staleness_status == "fresh" else set_staleness_status
    payload["priceCount"] = len(prices)
    payload["lastSuccessfulPriceUpdateAtUtc"] = last_update_utc
    payload["nextExpectedPriceUpdateAtUtc"] = set_next_expected_utc
    payload["expectedUpdateIntervalMinutes"] = expected_update_interval_minutes
    payload["isLivePricing"] = False
    payload["staleness"] = {
        "status": set_staleness_status,
        "ageSeconds": set_age_seconds,
        "freshForSeconds": fresh_for_seconds,
        "staleAfterSeconds": stale_after_seconds,
    }

    enriched_prices: list[dict] = []
    for entry in prices:
        if not isinstance(entry, dict):
            continue
        enriched_prices.append(
            ensure_price_record_freshness(
                entry,
                now_utc=now_utc,
                set_next_expected_utc=set_next_expected_utc,
                fallback_fetched_utc=last_update_utc,
                fresh_for_seconds=fresh_for_seconds,
                stale_after_seconds=stale_after_seconds,
            )
        )
    payload["prices"] = enriched_prices
    return payload


def summarize_en_set_freshness(current_dir: Path) -> dict:
    oldest_update_dt: datetime | None = None
    oldest_update_utc: str | None = None
    newest_update_dt: datetime | None = None
    newest_update_utc: str | None = None

    for path in sorted(current_dir.glob("*.json")):
        if path.name == "status.json":
            continue
        payload = load_json(path)
        if not isinstance(payload, dict):
            continue

        set_update_utc = payload.get("lastSuccessfulPriceUpdateAtUtc") or payload.get("generatedAtUtc")
        if not isinstance(set_update_utc, str) or not set_update_utc:
            continue
        parsed = parse_utc_timestamp(set_update_utc)
        if parsed is None:
            continue

        if oldest_update_dt is None or parsed < oldest_update_dt:
            oldest_update_dt = parsed
            oldest_update_utc = set_update_utc
        if newest_update_dt is None or parsed > newest_update_dt:
            newest_update_dt = parsed
            newest_update_utc = set_update_utc

    return {
        "oldestSetPriceUpdateAtUtc": oldest_update_utc,
        "newestSetPriceUpdateAtUtc": newest_update_utc,
    }


def summarize_current_price_files(current_dir: Path) -> tuple[int, int]:
    if not current_dir.exists():
        return 0, 0

    set_file_count = 0
    record_count = 0
    for path in sorted(current_dir.glob("*.json")):
        if path.name == "status.json":
            continue
        payload = load_json(path)
        if not isinstance(payload, dict):
            continue
        prices = payload.get("prices")
        if not isinstance(prices, list):
            continue
        set_file_count += 1
        record_count += len(prices)
    return set_file_count, record_count


def estimate_full_rotation_hours(set_count: int, batch_size: int, interval_minutes: int) -> int:
    if set_count <= 0 or batch_size <= 0 or interval_minutes <= 0:
        return 0
    runs = (set_count + batch_size - 1) // batch_size
    total_minutes = runs * interval_minutes
    return (total_minutes + 59) // 60


def derive_last_successful_push_utc(
    previous_prices_status: dict | None,
    update_built: bool,
    ts: str,
) -> str | None:
    if update_built:
        return ts
    if not isinstance(previous_prices_status, dict):
        return None
    languages = previous_prices_status.get("languages")
    if not isinstance(languages, dict):
        return None
    en_section = languages.get("en")
    if not isinstance(en_section, dict):
        return None
    last_push = en_section.get("lastSuccessfulPushAtUtc")
    return str(last_push) if last_push else None


def build_public_price_status_payloads(
    *,
    ts: str,
    diagnostics: dict,
    config: dict,
    refresh_state: dict,
    previous_prices_status: dict | None,
) -> tuple[dict, dict, dict]:
    en_set_count, en_record_count = summarize_current_price_files(CURRENT_PRICES_EN_DIR)
    jp_set_count, jp_record_count = summarize_current_price_files(CURRENT_PRICES_JP_DIR)
    en_set_freshness = summarize_en_set_freshness(CURRENT_PRICES_EN_DIR)

    interval_minutes = max(1, safe_int(config.get("localUpdaterIntervalMinutes"), 60))
    batch_size = max(
        1,
        safe_int(
            diagnostics.get("currentPriceEnBatchSize")
            or config.get("localUpdaterDefaultBatchSize")
            or config.get("scheduledCurrentPriceBatchSize"),
            10,
        ),
    )
    full_rotation_hours = estimate_full_rotation_hours(en_set_count, batch_size, interval_minutes)

    built_this_run = str(diagnostics.get("currentPriceEnStatus")) == "built"
    last_successful_update_utc = ts if built_this_run else (refresh_state.get("lastUpdatedAtUtc") or None)
    last_successful_push_utc = derive_last_successful_push_utc(previous_prices_status, built_this_run, ts)

    now_dt = parse_utc_timestamp(ts)
    next_expected_utc = None
    if now_dt is not None:
        next_expected_utc = utc_iso_from_datetime(now_dt + timedelta(minutes=interval_minutes))

    last_batch_set_ids = diagnostics.get("currentPriceEnBatchSetIds") or refresh_state.get("lastBatchSetIds") or []
    if not isinstance(last_batch_set_ids, list):
        last_batch_set_ids = []

    last_batch_started_utc = diagnostics.get("builtAtUtc")
    last_batch_duration_seconds = safe_int(diagnostics.get("lastCycleDurationSeconds"), 0)
    if last_batch_duration_seconds <= 0:
        last_batch_duration_seconds = 0
    last_batch_finished_utc = diagnostics.get("builtAtUtc")

    staleness_status, staleness_age_seconds = compute_staleness(last_successful_update_utc, ts)
    en_status = "ok"
    if staleness_status in {"stale", "very_stale", "unavailable"}:
        en_status = staleness_status
    if en_set_count <= 0:
        en_status = "unavailable"

    en_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "game": "pokemon",
        "language": "en",
        "status": en_status,
        "currentPriceFilesAvailable": en_set_count > 0,
        "currentPriceSetFileCount": en_set_count,
        "currentPriceRecordCount": en_record_count,
        "lastSuccessfulPriceUpdateAtUtc": last_successful_update_utc,
        "lastSuccessfulPushAtUtc": last_successful_push_utc,
        "lastBatchSetIds": last_batch_set_ids,
        "lastBatchSize": batch_size,
        "lastBatchStartedAtUtc": last_batch_started_utc,
        "lastBatchFinishedAtUtc": last_batch_finished_utc,
        "lastBatchDurationSeconds": last_batch_duration_seconds,
        "nextExpectedPriceUpdateAtUtc": next_expected_utc,
        "oldestSetPriceUpdateAtUtc": en_set_freshness["oldestSetPriceUpdateAtUtc"],
        "newestSetPriceUpdateAtUtc": en_set_freshness["newestSetPriceUpdateAtUtc"],
        "expectedUpdateIntervalMinutes": interval_minutes,
        "fullRotationEstimatedHours": full_rotation_hours,
        "currency": CURRENT_PRICE_CURRENCY,
        "isLivePricing": False,
        "staleness": {
            "status": staleness_status,
            "ageSeconds": staleness_age_seconds,
            "freshForSeconds": 86400,
            "staleAfterSeconds": 259200,
        },
        "notes": [
            "Current prices are latest-known cached values.",
            "Next update time is expected, not guaranteed.",
            "Timestamps are UTC. Apps should convert to the user's local timezone.",
        ],
    }

    jp_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "game": "pokemon",
        "language": "jp",
        "status": "not_available",
        "currentPriceFilesAvailable": False,
        "currentPriceSetFileCount": jp_set_count,
        "currentPriceRecordCount": jp_record_count,
        "lastSuccessfulPriceUpdateAtUtc": None,
        "lastSuccessfulPushAtUtc": None,
        "lastBatchSetIds": [],
        "lastBatchSize": 0,
        "lastBatchStartedAtUtc": None,
        "lastBatchFinishedAtUtc": None,
        "lastBatchDurationSeconds": None,
        "nextExpectedPriceUpdateAtUtc": None,
        "expectedUpdateIntervalMinutes": None,
        "fullRotationEstimatedHours": None,
        "currency": None,
        "isLivePricing": False,
        "staleness": {
            "status": "unavailable",
            "ageSeconds": None,
            "freshForSeconds": 86400,
            "staleAfterSeconds": 259200,
        },
        "notes": [
            "Japanese catalogue support is partial.",
            "Japanese current price cache is not available yet.",
            "Timestamps are UTC. Apps should convert to the user's local timezone.",
        ],
    }

    prices_status = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "cacheVersion": diagnostics.get("cacheVersion"),
        "status": "ok",
        "intendedConsumer": "cardscanr_app",
        "priceDataMode": "batched_refresh",
        "notes": [
            "Timestamps are UTC. Apps should convert to the user's local timezone.",
            "Next update times are expected, not guaranteed.",
            "Current prices are latest-known cached values, not live market quotes.",
        ],
        "languages": {
            "en": {
                "game": "pokemon",
                "language": "en",
                "status": en_payload["status"],
                "currentPriceFilesAvailable": en_payload["currentPriceFilesAvailable"],
                "currentPriceSetFileCount": en_payload["currentPriceSetFileCount"],
                "currentPriceRecordCount": en_payload["currentPriceRecordCount"],
                "lastSuccessfulPriceUpdateAtUtc": en_payload["lastSuccessfulPriceUpdateAtUtc"],
                "lastSuccessfulPushAtUtc": en_payload["lastSuccessfulPushAtUtc"],
                "lastBatchSize": en_payload["lastBatchSize"],
                "lastBatchSetIds": en_payload["lastBatchSetIds"],
                "lastBatchStartedAtUtc": en_payload["lastBatchStartedAtUtc"],
                "lastBatchFinishedAtUtc": en_payload["lastBatchFinishedAtUtc"],
                "lastBatchDurationSeconds": en_payload["lastBatchDurationSeconds"],
                "nextExpectedPriceUpdateAtUtc": en_payload["nextExpectedPriceUpdateAtUtc"],
                "oldestSetPriceUpdateAtUtc": en_payload["oldestSetPriceUpdateAtUtc"],
                "newestSetPriceUpdateAtUtc": en_payload["newestSetPriceUpdateAtUtc"],
                "expectedUpdateIntervalMinutes": en_payload["expectedUpdateIntervalMinutes"],
                "fullRotationEstimatedHours": en_payload["fullRotationEstimatedHours"],
                "staleness": en_payload["staleness"],
                "sourceSummary": {
                    "primarySource": CURRENT_PRICE_SOURCE,
                    "currency": CURRENT_PRICE_CURRENCY,
                    "isLivePricing": False,
                },
            },
            "jp": {
                "game": "pokemon",
                "language": "jp",
                "status": "catalogue_only",
                "currentPriceFilesAvailable": False,
                "currentPriceSetFileCount": jp_payload["currentPriceSetFileCount"],
                "currentPriceRecordCount": jp_payload["currentPriceRecordCount"],
                "lastSuccessfulPriceUpdateAtUtc": None,
                "nextExpectedPriceUpdateAtUtc": None,
                "staleness": jp_payload["staleness"],
                "sourceSummary": {
                    "primarySource": SOURCE_ID_TCGDEX,
                    "currency": None,
                    "isLivePricing": False,
                },
                "notes": jp_payload["notes"],
            },
        },
    }

    return prices_status, en_payload, jp_payload


def resolve_state_path(config: dict) -> Path:
    raw = str(config.get("scheduledCurrentPriceStatePath") or "data/scheduled_price_refresh_state.json").strip()
    if not raw:
        raw = "data/scheduled_price_refresh_state.json"
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def load_scheduled_refresh_state(path: Path) -> dict:
    default_state = {
        "schemaVersion": SCHEMA_VERSION,
        "enCurrentPriceCursor": 0,
        "lastUpdatedAtUtc": None,
        "lastBatchSetIds": [],
    }
    if not path.exists():
        return default_state

    try:
        payload = load_json(path)
    except OSError:
        return default_state

    if not isinstance(payload, dict):
        return default_state

    merged = {**default_state, **payload}
    try:
        merged["enCurrentPriceCursor"] = max(0, int(merged.get("enCurrentPriceCursor", 0)))
    except (TypeError, ValueError):
        merged["enCurrentPriceCursor"] = 0
    if not isinstance(merged.get("lastBatchSetIds"), list):
        merged["lastBatchSetIds"] = []
    return merged


def save_scheduled_refresh_state(path: Path, payload: dict) -> None:
    write_json(path, payload)


def resolve_batch_size(config: dict) -> int:
    env_value = os.getenv("CARDSCANR_CURRENT_PRICE_BATCH_SIZE", "").strip()
    if env_value:
        try:
            parsed = int(env_value)
            if parsed > 0:
                return parsed
        except ValueError:
            pass

    configured = config.get("scheduledCurrentPriceBatchSize", config.get("localUpdaterDefaultBatchSize", 10))
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return 10
    return max(1, value)


def price_sort_key(entry: dict) -> tuple[str, str, str, str]:
    return (
        normalize_number(entry.get("collectorNumber", "")),
        str(entry.get("collectorNumber", "")),
        str(entry.get("normalizedName", "")),
        str(entry.get("variant", "")),
    )


def catalogue_card_sort_key(entry: dict) -> tuple[str, str, str]:
    return (
        normalize_number(entry.get("collectorNumber", "")),
        str(entry.get("collectorNumber", "")),
        str(entry.get("normalizedName", "")),
    )


# ---------------------------------------------------------------------------
# Seed price generation
# ---------------------------------------------------------------------------
CONDITION_MULTIPLIER = {
    "near_mint": 1.0,
    "lightly_played": 0.70,
    "moderately_played": 0.50,
    "heavily_played": 0.30,
}

SEED_PRICES: dict[str, float] = {
    "charizard": 450.0,
    "blastoise": 210.0,
    "venusaur": 185.0,
    "rizaadon": 380.0,
    "kamekkusu": 175.0,
    "fushigibana": 160.0,
}

DEFAULT_SEED_PRICE = 10.0


def seed_price(normalized_name: str, condition: str) -> tuple[float, float, float]:
    base = SEED_PRICES.get(normalized_name.lower(), DEFAULT_SEED_PRICE)
    mult = CONDITION_MULTIPLIER.get(condition, 1.0)
    market = round(base * mult, 2)
    low = round(market * 0.85, 2)
    high = round(market * 1.30, 2)
    return market, low, high


def manual_seed_price_info(card: dict) -> dict:
    market, low, high = seed_price(card["normalizedName"], card["condition"])
    return {
        "marketPrice": market,
        "lowPrice": low,
        "highPrice": high,
        "currency": "AUD",
        "source": SOURCE_ID_MANUAL_SEED,
    }


def build_price_entry(card: dict, ts: str, price_info: dict) -> dict:
    return {
        "canonicalId": card["canonicalId"],
        "setId": card["setId"],
        "collectorNumber": card["collectorNumber"],
        "normalizedName": card["normalizedName"],
        "variant": card["variant"],
        "condition": card["condition"],
        "currency": price_info["currency"],
        "marketPrice": price_info["marketPrice"],
        "lowPrice": price_info["lowPrice"],
        "highPrice": price_info["highPrice"],
        "source": price_info["source"],
        "fetchedAtUtc": ts,
    }


def _derive_catalogue_status_from_sets_file(game: str, language: str) -> str | None:
    """Read sets.json and map catalogueStatus to the supported-languages enum."""
    sets_path = CATALOG_DIR / game / language / "sets.json"
    if not sets_path.exists():
        return None
    try:
        data = load_json(sets_path)
    except OSError:
        return None
    if not isinstance(data, dict):
        return None
    raw = str(data.get("catalogueStatus", ""))
    if raw in {"built"}:
        return "available"
    if raw in {"partial_built"}:
        return "partial"
    if raw in {"not_built_yet"}:
        return "unavailable"
    return None


def _derive_pricing_status_from_price_status(game: str, language: str) -> str | None:
    """Read prices/current/{game}/{language}/status.json and return pricingStatus."""
    status_path = CATALOG_DIR.parent / "prices" / "current" / game / language / "status.json"
    if not status_path.exists():
        return None
    try:
        data = load_json(status_path)
    except OSError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("currentPriceFilesAvailable") is True:
        return "available"
    return "unavailable"


def build_supported_language_manifest(ts: str) -> dict:
    """Build supported-languages.json from curated config + live status derivation."""
    config_data: dict = {}
    if SUPPORTED_LANGUAGES_CONFIG_PATH.exists():
        try:
            config_data = load_json(SUPPORTED_LANGUAGES_CONFIG_PATH)
        except OSError:
            pass
    if not isinstance(config_data, dict):
        config_data = {}

    base_languages: list[dict] = [
        entry for entry in config_data.get("languages", []) if isinstance(entry, dict)
    ]

    languages: list[dict] = []
    for entry in base_languages:
        lang_entry = dict(entry)
        game = str(lang_entry.get("game", ""))
        language = str(lang_entry.get("language", ""))

        # Derive catalogueStatus from live sets.json (never demotes human-set "planned" status)
        if lang_entry.get("catalogueStatus") not in {"planned", "unavailable"}:
            derived_cat = _derive_catalogue_status_from_sets_file(game, language)
            if derived_cat is not None:
                lang_entry["catalogueStatus"] = derived_cat

        # Derive pricingStatus from live price status file (never auto-promotes to "available"
        # if visibility is "planned" — that is a human editorial decision, or if
        # allowPricingAutoPromotion is explicitly false in the config)
        allow_auto = lang_entry.pop("allowPricingAutoPromotion", True)
        if lang_entry.get("visibility") not in {"planned", "hidden", "internal"} and allow_auto:
            derived_price = _derive_pricing_status_from_price_status(game, language)
            if derived_price is not None:
                # Allow downgrade to "unavailable" when status file says so, but
                # never silently flip "planned" or "hidden" languages to "available".
                if derived_price == "available":
                    lang_entry["pricingStatus"] = "available"
                elif derived_price == "unavailable" and lang_entry.get("pricingStatus") not in {"planned"}:
                    lang_entry["pricingStatus"] = "unavailable"

        languages.append(lang_entry)

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "languages": languages,
    }


def build_supported_market_manifest(ts: str) -> dict:
    """Build supported-markets.json from curated config (timestamp-only refresh)."""
    config_data: dict = {}
    if SUPPORTED_MARKETS_CONFIG_PATH.exists():
        try:
            config_data = load_json(SUPPORTED_MARKETS_CONFIG_PATH)
        except OSError:
            pass
    if not isinstance(config_data, dict):
        config_data = {}

    markets: list[dict] = [
        dict(entry) for entry in config_data.get("markets", []) if isinstance(entry, dict)
    ]

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "markets": markets,
    }


def build_api_manifest(ts: str) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "apiVersion": "v1",
        "generatedAtUtc": ts,
        "baseUrl": BASE_URL,
        "name": "CardScanR Internal Data API",
        "intendedConsumer": "cardscanr_app",
        "publicDeveloperApi": False,
        "thirdPartyUseSupported": False,
        "status": "internal_static_app_data_layer",
        "authRequired": False,
        "notes": [
            "This data layer is intended for the CardScanR mobile app and future CardScanR web app.",
            "It is not a supported public developer API.",
            "Static files may be publicly reachable for app delivery and caching.",
            "Future authenticated app routes may be served through Cloudflare Workers or Supabase.",
            "Current price files may be overwritten each build.",
            "Per-set current price files are latest-known values sourced from official provider API payloads and are not guaranteed live.",
            "Price freshness/status files provide UTC metadata for app-side update visibility.",
            "Tracked history means history since CardScanR started tracking the card.",
            "Lifetime/all-time market history is not currently provided.",
            "Images are referenced by URL and are not mirrored into this cache yet.",
        ],
        "endpoints": [
            {
                "id": "index",
                "method": "GET",
                "path": "/index.json",
                "description": "Dataset manifest for CardScanR cache files",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "app_config",
                "method": "GET",
                "path": "/app-config.json",
                "description": "Remote app feature flags and cache settings",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "api_manifest",
                "method": "GET",
                "path": "/api-manifest.json",
                "description": "Internal CardScanR app data API manifest",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "schemas",
                "method": "GET",
                "path": "/schemas.json",
                "description": "Machine-readable schema documentation for CardScanR cache files",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "api_notes",
                "method": "GET",
                "path": "/api-notes.json",
                "description": "Internal data layer notes and product constraints",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "current_prices",
                "method": "GET",
                "path": "/prices/pokemon/{language}/sample.json",
                "description": "Current tracked price cache for a language",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "current_prices_by_set",
                "method": "GET",
                "path": "/prices/current/pokemon/en/{setId}.json",
                "description": "Latest-known English Pokemon current prices by set where official API pricing is available",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "prices_status",
                "method": "GET",
                "path": "/prices/status.json",
                "description": "App-facing UTC freshness/status summary for CardScanR current price cache",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "current_prices_en_status",
                "method": "GET",
                "path": "/prices/current/pokemon/en/status.json",
                "description": "App-facing UTC freshness/status for Pokemon EN current prices",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "current_prices_jp_status",
                "method": "GET",
                "path": "/prices/current/pokemon/jp/status.json",
                "description": "App-facing UTC freshness/status for Pokemon JP current prices",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "catalog_pokemon_en_sets",
                "method": "GET",
                "path": "/catalog/pokemon/en/sets.json",
                "description": "English Pokemon catalogue set manifest built from official PokemonTCG API data",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "catalog_pokemon_en_cards",
                "method": "GET",
                "path": "/catalog/pokemon/en/cards/{setId}.json",
                "description": "English Pokemon catalogue cards for a single set",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "catalog_pokemon_jp_sets",
                "method": "GET",
                "path": "/catalog/pokemon/jp/sets.json",
                "description": "Japanese Pokemon catalogue set manifest built from official TCGdex data",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "catalog_pokemon_jp_cards",
                "method": "GET",
                "path": "/catalog/pokemon/jp/cards/{setId}.json",
                "description": "Japanese Pokemon catalogue cards for a single set built from official TCGdex data",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "current_prices_by_set_jp",
                "method": "GET",
                "path": "/prices/current/pokemon/jp/{setId}.json",
                "description": "Latest-known Japanese Pokemon current prices by set when official/free source pricing is available",
                "authRequired": False,
                "cacheable": True,
            },
            {
                "id": "tracked_history",
                "method": "GET",
                "path": "/history/tracked-cards.json",
                "description": "CardScanR tracked price history summary",
                "authRequired": False,
                "cacheable": True,
            },
        ],
        "futureInternalDynamicRoutes": [
            "/api/v1/card",
            "/api/v1/price",
            "/api/v1/history",
            "/api/v1/search",
        ],
    }


def build_api_notes(ts: str) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "intendedConsumer": "cardscanr_app",
        "publicDeveloperApi": False,
        "thirdPartyUseSupported": False,
        "notes": [
            "This is a static internal data layer for the CardScanR app.",
            "This is not a supported public developer API.",
            "Current price files may be overwritten each build.",
            "Per-set current price files are latest-known snapshots, not guaranteed live quotes.",
            "Price freshness/status metadata files are UTC and intended for app visibility and countdown UX.",
            "Card detail freshness should use priceRecord.fetchedAtUtc, priceRecord.nextExpectedPriceUpdateAtUtc, and priceRecord.staleness.status.",
            "Set-level freshness should use setFile.lastSuccessfulPriceUpdateAtUtc and setFile.nextExpectedPriceUpdateAtUtc.",
            "Current prices are cached latest-known values and are not guaranteed live quotes.",
            "Manual refresh in the app should fetch latest cache first, then optionally try live lookup when enabled.",
            "Manual refresh must never overwrite a valid saved price with no result, unavailable, or error responses.",
            "Future backend manual refresh may queue card-level priority refresh when backend support exists.",
            "Currency is provided on each price record.",
            "Tracked history means history since CardScanR started tracking the card.",
            "Lifetime/all-time market history is not currently provided.",
            "Images are referenced by URL and are not mirrored into this cache yet.",
            "Japanese Pokemon catalogue files are built from official TCGdex REST endpoints using the ja API language path.",
            "CardScanR cache language remains jp even when the upstream TCGdex API language path is ja.",
            "Japanese current price files are optional and are only written when official/free source pricing is available.",
            "Future authenticated app routes may be served by Cloudflare Workers or Supabase.",
        ],
    }


def build_schemas(ts: str) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "schemas": {
            "index_dataset_entry": {
                "requiredFields": ["id", "url", "sha256", "type", "description", "updatedAtUtc"],
                "notes": [
                    "Backwards compatible fields id/url/sha256 remain required.",
                    "Richer metadata may include game, language, schemaVersion, and recommendedCacheTtlSeconds.",
                ],
            },
            "app_config": {
                "requiredFields": ["featureFlags"],
                "notes": ["Static remote app settings used by CardScanR."],
            },
            "api_manifest": {
                "requiredFields": [
                    "schemaVersion",
                    "apiVersion",
                    "generatedAtUtc",
                    "baseUrl",
                    "name",
                    "intendedConsumer",
                    "publicDeveloperApi",
                    "thirdPartyUseSupported",
                    "status",
                    "authRequired",
                    "notes",
                    "endpoints",
                ],
                "notes": [
                    "This describes the CardScanR internal data API, not a public developer platform.",
                    "Images are referenced by URL only and are not mirrored into this cache yet.",
                ],
            },
            "api_notes": {
                "requiredFields": [
                    "schemaVersion",
                    "generatedAtUtc",
                    "intendedConsumer",
                    "publicDeveloperApi",
                    "thirdPartyUseSupported",
                    "notes",
                ],
                "notes": ["Consumer-facing summary of the internal app data layer constraints."],
            },
            "diagnostics": {
                "requiredFields": [
                    "buildStatus",
                    "builtAtUtc",
                    "cacheVersion",
                    "cardsRequested",
                    "cardsPriced",
                    "tcgdexAttempted",
                    "tcgdexMatched",
                    "tcgdexNoMatch",
                    "livePriceCount",
                    "manualFallbackCount",
                    "noResultCount",
                    "errorCount",
                    "sourcesUsed",
                    "datasetsBuilt",
                    "trackedHistoryWritten",
                    "trackedCardsTotal",
                    "dailyHistoryFilesWritten",
                    "firstTrackedCreatedCount",
                    "trackedCardsUpdatedCount",
                ],
                "notes": ["Build telemetry for current cache and CardScanR tracked history generation."],
            },
            "catalogue_sets_file": {
                "requiredFields": [
                    "schemaVersion",
                    "generatedAtUtc",
                    "game",
                    "language",
                    "catalogueStatus",
                    "cardsAvailable",
                    "sets",
                    "source",
                    "notes",
                ],
                "notes": [
                    "English Pokemon catalogue sets are built from official PokemonTCG API endpoints.",
                    "Japanese Pokemon catalogue sets are built from official TCGdex REST endpoints using the ja upstream language path.",
                ],
            },
            "catalogue_cards_file": {
                "requiredFields": [
                    "schemaVersion",
                    "generatedAtUtc",
                    "game",
                    "language",
                    "setId",
                    "setName",
                    "source",
                    "catalogueStatus",
                    "cardCount",
                    "cards",
                ],
                "notes": [
                    "Catalogue card records store image URLs only.",
                    "Use imageSmall, imageLarge, imageSource, and imageCached: false.",
                    "Japanese catalogue card records may include null values for metadata that is not present in set-level TCGdex responses.",
                ],
            },
            "current_price_record": {
                "requiredFields": [
                    "canonicalId",
                    "setId",
                    "collectorNumber",
                    "normalizedName",
                    "variant",
                    "condition",
                    "currency",
                    "marketPrice",
                    "lowPrice",
                    "highPrice",
                    "source",
                    "fetchedAtUtc",
                    "nextExpectedPriceUpdateAtUtc",
                    "staleness",
                ],
                "notes": [
                    "Current price cache entry.",
                    "Per-set current price files are latest-known snapshots and may be overwritten each build.",
                    "At least one of marketPrice, lowPrice, or highPrice should be numeric when present.",
                    "Currency is stored per price record.",
                    "Use fetchedAtUtc and staleness fields for app card-detail freshness messaging.",
                    "Japanese price files are only emitted when official/free source pricing is actually available.",
                ],
            },
            "current_price_set_file": {
                "requiredFields": [
                    "schemaVersion",
                    "generatedAtUtc",
                    "game",
                    "language",
                    "setId",
                    "setName",
                    "source",
                    "currency",
                    "status",
                    "priceCount",
                    "lastSuccessfulPriceUpdateAtUtc",
                    "nextExpectedPriceUpdateAtUtc",
                    "expectedUpdateIntervalMinutes",
                    "isLivePricing",
                    "staleness",
                    "prices",
                ],
                "notes": [
                    "English Pokemon current prices by set are built from PokemonTCG API card pricing fields.",
                    "Japanese Pokemon current prices by set are optional and only appear when official/free source pricing is available.",
                    "These files are latest-known current snapshots, not lifetime/all-time price history.",
                    "App card detail freshness should combine set-level and record-level freshness timestamps.",
                    "Manual refresh should check cache first and never overwrite valid values with no-result/error data.",
                    "Tracked historical movement remains limited to CardScanR-tracked cards.",
                ],
            },
            "prices_status_file": {
                "requiredFields": [
                    "schemaVersion",
                    "generatedAtUtc",
                    "cacheVersion",
                    "status",
                    "languages",
                ],
                "notes": [
                    "Top-level app-facing UTC status summary for current price freshness.",
                    "Contains language-level status, staleness, and expected update metadata.",
                ],
            },
            "current_price_language_status_file": {
                "requiredFields": [
                    "schemaVersion",
                    "generatedAtUtc",
                    "game",
                    "language",
                    "status",
                    "staleness",
                ],
                "notes": [
                    "Language-specific app-facing UTC price freshness metadata.",
                    "Includes expected update cadence and batch metadata for EN local-first rotation.",
                ],
            },
            "tracked_cards_record": {
                "requiredFields": [
                    "canonicalId",
                    "game",
                    "language",
                    "setId",
                    "collectorNumber",
                    "normalizedName",
                    "variant",
                    "condition",
                    "firstTrackedAtUtc",
                    "lastTrackedAtUtc",
                    "firstTrackedPrice",
                    "latestPrice",
                    "trackingStats",
                ],
                "notes": [
                    "CardScanR tracked history means history since CardScanR started tracking the card.",
                    "This is not lifetime/all-time market history.",
                ],
            },
            "daily_tracked_history_file": {
                "requiredFields": ["schemaVersion", "generatedAtUtc", "date", "game", "language", "prices"],
                "notes": [
                    "Daily snapshots only for tracked cards from data/cards_to_track.json for now.",
                    "This is append-by-day history, not a full all-card historical archive.",
                ],
            },
        },
        "notes": [
            "CardScanR tracked history starts from the first tracked build for each card.",
            "Images are referenced by URL and are not mirrored into this cache yet.",
        ],
    }


def build_catalog_sets_placeholder(game: str, language: str, ts: str) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "game": game,
        "language": language,
        "catalogueStatus": "not_built_yet",
        "cardsAvailable": False,
        "sets": [],
        "source": None,
        "notes": [
            "Full catalogue cache is planned but not built yet.",
            "Card records should store image URLs only when implemented.",
        ],
    }


def pokemon_tcg_headers() -> dict:
    headers = {}
    api_key = os.getenv("POKEMON_TCG_API_KEY")
    if api_key:
        headers["X-Api-Key"] = api_key
    return headers


def pokemon_tcg_get(endpoint: str, params: dict | None = None) -> dict:
    if remaining_current_price_requests(SOURCE_ID_POKEMON_TCG_API) is not None and remaining_current_price_requests(SOURCE_ID_POKEMON_TCG_API) <= 0:
        raise RequestCapReachedError(
            f"current price request cap reached before requesting {endpoint}",
            provider=SOURCE_ID_POKEMON_TCG_API,
        )
    response = requests.get(
        f"{POKEMON_TCG_API_BASE}/{endpoint.lstrip('/')}",
        params=params or {},
        headers=pokemon_tcg_headers(),
        timeout=30,
    )
    payload: object = {}
    detail_text = ""
    try:
        payload = response.json()
        detail_text = extract_error_detail(payload)
    except ValueError:
        detail_text = (response.text or "").strip()

    is_rate_limited = response.status_code == 429
    if not is_rate_limited and response.status_code in {401, 403}:
        is_rate_limited = is_rate_limit_detail_text(detail_text)
    if not is_rate_limited and response.status_code == 400:
        is_rate_limited = is_rate_limit_detail_text(detail_text)
    if not is_rate_limited and isinstance(payload, dict):
        is_rate_limited = is_rate_limit_detail_text(detail_text)

    if is_rate_limited:
        mark_request_attempt(success=False, rate_limited=True, provider=SOURCE_ID_POKEMON_TCG_API)
        raise ProviderRateLimitError(
            provider=SOURCE_ID_POKEMON_TCG_API,
            status_code=response.status_code,
            detail=detail_text or "provider reported a request limit",
        )

    if response.status_code >= 400:
        mark_request_attempt(success=False, provider=SOURCE_ID_POKEMON_TCG_API)
        response.raise_for_status()

    mark_request_attempt(success=True, provider=SOURCE_ID_POKEMON_TCG_API)
    if not isinstance(payload, dict):
        raise ValueError(f"PokemonTCG API returned non-object payload for {endpoint}")
    return payload


def fetch_pokemon_tcg_paginated(
    endpoint: str,
    *,
    base_params: dict | None = None,
    page_size: int = 250,
    max_pages: int = 1000,
    sleep_seconds: float = 0.0,
) -> tuple[list[dict], int | None, int]:
    records: list[dict] = []
    total_count: int | None = None
    pages_fetched = 0

    for page in range(1, max_pages + 1):
        params = dict(base_params or {})
        params.update({"page": page, "pageSize": page_size})
        payload = pokemon_tcg_get(endpoint, params=params)
        data = payload.get("data", [])
        if not isinstance(data, list):
            raise ValueError(f"PokemonTCG API returned non-list data for {endpoint}")

        records.extend([item for item in data if isinstance(item, dict)])
        pages_fetched += 1

        if isinstance(payload.get("totalCount"), int):
            total_count = payload["totalCount"]
        if total_count is not None and len(records) >= total_count:
            break
        if len(data) < page_size:
            break
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return records, total_count, pages_fetched


def build_catalog_set_record(set_data: dict) -> dict:
    images = set_data.get("images") if isinstance(set_data.get("images"), dict) else {}
    return {
        "id": set_data.get("id"),
        "name": set_data.get("name"),
        "series": set_data.get("series"),
        "printedTotal": set_data.get("printedTotal"),
        "total": set_data.get("total"),
        "releaseDate": set_data.get("releaseDate"),
        "updatedAt": set_data.get("updatedAt"),
        "ptcgoCode": set_data.get("ptcgoCode"),
        "symbolUrl": images.get("symbol"),
        "logoUrl": images.get("logo"),
        "imageSource": SOURCE_ID_POKEMON_TCG_API,
        "imageCached": False,
    }


def build_catalog_card_record(card: dict, set_id: str, set_name: str) -> dict:
    images = card.get("images") if isinstance(card.get("images"), dict) else {}
    normalized_name = normalize_catalog_name(card.get("name", ""))
    collector_number = str(card.get("number") or "")

    return {
        "canonicalBaseId": f"pokemon|en|{set_id}|{collector_number}|{normalized_name}",
        "game": "pokemon",
        "language": "en",
        "setId": set_id,
        "setName": set_name,
        "collectorNumber": collector_number,
        "name": card.get("name"),
        "normalizedName": normalized_name,
        "rarity": card.get("rarity"),
        "supertype": card.get("supertype"),
        "supertypes": [card.get("supertype")] if isinstance(card.get("supertype"), str) and card.get("supertype") else [],
        "subtypes": card.get("subtypes") if isinstance(card.get("subtypes"), list) else [],
        "types": card.get("types") if isinstance(card.get("types"), list) else [],
        "hp": card.get("hp"),
        "artist": card.get("artist"),
        "illustrator": card.get("artist"),
        "imageSmall": images.get("small"),
        "imageLarge": images.get("large"),
        "imageSource": SOURCE_ID_POKEMON_TCG_API,
        "imageCached": False,
        "providerIds": {
            "pokemonTcgApi": card.get("id"),
            "tcgdex": None,
            "pokewallet": None,
        },
        "pricingReferences": {
            "tcgplayerAvailable": isinstance(card.get("tcgplayer"), dict),
            "cardmarketAvailable": False,
        },
        "externalIds": {
            "pokemonTcgApiId": card.get("id"),
            "tcgdexCardId": None,
            "tcgplayerProductId": None,
            "pricechartingId": None,
        },
        "availableVariants": [],
    }


def build_tcgdex_card_image_url(language: str, serie_id: str | None, set_id: str, local_id: str, quality: str) -> str | None:
    if not serie_id or not set_id or not local_id:
        return None
    return f"https://assets.tcgdex.net/{language}/{serie_id}/{set_id}/{local_id}/{quality}.webp"


def build_japanese_catalog_set_record(set_data: dict) -> dict:
    card_count = set_data.get("cardCount") if isinstance(set_data.get("cardCount"), dict) else {}
    serie = set_data.get("serie") if isinstance(set_data.get("serie"), dict) else {}
    return {
        "id": set_data.get("id"),
        "name": set_data.get("name"),
        "series": serie.get("name"),
        "printedTotal": card_count.get("official"),
        "total": card_count.get("total"),
        "releaseDate": set_data.get("releaseDate"),
        "updatedAt": set_data.get("updated"),
        "ptcgoCode": None,
        "symbolUrl": set_data.get("symbol"),
        "logoUrl": set_data.get("logo"),
        "imageSource": SOURCE_ID_TCGDEX,
        "imageCached": False,
    }


def build_japanese_catalog_card_record(card: dict, set_id: str, set_name: str, serie_id: str | None) -> dict:
    collector_number = str(card.get("localId") or "")
    normalized_name = normalize_catalog_name_multilingual(card.get("name", ""))
    available_variants = []
    variants = card.get("variants") if isinstance(card.get("variants"), dict) else {}
    for variant_name, is_available in sorted(variants.items()):
        if is_available:
            available_variants.append(variant_name)

    return {
        "canonicalBaseId": f"pokemon|jp|{set_id}|{collector_number}|{normalized_name}",
        "game": "pokemon",
        "language": "jp",
        "setId": set_id,
        "setName": set_name,
        "collectorNumber": collector_number,
        "name": card.get("name"),
        "normalizedName": normalized_name,
        "rarity": card.get("rarity"),
        "category": card.get("category"),
        "illustrator": card.get("illustrator"),
        "supertype": card.get("category"),
        "supertypes": [card.get("category")] if isinstance(card.get("category"), str) and card.get("category") else [],
        "subtypes": card.get("types") if isinstance(card.get("types"), list) else [],
        "types": card.get("types") if isinstance(card.get("types"), list) else [],
        "hp": card.get("hp"),
        "imageSmall": build_tcgdex_card_image_url("ja", serie_id, set_id, collector_number, "low"),
        "imageLarge": build_tcgdex_card_image_url("ja", serie_id, set_id, collector_number, "high"),
        "imageSource": SOURCE_ID_TCGDEX,
        "imageCached": False,
        "providerIds": {
            "pokemonTcgApi": None,
            "tcgdex": card.get("id"),
            "pokewallet": None,
        },
        "pricingReferences": {
            "tcgplayerAvailable": False,
            "cardmarketAvailable": False,
        },
        "externalIds": {
            "pokemonTcgApiId": None,
            "tcgdexCardId": card.get("id"),
            "tcgplayerProductId": None,
            "pricechartingId": None,
        },
        "availableVariants": available_variants,
    }


def parse_tcgdex_card_id(card_id: str) -> tuple[str, str] | None:
    if not isinstance(card_id, str) or "-" not in card_id:
        return None
    set_id, local_id = card_id.rsplit("-", 1)
    set_id = set_id.strip()
    local_id = local_id.strip()
    if not set_id or not local_id:
        return None
    return set_id, local_id


def build_japanese_global_card_record(card: dict, set_id: str, set_name: str, local_id: str) -> dict:
    name = card.get("name")
    normalized_name = normalize_catalog_name_multilingual(name)
    image = card.get("image")
    if image is None:
        images = card.get("images") if isinstance(card.get("images"), dict) else {}
        image = images.get("small") or images.get("large")
    return {
        "canonicalBaseId": f"pokemon|jp|{set_id}|{local_id}|{normalized_name}",
        "game": "pokemon",
        "language": "jp",
        "setId": set_id,
        "setName": set_name,
        "collectorNumber": local_id,
        "name": name,
        "normalizedName": normalized_name,
        "rarity": card.get("rarity"),
        "category": card.get("category"),
        "illustrator": card.get("illustrator"),
        "supertype": card.get("category"),
        "supertypes": [card.get("category")] if isinstance(card.get("category"), str) and card.get("category") else [],
        "subtypes": card.get("types") if isinstance(card.get("types"), list) else [],
        "types": card.get("types") if isinstance(card.get("types"), list) else [],
        "hp": card.get("hp"),
        "imageSmall": image,
        "imageLarge": image,
        "imageSource": SOURCE_ID_TCGDEX,
        "imageCached": False,
        "providerIds": {
            "pokemonTcgApi": None,
            "tcgdex": card.get("id"),
            "pokewallet": None,
        },
        "pricingReferences": {
            "tcgplayerAvailable": False,
            "cardmarketAvailable": False,
        },
        "externalIds": {
            "pokemonTcgApiId": None,
            "tcgdexCardId": card.get("id"),
            "tcgplayerProductId": None,
            "pricechartingId": None,
        },
        "availableVariants": [],
    }


def merge_japanese_set_cards(set_detail_records: list[dict], global_records: list[dict]) -> tuple[list[dict], int]:
    merged: list[dict] = []
    seen_canonical_base_ids: set[str] = set()
    seen_tcgdex_ids: set[str] = set()
    duplicates_removed = 0

    for record in set_detail_records + global_records:
        external_ids = record.get("externalIds") if isinstance(record.get("externalIds"), dict) else {}
        tcgdex_id = str(external_ids.get("tcgdexCardId") or "").strip()
        canonical_base_id = str(record.get("canonicalBaseId") or "").strip()
        if tcgdex_id and tcgdex_id in seen_tcgdex_ids:
            duplicates_removed += 1
            continue
        if canonical_base_id and canonical_base_id in seen_canonical_base_ids:
            duplicates_removed += 1
            continue
        if tcgdex_id:
            seen_tcgdex_ids.add(tcgdex_id)
        if canonical_base_id:
            seen_canonical_base_ids.add(canonical_base_id)
        merged.append(record)

    return merged, duplicates_removed


def fetch_global_jp_cards(max_probe: int) -> list[dict]:
    response = requests.get("https://api.tcgdex.net/v2/ja/cards", timeout=90)
    mark_request_attempt(
        success=response.status_code < 400,
        rate_limited=(response.status_code == 429),
        provider=SOURCE_ID_TCGDEX,
    )
    if response.status_code == 429:
        raise ProviderRateLimitError(provider=SOURCE_ID_TCGDEX, status_code=429, detail="TCGdex global cards endpoint")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("TCGdex returned non-list payload for /v2/ja/cards")
    if max_probe > 0:
        return [item for item in payload[:max_probe] if isinstance(item, dict)]
    return [item for item in payload if isinstance(item, dict)]


def build_japanese_pokemon_catalogue(
    ts: str, config: dict
) -> tuple[dict, list[tuple[str, str, Path]], dict, list[tuple[str, str, Path]], dict]:
    metrics = {
        "catalogueJpStatus": "not_built_yet",
        "catalogueJpProviderLanguage": "ja",
        "catalogueJpSourceStrategy": "tcgdex_set_details_plus_global_card_list",
        "catalogueJpFetchStrategy": str(config.get("japaneseCatalogueFetchStrategy", "tcgdex_set_by_set")),
        "catalogueJpSetCount": 0,
        "catalogueJpSetsAttempted": 0,
        "catalogueJpSetsBuilt": 0,
        "catalogueJpSetsFailed": 0,
        "catalogueJpCardsFetched": 0,
        "catalogueJpCardsFromSetDetails": 0,
        "catalogueJpCardsFromGlobalList": 0,
        "catalogueJpCardsMergedTotal": 0,
        "catalogueJpDuplicateCardsRemoved": 0,
        "catalogueJpGlobalCardsFetched": 0,
        "catalogueJpGlobalCardsGrouped": 0,
        "catalogueJpGlobalCardsSkippedUnparseableId": 0,
        "catalogueJpGlobalCardsSkippedUnknownSet": 0,
        "catalogueJpCoverageImprovedByGlobalFallback": False,
        "catalogueJpSetsSkippedEmptyCards": 0,
        "catalogueJpFailedSetIds": [],
        "catalogueJpSkippedEmptySetIds": [],
        "catalogueJpEmptySetIds": [],
        "catalogueJpStoppedReason": None,
    }
    current_price_metrics = {
        "currentPriceJpStatus": "not_built_yet",
        "currentPriceJpSetsWritten": 0,
        "currentPriceJpPriceRecordsWritten": 0,
        "currentPriceJpSkippedNoPriceSets": 0,
    }

    if not config.get("fullCatalogueEnabled", True) or not config.get("buildJapaneseFromTcgdex", True):
        metrics["catalogueJpStoppedReason"] = "disabled_by_config"
        current_price_metrics["currentPriceJpStatus"] = "disabled_by_config"
        return build_catalog_sets_placeholder("pokemon", "jp", ts), [], metrics, [], current_price_metrics

    max_sets_per_run = int(config.get("jpMaxSetsPerRun", 9999))
    sleep_seconds = float(config.get("jpCatalogueRequestSleepSeconds", 0.15))

    print("[build_price_cache] Fetching TCGdex sets for JP catalogue")
    response = requests.get("https://api.tcgdex.net/v2/ja/sets", timeout=30)
    mark_request_attempt(
        success=response.status_code < 400,
        rate_limited=(response.status_code == 429),
        provider=SOURCE_ID_TCGDEX,
    )
    response.raise_for_status()
    sets = response.json()
    if not isinstance(sets, list):
        raise ValueError("TCGdex returned non-list payload for /v2/ja/sets")

    filtered_sets = [item for item in sets if isinstance(item, dict) and item.get("id")]
    filtered_sets.sort(key=lambda item: (str(item.get("releaseDate") or ""), str(item.get("id") or "")))

    unique_sets: list[dict] = []
    seen_set_ids: set[str] = set()
    for item in filtered_sets:
        raw_id = str(item.get("id") or "").strip()
        if not raw_id:
            continue
        key = raw_id.lower()
        if key in seen_set_ids:
            continue
        seen_set_ids.add(key)
        unique_sets.append(item)

    if max_sets_per_run > 0:
        unique_sets = unique_sets[:max_sets_per_run]

    metrics["catalogueJpSetCount"] = len(unique_sets)
    if max_sets_per_run > 0 and len(unique_sets) < len(sets):
        metrics["catalogueJpStoppedReason"] = "max_sets_per_run_reached"

    set_name_by_id_lower: dict[str, str] = {}
    canonical_set_id_by_lower: dict[str, str] = {}
    for item in unique_sets:
        set_id = str(item.get("id") or "").strip()
        if not set_id:
            continue
        key = set_id.lower()
        canonical_set_id_by_lower[key] = set_id
        set_name_by_id_lower[key] = str(item.get("name") or set_id)

    grouped_global_cards: dict[str, list[dict]] = {}
    use_global_fallback = bool(
        config.get("japaneseCatalogueFallbackCardListEnabled", True)
        and config.get("jpUseGlobalCardListFallback", True)
    )
    if use_global_fallback:
        max_global_cards_probe = int(config.get("jpMaxGlobalCardsProbe", 100000) or 0)
        log_line("[build_price_cache] Fetching TCGdex global JP cards from /v2/ja/cards")
        try:
            global_cards = fetch_global_jp_cards(max_global_cards_probe)
            metrics["catalogueJpGlobalCardsFetched"] = len(global_cards)
            for card in global_cards:
                parsed = parse_tcgdex_card_id(str(card.get("id") or ""))
                if not parsed:
                    metrics["catalogueJpGlobalCardsSkippedUnparseableId"] += 1
                    continue
                parsed_set_id, local_id = parsed
                key = parsed_set_id.lower()
                canonical_set_id = canonical_set_id_by_lower.get(key)
                if canonical_set_id is None:
                    metrics["catalogueJpGlobalCardsSkippedUnknownSet"] += 1
                    continue
                set_name = set_name_by_id_lower.get(key, canonical_set_id)
                global_record = build_japanese_global_card_record(card, canonical_set_id, set_name, local_id)
                grouped_global_cards.setdefault(canonical_set_id, []).append(global_record)
                metrics["catalogueJpGlobalCardsGrouped"] += 1
        except (requests.RequestException, ValueError) as exc:
            log_line(f"  [WARN] Failed global JP card fallback fetch: {exc}")

    card_files: list[tuple[str, str, Path]] = []
    current_price_files: list[tuple[str, str, Path]] = []
    failed_set_ids: list[str] = []
    empty_set_ids: list[str] = []
    cards_dir = CATALOG_DIR / "pokemon" / "jp" / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    for idx, set_summary in enumerate(unique_sets, start=1):
        set_id = str(set_summary.get("id") or "")
        set_name = str(set_summary.get("name") or set_id)
        if not set_id:
            continue

        metrics["catalogueJpSetsAttempted"] += 1
        if idx == 1 or idx % 25 == 0 or idx == len(unique_sets):
            log_line(f"  Fetching JP cards progress: {idx}/{len(unique_sets)} (latest set {set_id})")

        try:
            set_response = requests.get(f"https://api.tcgdex.net/v2/ja/sets/{set_id}", timeout=30)
            mark_request_attempt(
                success=set_response.status_code < 400,
                rate_limited=(set_response.status_code == 429),
                provider=SOURCE_ID_TCGDEX,
            )
            set_response.raise_for_status()
            set_payload = set_response.json()
            if not isinstance(set_payload, dict):
                raise ValueError(f"TCGdex returned non-object payload for set {set_id}")

            serie = set_payload.get("serie") if isinstance(set_payload.get("serie"), dict) else {}
            serie_id = str(serie.get("id") or "") or None
            set_detail_records = [
                build_japanese_catalog_card_record(card, set_id, set_name, serie_id)
                for card in set_payload.get("cards", [])
                if isinstance(card, dict)
            ]

            global_records = grouped_global_cards.get(set_id, [])
            card_records, duplicates_removed = merge_japanese_set_cards(set_detail_records, global_records)
            metrics["catalogueJpCardsFromSetDetails"] += len(set_detail_records)
            metrics["catalogueJpCardsFromGlobalList"] += len(global_records)
            metrics["catalogueJpDuplicateCardsRemoved"] += duplicates_removed

            if not card_records:
                metrics["catalogueJpSetsSkippedEmptyCards"] += 1
                metrics["catalogueJpSkippedEmptySetIds"].append(set_id)
                empty_set_ids.append(set_id)
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                continue

            card_records.sort(key=catalogue_card_sort_key)

            card_file_payload = {
                "schemaVersion": SCHEMA_VERSION,
                "generatedAtUtc": ts,
                "game": "pokemon",
                "language": "jp",
                "setId": set_id,
                "setName": set_name,
                "source": SOURCE_ID_TCGDEX,
                "catalogueStatus": "built",
                "cardCount": len(card_records),
                "cards": card_records,
            }
            card_path = cards_dir / f"{set_id}.json"
            write_json(card_path, card_file_payload)
            card_files.append((set_id, set_name, card_path))
            metrics["catalogueJpSetsBuilt"] += 1
            metrics["catalogueJpCardsFetched"] += len(card_records)
            metrics["catalogueJpCardsMergedTotal"] += len(card_records)
            current_price_metrics["currentPriceJpSkippedNoPriceSets"] += 1
        except (requests.RequestException, ValueError) as exc:
            log_line(f"  [WARN] Failed to build JP catalogue cards for set {set_id}: {exc}")
            failed_set_ids.append(set_id)
            if not config.get("jpContinueOnSetError", True):
                metrics["catalogueJpStoppedReason"] = f"set_error:{set_id}"
                break

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    failed_set_ids.sort()
    empty_set_ids.sort()
    metrics["catalogueJpFailedSetIds"] = failed_set_ids
    metrics["catalogueJpSetsFailed"] = len(failed_set_ids)
    metrics["catalogueJpSkippedEmptySetIds"] = sorted(metrics["catalogueJpSkippedEmptySetIds"])
    metrics["catalogueJpEmptySetIds"] = empty_set_ids
    metrics["catalogueJpCoverageImprovedByGlobalFallback"] = metrics["catalogueJpCardsFromGlobalList"] > metrics[
        "catalogueJpDuplicateCardsRemoved"
    ]

    # Calculate status based on what was actually built
    if card_files:
        has_skip_or_failure = (
            bool(failed_set_ids)
            or metrics["catalogueJpSetsSkippedEmptyCards"] > 0
            or metrics["catalogueJpGlobalCardsSkippedUnparseableId"] > 0
            or metrics["catalogueJpGlobalCardsSkippedUnknownSet"] > 0
        )
        metrics["catalogueJpStatus"] = "partial_built" if has_skip_or_failure else "built"
    else:
        metrics["catalogueJpStatus"] = "not_built_yet"
    
    if metrics["catalogueJpStoppedReason"] is None:
        metrics["catalogueJpStoppedReason"] = "completed"

    # Add endpoint examples for diagnostics
    metrics["catalogueJpEndpointExamples"] = [
        "https://api.tcgdex.net/v2/ja/sets",
        "https://api.tcgdex.net/v2/ja/sets/{setId}",
        "https://api.tcgdex.net/v2/ja/cards",
    ]

    current_price_metrics["currentPriceJpStatus"] = "skipped_no_set_level_pricing"

    sets_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "game": "pokemon",
        "language": "jp",
        "catalogueStatus": metrics["catalogueJpStatus"],
        "cardsAvailable": len(card_files) > 0,
        "source": SOURCE_ID_TCGDEX,
        "setCount": len(unique_sets),
        "cardCount": metrics["catalogueJpCardsFetched"],
        "partialSetCount": metrics["catalogueJpSetsSkippedEmptyCards"],
        "failedSetCount": len(failed_set_ids),
        "failedSetIds": failed_set_ids,
        "sets": [build_japanese_catalog_set_record(item) for item in unique_sets],
        "notes": [
            "Japanese catalogue is built from official TCGdex set endpoints using the ja upstream language path.",
            "Card records store image URLs only; images are not mirrored into this cache.",
            "Some JP card metadata remains null when it is not available from set-level TCGdex responses.",
        ],
    }
    return sets_payload, card_files, metrics, current_price_files, current_price_metrics


def build_english_pokemon_catalogue(ts: str, config: dict) -> tuple[dict, list[tuple[str, str, Path]], dict]:
    metrics = {
        "catalogueEnStatus": "not_built_yet",
        "catalogueEnFetchStrategy": "set_by_set",
        "catalogueEnSetCount": 0,
        "catalogueEnSetsAttempted": 0,
        "catalogueEnSetsBuilt": 0,
        "catalogueEnSetsFailed": 0,
        "catalogueEnCardsFetched": 0,
        "catalogueEnFailedSetIds": [],
        "catalogueEnStoppedReason": None,
    }

    if not config.get("fullCatalogueEnabled", True) or not config.get("buildEnglishFromPokemonTcgApi", True):
        metrics["catalogueEnStoppedReason"] = "disabled_by_config"
        return build_catalog_sets_placeholder("pokemon", "en", ts), [], metrics

    page_size = int(config.get("pageSize", 250))
    max_pages_per_run = int(config.get("maxPagesPerRun", 1000))
    max_pages_per_set = int(config.get("maxPagesPerSet", 50))
    max_sets_per_run = int(config.get("maxSetsPerRun", 9999))
    sleep_seconds = float(config.get("catalogueRequestSleepSeconds", 0.15))

    print("[build_price_cache] Fetching PokemonTCG API sets for EN catalogue")
    sets, total_sets, _pages = fetch_pokemon_tcg_paginated(
        "sets",
        page_size=page_size,
        max_pages=max_pages_per_run,
        sleep_seconds=sleep_seconds,
    )
    sets.sort(key=lambda item: (str(item.get("releaseDate") or ""), str(item.get("id") or "")))
    if max_sets_per_run > 0:
        sets = sets[:max_sets_per_run]

    metrics["catalogueEnSetCount"] = len(sets)
    if total_sets is not None and len(sets) < total_sets:
        metrics["catalogueEnStoppedReason"] = "max_sets_per_run_reached"

    card_files: list[tuple[str, str, Path]] = []
    failed_set_ids: list[str] = []
    cards_dir = CATALOG_DIR / "pokemon" / "en" / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)

    for set_data in sets:
        set_id = str(set_data.get("id") or "")
        set_name = str(set_data.get("name") or "")
        if not set_id:
            continue

        metrics["catalogueEnSetsAttempted"] += 1
        print(f"  Fetching cards for set {set_id} ({set_name})")

        try:
            cards, _total_cards, _pages = fetch_pokemon_tcg_paginated(
                "cards",
                base_params={"q": f"set.id:{set_id}"},
                page_size=page_size,
                max_pages=max_pages_per_set,
                sleep_seconds=sleep_seconds,
            )
            card_records = [build_catalog_card_record(card, set_id, set_name) for card in cards]
            card_records.sort(key=catalogue_card_sort_key)

            card_payload = {
                "schemaVersion": SCHEMA_VERSION,
                "generatedAtUtc": ts,
                "game": "pokemon",
                "language": "en",
                "setId": set_id,
                "setName": set_name,
                "source": SOURCE_ID_POKEMON_TCG_API,
                "catalogueStatus": "built",
                "cardCount": len(card_records),
                "cards": card_records,
            }
            card_path = cards_dir / f"{set_id}.json"
            write_json(card_path, card_payload)
            card_files.append((set_id, set_name, card_path))
            metrics["catalogueEnSetsBuilt"] += 1
            metrics["catalogueEnCardsFetched"] += len(card_records)
        except ProviderRateLimitError as exc:
            print(f"  [WARN] Stopping EN catalogue build due to provider rate limit: {exc}")
            metrics["catalogueEnStoppedReason"] = "rate_limited"
            break
        except (requests.RequestException, ValueError) as exc:
            print(f"  [WARN] Failed to build catalogue cards for set {set_id}: {exc}")
            failed_set_ids.append(set_id)
            if not config.get("continueOnSetError", True):
                metrics["catalogueEnStoppedReason"] = f"set_error:{set_id}"
                break
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    failed_set_ids.sort()
    metrics["catalogueEnFailedSetIds"] = failed_set_ids
    metrics["catalogueEnSetsFailed"] = len(failed_set_ids)

    if failed_set_ids:
        status = "partial_built"
    else:
        status = "built"
    metrics["catalogueEnStatus"] = status
    if metrics["catalogueEnStoppedReason"] is None:
        metrics["catalogueEnStoppedReason"] = "completed"

    sets_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "game": "pokemon",
        "language": "en",
        "catalogueStatus": status,
        "cardsAvailable": len(card_files) > 0,
        "source": SOURCE_ID_POKEMON_TCG_API,
        "setCount": len(sets),
        "cardCount": metrics["catalogueEnCardsFetched"],
        "partialSetCount": len(failed_set_ids),
        "failedSetCount": len(failed_set_ids),
        "failedSetIds": failed_set_ids,
        "sets": [build_catalog_set_record(item) for item in sets],
        "notes": [
            "English catalogue is built from official PokemonTCG API endpoints.",
            "Card records store image URLs only; images are not mirrored into this cache.",
        ],
    }
    return sets_payload, card_files, metrics


def load_existing_catalogue_sets() -> dict:
    path = CATALOG_DIR / "pokemon" / "en" / "sets.json"
    if not path.exists():
        return build_catalog_sets_placeholder("pokemon", "en", now_utc())
    data = load_json(path)
    return data if isinstance(data, dict) else build_catalog_sets_placeholder("pokemon", "en", now_utc())


def load_existing_catalogue_card_files() -> list[tuple[str, str, Path]]:
    cards_dir = CATALOG_DIR / "pokemon" / "en" / "cards"
    if not cards_dir.exists():
        return []

    card_files: list[tuple[str, str, Path]] = []
    for path in sorted(cards_dir.glob("*.json")):
        try:
            payload = load_json(path)
        except OSError:
            continue
        if not isinstance(payload, dict):
            continue
        set_id = str(payload.get("setId") or path.stem)
        set_name = str(payload.get("setName") or set_id)
        card_files.append((set_id, set_name, path))
    return card_files


def load_existing_japanese_catalogue_card_files() -> list[tuple[str, str, Path]]:
    cards_dir = CATALOG_DIR / "pokemon" / "jp" / "cards"
    if not cards_dir.exists():
        return []

    card_files: list[tuple[str, str, Path]] = []
    for path in sorted(cards_dir.glob("*.json")):
        try:
            payload = load_json(path)
        except OSError:
            continue
        if not isinstance(payload, dict):
            continue
        set_id = str(payload.get("setId") or path.stem)
        set_name = str(payload.get("setName") or set_id)
        card_files.append((set_id, set_name, path))
    return card_files


def load_existing_japanese_catalogue(ts: str) -> dict:
    path = CATALOG_DIR / "pokemon" / "jp" / "sets.json"
    if not path.exists():
        return build_catalog_sets_placeholder("pokemon", "jp", ts)
    data = load_json(path)
    return data if isinstance(data, dict) else build_catalog_sets_placeholder("pokemon", "jp", ts)


def compact_current_price(pricing: dict) -> dict | None:
    market = to_float(pricing.get("market") if pricing.get("market") is not None else pricing.get("mid"))
    low = to_float(pricing.get("low"))
    high = to_float(pricing.get("high"))

    if market is None and low is None and high is None:
        return None

    return {
        "marketPrice": market,
        "lowPrice": low,
        "highPrice": high,
    }


def normalize_collector_number(value: object) -> str:
    raw = str(value or "").strip().upper().replace(" ", "")
    if not raw:
        return ""
    match = re.match(r"^([A-Z]*)(\d+)(?:/(\d+))?$", raw)
    if not match:
        return raw
    prefix, first, second = match.groups()
    first_norm = str(int(first)) if first.isdigit() else first
    if second:
        second_norm = str(int(second)) if second.isdigit() else second
        return f"{prefix}{first_norm}/{second_norm}"
    return f"{prefix}{first_norm}"


def load_catalogue_card_index_for_set(set_id: str, language: str = "en") -> dict[str, list[dict]]:
    cards_path = CATALOG_DIR / "pokemon" / language / "cards" / f"{set_id}.json"
    if not cards_path.exists():
        return {}
    payload = load_json(cards_path)
    cards = payload.get("cards") if isinstance(payload, dict) else []
    if not isinstance(cards, list):
        return {}
    index: dict[str, list[dict]] = {}
    for item in cards:
        if not isinstance(item, dict):
            continue
        collector = normalize_collector_number(item.get("collectorNumber"))
        if not collector:
            continue
        index.setdefault(collector, []).append(item)
    return index


def normalize_pokewallet_variant(value: object) -> str:
    raw = normalize_catalog_name(str(value or "normal")).strip("_") or "normal"
    aliases = {
        "normal": "normal",
        "non_holo": "normal",
        "holo": "holo",
        "holofoil": "holo",
        "holo_foil": "holo",
        "reverse": "reverse",
        "reverse_holo": "reverse",
        "reverse_holofoil": "reverse",
        "1st_edition_holofoil": "first_edition_holo",
        "first_edition_holofoil": "first_edition_holo",
        "1st_edition_normal": "first_edition_normal",
        "first_edition_normal": "first_edition_normal",
    }
    return aliases.get(raw, raw)


def summarize_pokewallet_raw_item(record: object) -> dict[str, object]:
    if not isinstance(record, dict):
        return {"shape": type(record).__name__}
    tcgplayer = record.get("tcgplayer") if isinstance(record.get("tcgplayer"), dict) else {}
    cardmarket = record.get("cardmarket") if isinstance(record.get("cardmarket"), dict) else {}
    card_info = record.get("card_info") if isinstance(record.get("card_info"), dict) else {}
    return {
        "cardName": card_info.get("name") or record.get("name") or record.get("card_name"),
        "cardNumber": card_info.get("number") or record.get("card_number") or record.get("number"),
        "variant": card_info.get("sub_type_name") or record.get("variant") or record.get("sub_type_name"),
        "providerId": record.get("id") or record.get("provider_id") or record.get("product_id"),
        "keys": sorted(list(record.keys()))[:24],
        "tcgplayerKeys": sorted(list(tcgplayer.keys()))[:24],
        "cardmarketKeys": sorted(list(cardmarket.keys()))[:24],
    }


def extract_pokewallet_raw_records(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("results")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def derive_pokewallet_collector_number(record: dict) -> str:
    card_info = record.get("card_info") if isinstance(record.get("card_info"), dict) else {}
    direct = str(card_info.get("number") or record.get("card_number") or record.get("number") or "").strip()
    name_text = str(card_info.get("name") or record.get("name") or "")

    bw_token = re.search(r"\b([A-Z]{1,4}\d{1,4})\b", name_text.upper())
    if bw_token and "/" in direct and re.match(r"^\d+/\d+$", direct):
        return bw_token.group(1)
    if direct:
        return direct
    if bw_token:
        return bw_token.group(1)
    return ""


def extract_pokewallet_tcgplayer_pricings(record: dict) -> list[tuple[dict, str]]:
    tcgplayer = record.get("tcgplayer") if isinstance(record.get("tcgplayer"), dict) else {}
    if not tcgplayer:
        return []

    variants: list[tuple[dict, str]] = []
    prices = tcgplayer.get("prices")
    if isinstance(prices, dict):
        for item in prices.values():
            if not isinstance(item, dict):
                continue
            compacted = compact_current_price(item)
            if compacted is None:
                continue
            variant = normalize_pokewallet_variant(
                item.get("sub_type_name") or item.get("variant") or record.get("variant") or "normal"
            )
            variants.append((item, variant))
        return variants

    flattened = {
        "market": tcgplayer.get("market_price") if tcgplayer.get("market_price") is not None else tcgplayer.get("mid_price"),
        "mid": tcgplayer.get("mid_price"),
        "low": tcgplayer.get("low_price") if tcgplayer.get("low_price") is not None else tcgplayer.get("direct_low_price"),
        "high": tcgplayer.get("high_price"),
    }
    if compact_current_price(flattened) is None:
        return []
    variants.append((flattened, normalize_pokewallet_variant(record.get("variant") or "normal")))
    return variants


def build_current_price_record_from_fields(
    *,
    set_id: str,
    set_name: str,
    collector_number: str,
    normalized_name: str,
    variant: str,
    pricing: dict,
    ts: str,
    source: str = CURRENT_PRICE_SOURCE,
    currency: str = CURRENT_PRICE_CURRENCY,
    market: str = "us",
    country: str = "US",
    confidence: str = "medium",
    diagnostics_notes: list[str] | None = None,
    provider_diagnostics: dict[str, object] | None = None,
) -> dict | None:
    compacted = compact_current_price(pricing)
    if compacted is None:
        return None

    canonical_card_id = f"pokemon|en|{set_id}|{collector_number}|{normalized_name}"
    condition_value = "near_mint"
    price_identity_id = (
        f"{canonical_card_id}|{variant}|{condition_value}|{market}|{currency.lower()}"
    )

    diagnostics_payload: dict[str, object] = {
        "sourceRecordStatus": "priced",
        "notes": list(diagnostics_notes or []),
    }
    if provider_diagnostics:
        diagnostics_payload["providerRecord"] = provider_diagnostics

    return {
        "canonicalId": f"{canonical_card_id}|{variant}|{condition_value}",
        "canonicalCardId": canonical_card_id,
        "priceIdentityId": price_identity_id,
        "setId": set_id,
        "setName": set_name,
        "collectorNumber": collector_number,
        "normalizedName": normalized_name,
        "variant": variant,
        "condition": condition_value,
        "currency": currency,
        "market": market,
        "country": country,
        "sourceCurrency": currency,
        "targetCurrency": currency,
        "conversionPolicy": "none",
        "status": "priced",
        "confidence": confidence,
        "diagnostics": diagnostics_payload,
        "marketPrice": compacted["marketPrice"],
        "lowPrice": compacted["lowPrice"],
        "highPrice": compacted["highPrice"],
        "source": source,
        "fetchedAtUtc": ts,
    }


def build_current_price_record(card: dict, variant: str, pricing: dict, ts: str) -> dict | None:
    set_data = card.get("set") if isinstance(card.get("set"), dict) else {}
    return build_current_price_record_from_fields(
        set_id=str(set_data.get("id") or ""),
        set_name=str(set_data.get("name") or ""),
        collector_number=str(card.get("number") or ""),
        normalized_name=normalize_catalog_name(card.get("name", "")),
        variant=variant,
        pricing=pricing,
        ts=ts,
    )


def extract_current_price_records(card: dict, ts: str) -> list[dict]:
    pricing_root = card.get("tcgplayer")
    if not isinstance(pricing_root, dict):
        return []
    prices = pricing_root.get("prices")
    if not isinstance(prices, dict):
        return []

    records = []
    for api_key, variant in CURRENT_PRICE_VARIANTS:
        pricing = prices.get(api_key)
        if not isinstance(pricing, dict):
            continue
        record = build_current_price_record(card, variant, pricing, ts)
        if record is not None:
            records.append(record)
    return records


def iter_pokewallet_price_variants(record: dict) -> list[tuple[str, str, str, dict, str]]:
    variants: list[tuple[str, str, str, dict, str]] = []
    for item, variant in extract_pokewallet_tcgplayer_pricings(record):
        variants.append((SOURCE_ID_POKEWALLET, "USD", "us", item, variant))

    return variants


def pokewallet_provider_record_diagnostics(record: dict) -> dict[str, object]:
    card_info = record.get("card_info") if isinstance(record.get("card_info"), dict) else {}
    tcgplayer = record.get("tcgplayer") if isinstance(record.get("tcgplayer"), dict) else {}
    identifiers = {
        "providerId": record.get("id") or record.get("provider_id") or record.get("product_id"),
        "tcgplayerProductId": (
            tcgplayer.get("product_id")
            or tcgplayer.get("productId")
            or record.get("tcgplayer_product_id")
            or record.get("tcgplayerProductId")
        ),
        "cardName": card_info.get("name") or record.get("name") or record.get("card_name"),
        "cardNumber": card_info.get("number") or record.get("card_number") or record.get("number"),
        "variant": card_info.get("sub_type_name") or record.get("variant") or record.get("sub_type_name"),
    }
    return {key: value for key, value in identifiers.items() if value not in (None, "")}


def confidence_rank(value: object) -> int:
    return {"high": 3, "medium": 2, "low": 1, "unknown": 0}.get(str(value or "unknown"), 0)


def utc_sort_value(value: object) -> str:
    parsed = parse_utc_timestamp(str(value or ""))
    if parsed is None:
        return ""
    return parsed.isoformat()


def is_numeric_price(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def pokewallet_record_quality_key(record: dict, original_index: int) -> tuple[object, ...]:
    notes = []
    diagnostics = record.get("diagnostics")
    if isinstance(diagnostics, dict) and isinstance(diagnostics.get("notes"), list):
        notes = [str(item) for item in diagnostics["notes"]]
    return (
        1 if is_numeric_price(record.get("marketPrice")) else 0,
        confidence_rank(record.get("confidence")),
        1
        if (
            record.get("source") == SOURCE_ID_POKEWALLET
            and record.get("currency") == "USD"
            and record.get("market") == "us"
            and "pokewallet_tcgplayer_usd" in notes
        )
        else 0,
        utc_sort_value(record.get("fetchedAtUtc")),
        -original_index,
    )


def dedupe_pokewallet_current_price_records(records: list[dict]) -> tuple[list[dict], dict[str, object]]:
    counts = Counter(str(record.get("canonicalId") or "") for record in records if record.get("canonicalId"))
    duplicate_counts = {cid: count for cid, count in sorted(counts.items()) if count > 1}
    best_by_canonical_id: dict[str, tuple[tuple[object, ...], dict, int]] = {}
    deduped_provider_rows: list[dict[str, object]] = []

    for index, record in enumerate(records):
        canonical_id = str(record.get("canonicalId") or "")
        if not canonical_id:
            continue
        quality_key = pokewallet_record_quality_key(record, index)
        existing = best_by_canonical_id.get(canonical_id)
        if existing is None or quality_key > existing[0]:
            if existing is not None:
                deduped_provider_rows.append(
                    {
                        "canonicalId": canonical_id,
                        "keptIndex": index,
                        "dedupedIndex": existing[2],
                        "dedupedProviderRecord": (
                            existing[1].get("diagnostics", {}).get("providerRecord")
                            if isinstance(existing[1].get("diagnostics"), dict)
                            else None
                        ),
                    }
                )
            best_by_canonical_id[canonical_id] = (quality_key, record, index)
        else:
            deduped_provider_rows.append(
                {
                    "canonicalId": canonical_id,
                    "keptIndex": existing[2],
                    "dedupedIndex": index,
                    "dedupedProviderRecord": (
                        record.get("diagnostics", {}).get("providerRecord")
                        if isinstance(record.get("diagnostics"), dict)
                        else None
                    ),
                }
            )

    deduped = [item[1] for item in sorted(best_by_canonical_id.values(), key=lambda item: item[2])]
    diagnostics = {
        "usableRecordsBeforeDedupe": len(records),
        "usableRecordsAfterDedupe": len(deduped),
        "dedupedRecords": max(0, len(records) - len(deduped)),
        "duplicateCanonicalIdCounts": duplicate_counts,
        "sampleDedupedProviderRows": deduped_provider_rows[:25],
    }
    return deduped, diagnostics


def extract_pokewallet_current_price_records(
    record: dict,
    set_data: dict,
    ts: str,
    *,
    catalog_index: dict[str, list[dict]] | None = None,
) -> tuple[list[dict], list[str]]:
    catalog_index = catalog_index or {}
    reasons: list[str] = []
    collector_number_raw = derive_pokewallet_collector_number(record)
    collector_number = str(collector_number_raw or "").strip()
    card_info = record.get("card_info") if isinstance(record.get("card_info"), dict) else {}
    raw_name = str(card_info.get("name") or record.get("name") or "").strip()
    normalized_name = normalize_catalog_name(raw_name)

    if not collector_number:
        reasons.append("missing_card_number")
    if not normalized_name:
        reasons.append("missing_name")

    if reasons:
        return [], reasons

    matched_catalog_card: dict | None = None
    normalized_collector = normalize_collector_number(collector_number)
    candidates = catalog_index.get(normalized_collector, []) if normalized_collector else []
    if len(candidates) > 1:
        return [], ["ambiguous_catalogue_match"]
    if len(candidates) == 1:
        matched_catalog_card = candidates[0]
    else:
        return [], ["no_catalogue_match"]

    effective_collector = str(matched_catalog_card.get("collectorNumber") or collector_number)
    effective_name = str(matched_catalog_card.get("normalizedName") or normalized_name)

    variants = iter_pokewallet_price_variants(record)
    if not variants:
        cardmarket = record.get("cardmarket") if isinstance(record.get("cardmarket"), dict) else {}
        if cardmarket:
            return [], ["unsupported_currency"]
        return [], ["missing_tcgplayer_price"]

    set_id = str(set_data.get("id") or "")
    set_name = str(set_data.get("name") or "")

    records: list[dict] = []
    for source, currency, market, pricing, variant in variants:
        current = build_current_price_record_from_fields(
            set_id=set_id,
            set_name=set_name,
            collector_number=effective_collector,
            normalized_name=effective_name,
            variant=variant,
            pricing=pricing,
            ts=ts,
            source=source,
            currency=currency,
            market=market,
            country=market.upper(),
            confidence="medium",
            diagnostics_notes=["pokewallet_tcgplayer_usd"],
            provider_diagnostics=pokewallet_provider_record_diagnostics(record),
        )
        if current is not None:
            records.append(current)
    if not records:
        return [], ["missing_tcgplayer_price"]
    return records, []


def resolve_price_provider_priority(config: dict) -> list[str]:
    raw_value = str(
        os.getenv(POKEWALLET_PRICE_PROVIDER_PRIORITY_ENV, "")
        or config.get("priceProviderPriority")
        or config.get("currentPriceProviderPriority")
        or ""
    ).strip()
    if not raw_value:
        return [SOURCE_ID_POKEMON_TCG_API]
    providers = [item.strip().lower() for item in raw_value.split(",") if item.strip()]
    return providers or [SOURCE_ID_POKEMON_TCG_API]


def should_use_pokewallet_prices(config: dict) -> bool:
    flag = parse_bool_env(POKEWALLET_USE_PRICES_ENV, default=False)
    if bool(config.get("usePokewalletPrices", False)):
        flag = True
    priority = resolve_price_provider_priority(config)
    return flag or (priority and priority[0] == SOURCE_ID_POKEWALLET)


def build_english_current_prices_by_set(
    ts: str,
    config: dict,
    catalog_sets: dict,
    output_dir: Path,
    mode: str,
    refresh_state: dict,
    fail_after_set_count: int = 0,
    set_id_override: str | None = None,
) -> tuple[list[tuple[str, str, Path]], dict, dict]:
    provider_priority = resolve_price_provider_priority(config)
    use_pokewallet_prices = should_use_pokewallet_prices(config)
    require_pokewallet_prices = parse_bool_env(POKEWALLET_REQUIRE_PRICES_ENV, default=False)
    pokewallet_api_key = resolve_pokewallet_api_key() if use_pokewallet_prices else ""
    pokemon_tcg_api_key_present = bool(os.getenv("POKEMON_TCG_API_KEY", "").strip())
    pokewallet_api_key_present = bool(pokewallet_api_key)

    metrics = {
        "currentPriceEnStatus": "not_built_yet",
        "currentPriceEnSetsAttempted": 0,
        "currentPriceEnSetsWritten": 0,
        "currentPriceEnSetsUpdated": 0,
        "currentPriceEnSetsKeptExisting": 0,
        "currentPriceEnSetsFailedTransient": 0,
        "currentPriceEnPriceRecordsWritten": 0,
        "currentPriceEnSkippedNoPriceSets": 0,
        "currentPriceEnSource": CURRENT_PRICE_SOURCE,
        "currentPriceEnCurrency": CURRENT_PRICE_CURRENCY,
        "currentPriceEnRateLimited": False,
        "currentPriceEnRequestCap": CURRENT_PRICE_REQUEST_CAP,
        "currentPriceEnProviderRequestCaps": dict(CURRENT_PRICE_PROVIDER_REQUEST_CAPS),
        "currentPriceEnRequestsUsed": 0,
        "currentPriceEnProviderRequestCounts": {},
        "currentPriceEnStopReason": None,
        "currentPriceEnProviderPriority": provider_priority,
        "currentPriceEnProviderUsed": [],
        "currentPriceEnFallbackReasons": [],
        "currentPriceEnTransientFailureReasons": [],
        "currentPriceEnProviderFailureReasons": [],
        "currentPriceEnKeptExistingSetIds": [],
        "currentPriceEnFailedTransientSetIds": [],
        "pokewalletEnabled": use_pokewallet_prices,
        "pokewalletApiKeyPresent": pokewallet_api_key_present,
        "providerPriority": provider_priority,
        "pokewalletSetsAttempted": 0,
        "pokewalletSetsSucceeded": 0,
        "pokewalletSetsSkippedNoMatch": 0,
        "pokewalletSetsFailed": 0,
        "pokemonTcgApiFallbackSets": 0,
        "providerFallbackReasons": [],
        "providerSourceCounts": {},
        "setsAttempted": 0,
        "setsUpdated": 0,
        "setsKeptExisting": 0,
        "setsFailedTransient": 0,
        "transientFailureReasons": [],
        "providerFailureReasons": [],
        "pokewalletNoUsableRecords": 0,
        "fallbackTimeout": 0,
        "currentPriceEnRequirePokewallet": require_pokewallet_prices,
        "currentPriceEnTargetSetId": None,
    }

    if not config.get("buildCurrentPricesFromPokemonTcgApi", True):
        metrics["currentPriceEnStatus"] = "disabled_by_config"
        return [], metrics, refresh_state

    if not isinstance(catalog_sets, dict) or catalog_sets.get("catalogueStatus") not in {"built", "partial_built"}:
        metrics["currentPriceEnStatus"] = "skipped_no_built_catalogue"
        return [], metrics, refresh_state

    sets_all = [item for item in catalog_sets.get("sets", []) if isinstance(item, dict) and item.get("id")]
    sets_all.sort(key=lambda item: str(item.get("id") or ""))
    if not sets_all:
        metrics["currentPriceEnStatus"] = "skipped_no_sets"
        return [], metrics, refresh_state

    requested_set_id = str(set_id_override or os.getenv(CURRENT_PRICE_SET_ID_ENV, "")).strip()
    if requested_set_id:
        requested_lookup = requested_set_id.lower()
        requested_matches = [
            item for item in sets_all if str(item.get("id") or "").strip().lower() == requested_lookup
        ]
        if not requested_matches:
            raise RuntimeError(f"Requested set id not found in EN catalogue: {requested_set_id}")
        sets_all = requested_matches
        metrics["currentPriceEnTargetSetId"] = str(requested_matches[0].get("id") or requested_set_id)

    strategy = str(
        config.get("scheduledCurrentPriceRefreshStrategy")
        or config.get("localUpdaterRefreshStrategy")
        or "rotating_set_batch"
    ).strip().lower()
    batch_enabled = bool(config.get("scheduledCurrentPriceBatchEnabled", True))
    request_cap = int(metrics["currentPriceEnRequestCap"] or 0)

    selected_sets = list(sets_all)
    cursor_before = int(refresh_state.get("enCurrentPriceCursor", 0) or 0)
    cursor_after = cursor_before
    selected_set_ids: list[str] = [str(item.get("id") or "") for item in selected_sets]
    processed_set_ids: list[str] = []
    rate_limited = False

    if batch_enabled and strategy == "rotating_set_batch" and mode in {"scheduled", "current_prices"}:
        batch_size = resolve_batch_size(config)
        total_sets = len(sets_all)
        start = cursor_before % total_sets
        selected_sets = [sets_all[(start + idx) % total_sets] for idx in range(min(batch_size, total_sets))]
        selected_set_ids = [str(item.get("id") or "") for item in selected_sets]
        cursor_after = (start + len(selected_sets)) % total_sets
        metrics["currentPriceEnBatchEnabled"] = True
        metrics["currentPriceEnBatchStrategy"] = "rotating_set_batch"
        metrics["currentPriceEnBatchSize"] = batch_size
        metrics["currentPriceEnBatchCursorBefore"] = start
        metrics["currentPriceEnBatchCursorAfter"] = cursor_after
    else:
        metrics["currentPriceEnBatchEnabled"] = False
        metrics["currentPriceEnBatchStrategy"] = "all_sets"
        metrics["currentPriceEnBatchSize"] = len(selected_sets)
        metrics["currentPriceEnBatchCursorBefore"] = cursor_before
        metrics["currentPriceEnBatchCursorAfter"] = cursor_before

    interval_minutes = max(1, safe_int(config.get("localUpdaterIntervalMinutes"), 60))
    effective_batch_size = max(1, safe_int(metrics.get("currentPriceEnBatchSize"), len(selected_sets)))
    full_rotation_hours = estimate_full_rotation_hours(len(sets_all), effective_batch_size, interval_minutes)
    metrics["currentPriceEnExpectedUpdateIntervalMinutes"] = interval_minutes
    metrics["currentPriceEnFullRotationEstimatedHours"] = full_rotation_hours

    prepare_empty_dir(output_dir)

    existing_current_files = load_existing_current_price_files("en")
    existing_current_file_by_set_id = {
        str(existing_set_id): (str(existing_set_name), existing_path)
        for existing_set_id, existing_set_name, existing_path in existing_current_files
    }
    selected_set_id_lookup = {str(item.get("id") or "") for item in selected_sets}
    for existing_set_id, _existing_set_name, existing_path in existing_current_files:
        if existing_set_id in selected_set_id_lookup:
            continue
        destination = output_dir / existing_path.name
        existing_payload = load_json(existing_path)
        if not isinstance(existing_payload, dict):
            continue
        enriched_existing_payload = enrich_en_current_set_payload(
            existing_payload,
            now_utc=ts,
            expected_update_interval_minutes=interval_minutes,
            full_rotation_hours=full_rotation_hours,
        )
        write_json(destination, enriched_existing_payload)

    page_size = int(config.get("pageSize", 250))
    max_pages_per_set = int(config.get("maxPagesPerSet", 50))
    sleep_seconds = float(config.get("catalogueRequestSleepSeconds", 0.15))
    transient_retry_count = max(
        0,
        min(
            2,
            parse_int_env(
                CURRENT_PRICE_TRANSIENT_RETRY_COUNT_ENV,
                safe_int(config.get("currentPriceTransientRetryCount"), 1),
            ),
        ),
    )
    transient_retry_sleep_seconds = max(0.0, float(config.get("currentPriceTransientRetrySleepSeconds", 0.05)))
    written_files: list[tuple[str, str, Path]] = []
    failed_set_ids: list[str] = []
    transient_failed_set_ids: list[str] = []
    request_cap_reached = False
    pokewallet_set_map = load_pokewallet_set_code_map() if use_pokewallet_prices and pokewallet_api_key else {}

    if mode == "current_prices":
        request_cap_display = request_cap if request_cap > 0 else "none"
        print(
            "[current_prices] provider config: "
            f"usePokeWalletPrices={use_pokewallet_prices}, "
            f"providerPriority={provider_priority}, "
            f"pokewalletApiKeyPresent={pokewallet_api_key_present}, "
            f"pokemonTcgApiKeyPresent={pokemon_tcg_api_key_present}, "
            f"requestCap={request_cap_display}"
        )

    if require_pokewallet_prices and not use_pokewallet_prices:
        raise RuntimeError(
            f"{POKEWALLET_REQUIRE_PRICES_ENV}=true requires PokeWallet provider to be enabled"
        )
    if require_pokewallet_prices and not pokewallet_api_key_present:
        raise RuntimeError(
            f"{POKEWALLET_REQUIRE_PRICES_ENV}=true requires a configured PokeWallet API key"
        )

    def keep_existing_after_transient_failure(set_id: str, set_name: str, reason: str) -> None:
        transient_failed_set_ids.append(set_id)
        if set_id not in metrics["currentPriceEnFailedTransientSetIds"]:
            metrics["currentPriceEnFailedTransientSetIds"].append(set_id)
        metrics["currentPriceEnSetsFailedTransient"] += 1
        metrics["setsFailedTransient"] += 1
        reason_entry = {"setId": set_id, "reason": reason}
        metrics["currentPriceEnTransientFailureReasons"].append(reason_entry)
        metrics["transientFailureReasons"].append(reason_entry)
        metrics["currentPriceEnProviderFailureReasons"].append(reason_entry)
        metrics["providerFailureReasons"].append(reason_entry)
        kept_path = keep_existing_current_price_file(
            set_id=set_id,
            output_dir=output_dir,
            existing_file_by_set_id=existing_current_file_by_set_id,
        )
        if kept_path is not None:
            metrics["currentPriceEnSetsKeptExisting"] += 1
            metrics["setsKeptExisting"] += 1
            metrics["currentPriceEnKeptExistingSetIds"].append(set_id)
            written_files.append((set_id, set_name, kept_path))
        else:
            metrics["currentPriceEnSkippedNoPriceSets"] += 1
        processed_set_ids.append(set_id)
        print(f"  Failed transiently for set {set_id}, keeping existing file and continuing")

    def keep_existing_after_budget_stop(set_id: str, set_name: str) -> None:
        kept_path = keep_existing_current_price_file(
            set_id=set_id,
            output_dir=output_dir,
            existing_file_by_set_id=existing_current_file_by_set_id,
        )
        if kept_path is not None:
            metrics["currentPriceEnSetsKeptExisting"] += 1
            metrics["setsKeptExisting"] += 1
            metrics["currentPriceEnKeptExistingSetIds"].append(set_id)
            written_files.append((set_id, set_name, kept_path))
            if set_id not in processed_set_ids:
                processed_set_ids.append(set_id)

    def provider_budget_remaining(provider: str) -> int | None:
        return remaining_current_price_requests(provider)

    for set_data in selected_sets:
        set_id = str(set_data.get("id"))
        set_name = str(set_data.get("name") or set_id)
        catalog_index = load_catalogue_card_index_for_set(set_id, "en")
        debug_pokewallet_response = mode == "current_prices" and (
            require_pokewallet_prices or bool(metrics.get("currentPriceEnTargetSetId"))
        )
        metrics["currentPriceEnSetsAttempted"] += 1
        metrics["setsAttempted"] += 1
        print(f"  Fetching current prices for set {set_id} ({set_name})")

        prices: list[dict] = []
        current_source = CURRENT_PRICE_SOURCE
        fallback_reason = ""
        attempted_pokewallet_for_set = False
        used_pokemon_fallback = False
        provider_diagnostics: dict[str, object] | None = None
        providers_to_try = [item for item in provider_priority if item in {SOURCE_ID_POKEWALLET, SOURCE_ID_POKEMON_TCG_API}]
        if use_pokewallet_prices and SOURCE_ID_POKEWALLET not in providers_to_try:
            providers_to_try = [SOURCE_ID_POKEWALLET] + providers_to_try
        if not providers_to_try:
            providers_to_try = [SOURCE_ID_POKEMON_TCG_API]

        for provider in providers_to_try:
            if prices:
                break

            if provider == SOURCE_ID_POKEWALLET:
                if not use_pokewallet_prices:
                    continue
                if provider_budget_remaining(SOURCE_ID_POKEWALLET) is not None and provider_budget_remaining(SOURCE_ID_POKEWALLET) <= 0:
                    print(f"  [WARN] Stopping EN current prices because PokeWallet request cap is exhausted before set {set_id}")
                    keep_existing_after_budget_stop(set_id, set_name)
                    metrics["currentPriceEnStopReason"] = "request_cap_reached:pokewallet"
                    request_cap_reached = True
                    break
                if not pokewallet_api_key:
                    fallback_reason = "pokewallet_api_key_missing"
                    print(f"  PokeWallet failed for set {set_id}: reason=api_key_missing")
                    metrics["pokewalletSetsFailed"] += 1
                    metrics["providerFallbackReasons"].append({"setId": set_id, "reason": fallback_reason})
                    if require_pokewallet_prices:
                        raise RuntimeError(f"PokeWallet required for set {set_id} but API key is missing")
                    continue

                match = resolve_pokewallet_set_match(set_data, pokewallet_set_map)
                set_code = str(match.get("matchedCode") or "").strip() or None
                if not set_code:
                    fallback_reason = f"pokewallet_set_code_unresolved:{match.get('reason') or 'unknown'}"
                    print(f"  PokeWallet skipped for set {set_id}: no provider set-code match")
                    for diag in build_pokewallet_match_diagnostics(
                        set_id,
                        set_name,
                        str(match.get("reason") or "no_reason"),
                        [item for item in match.get("candidates", []) if isinstance(item, dict)],
                    ):
                        print(f"    {diag}")
                    metrics["pokewalletSetsSkippedNoMatch"] += 1
                    metrics["providerFallbackReasons"].append({"setId": set_id, "reason": fallback_reason})
                    if require_pokewallet_prices:
                        raise RuntimeError(
                            "PokeWallet required for set "
                            f"{set_id} but no provider set-code match was found "
                            f"(reason={match.get('reason') or 'unknown'})"
                        )
                    continue

                attempted_pokewallet_for_set = True
                metrics["pokewalletSetsAttempted"] += 1
                print(f"  Trying PokeWallet for set {set_id} using providerSetCode {set_code}")
                try:
                    endpoint_used = f"prices/{quote(set_code, safe='')}"
                    payload, status_code = pokewallet_get_detailed(endpoint_used, pokewallet_api_key)
                    provider_records = extract_pokewallet_raw_records(payload)

                    matched_set_id = str(match.get("matchedSetId") or "").strip()
                    disambiguation_detected = bool(
                        isinstance(payload, dict) and (
                            payload.get("disambiguation") is not None or payload.get("matches") is not None
                        )
                    )
                    if not provider_records and disambiguation_detected and matched_set_id:
                        retry_endpoint = f"prices/{quote(matched_set_id, safe='')}"
                        payload, status_code = pokewallet_get_detailed(retry_endpoint, pokewallet_api_key)
                        endpoint_used = retry_endpoint
                        provider_records = extract_pokewallet_raw_records(payload)

                    if debug_pokewallet_response:
                        top_keys = sorted(payload.keys()) if isinstance(payload, dict) else [type(payload).__name__]
                        print(
                            "  PokeWallet response diagnostics: "
                            f"status={status_code}, topLevelKeys={top_keys}, "
                            f"shape={'dict' if isinstance(payload, dict) else type(payload).__name__}, "
                            f"endpoint={endpoint_used}"
                        )
                        print(f"  PokeWallet raw item count before filtering: {len(provider_records)}")
                        for idx, sample in enumerate(provider_records[:3]):
                            print(f"  PokeWallet raw item {idx + 1}: {summarize_pokewallet_raw_item(sample)}")

                    rejection_counts: dict[str, int] = {}
                    pokewallet_prices: list[dict] = []
                    for record in provider_records:
                        extracted_records, reject_reasons = extract_pokewallet_current_price_records(
                            record,
                            set_data,
                            ts,
                            catalog_index=catalog_index,
                        )
                        pokewallet_prices.extend(extracted_records)
                        for reason in reject_reasons:
                            rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1

                    deduped_prices, dedupe_diagnostics = dedupe_pokewallet_current_price_records(pokewallet_prices)
                    prices = deduped_prices
                    provider_diagnostics = {
                        "pokewallet": {
                            "rawItems": len(provider_records),
                            "usableRecordsBeforeDedupe": dedupe_diagnostics["usableRecordsBeforeDedupe"],
                            "usableRecordsAfterDedupe": dedupe_diagnostics["usableRecordsAfterDedupe"],
                            "dedupedRecords": dedupe_diagnostics["dedupedRecords"],
                            "duplicateCanonicalIdCounts": dedupe_diagnostics["duplicateCanonicalIdCounts"],
                            "rejectionReasonCounts": rejection_counts,
                            "sampleDedupedProviderRows": dedupe_diagnostics["sampleDedupedProviderRows"],
                        }
                    }

                    if debug_pokewallet_response:
                        usable_records_before_dedupe = len(pokewallet_prices)
                        usable_records_after_dedupe = len(prices)
                        rejected_records = max(0, len(provider_records) - usable_records_before_dedupe)
                        print(
                            "  PokeWallet parse summary: "
                            f"rawItems={len(provider_records)}, "
                            f"usableRecordsBeforeDedupe={usable_records_before_dedupe}, "
                            f"usableRecordsAfterDedupe={usable_records_after_dedupe}, "
                            f"dedupedRecords={dedupe_diagnostics['dedupedRecords']}, "
                            f"rejectedRecords={rejected_records}, rejectionReasonCounts={rejection_counts}"
                        )

                    if prices:
                        current_source = SOURCE_ID_POKEWALLET
                        metrics["pokewalletSetsSucceeded"] += 1
                        print(f"  PokeWallet success for set {set_id}: records={len(prices)}")
                    else:
                        fallback_reason = "pokewallet_no_price_records"
                        metrics["pokewalletNoUsableRecords"] += 1
                        metrics["pokewalletSetsFailed"] += 1
                        if rejection_counts:
                            metrics["providerFallbackReasons"].append(
                                {
                                    "setId": set_id,
                                    "reason": fallback_reason,
                                    "rejectionReasonCounts": rejection_counts,
                                }
                            )
                        else:
                            metrics["providerFallbackReasons"].append({"setId": set_id, "reason": fallback_reason})
                        print(f"  PokeWallet failed for set {set_id}: reason=no_usable_records")
                        if require_pokewallet_prices:
                            raise RuntimeError(
                                f"PokeWallet required for set {set_id} but returned no usable price records"
                            )
                except RequestCapReachedError as exc:
                    print(f"  [WARN] Stopping EN current prices due to PokeWallet request cap while building set {set_id}")
                    keep_existing_after_budget_stop(set_id, set_name)
                    metrics["currentPriceEnStopReason"] = f"request_cap_reached:{exc.provider or SOURCE_ID_POKEWALLET}"
                    request_cap_reached = True
                    break
                except ProviderRateLimitError as exc:
                    print(f"  PokeWallet failed for set {set_id}: status={exc.status_code}, reason={exc.detail or 'rate_limited'}")
                    metrics["pokewalletSetsFailed"] += 1
                    metrics["providerFallbackReasons"].append(
                        {"setId": set_id, "reason": f"pokewallet_rate_limited:{exc.status_code or 'unknown'}"}
                    )
                    if require_pokewallet_prices:
                        raise RuntimeError(f"PokeWallet required for set {set_id} but provider was rate limited")
                    metrics["currentPriceEnStatus"] = "rate_limited"
                    metrics["currentPriceEnRateLimited"] = True
                    metrics["currentPriceEnStopReason"] = f"rate_limited:{set_id}"
                    rate_limited = True
                    break
                except requests.RequestException as exc:
                    fallback_reason = f"pokewallet_unavailable:{exc.__class__.__name__}"
                    metrics["pokewalletSetsFailed"] += 1
                    metrics["providerFallbackReasons"].append({"setId": set_id, "reason": fallback_reason})
                    print(
                        f"  PokeWallet failed for set {set_id}: "
                        f"error={exc.__class__.__name__}, reason={exc}"
                    )
                    if require_pokewallet_prices:
                        raise RuntimeError(
                            f"PokeWallet required for set {set_id} but request failed: {exc.__class__.__name__}: {exc}"
                        )
                continue

            if provider == SOURCE_ID_POKEMON_TCG_API:
                if provider_budget_remaining(SOURCE_ID_POKEMON_TCG_API) is not None and provider_budget_remaining(SOURCE_ID_POKEMON_TCG_API) <= 0:
                    if not use_pokewallet_prices:
                        print(f"  [WARN] Stopping EN current prices because pokemon_tcg_api request cap is exhausted before set {set_id}")
                        keep_existing_after_budget_stop(set_id, set_name)
                        metrics["currentPriceEnStopReason"] = "request_cap_reached"
                        request_cap_reached = True
                        break
                    fallback_reason = "pokemon_tcg_api_request_cap_reached"
                    metrics["providerFallbackReasons"].append({"setId": set_id, "reason": fallback_reason})
                    print(f"  Skipping pokemon_tcg_api fallback for set {set_id}: request cap exhausted")
                    keep_existing_after_budget_stop(set_id, set_name)
                    break
                if use_pokewallet_prices and not prices and not require_pokewallet_prices:
                    print(f"  Falling back to pokemon_tcg_api for set {set_id}")
                    used_pokemon_fallback = True
                try:
                    cards, _total_cards, _pages = retry_transient_request(
                        lambda: fetch_pokemon_tcg_paginated(
                            "cards",
                            base_params={"q": f"set.id:{set_id}"},
                            page_size=page_size,
                            max_pages=max_pages_per_set,
                            sleep_seconds=sleep_seconds,
                        ),
                        provider=SOURCE_ID_POKEMON_TCG_API,
                        retries=transient_retry_count,
                        sleep_seconds=transient_retry_sleep_seconds,
                    )
                except RequestCapReachedError as exc:
                    if exc.provider == SOURCE_ID_POKEMON_TCG_API and use_pokewallet_prices:
                        fallback_reason = "pokemon_tcg_api_request_cap_reached"
                        metrics["providerFallbackReasons"].append({"setId": set_id, "reason": fallback_reason})
                        print(f"  Skipping pokemon_tcg_api fallback for set {set_id}: request cap exhausted")
                        keep_existing_after_budget_stop(set_id, set_name)
                        break
                    print(f"  [WARN] Stopping EN current prices due to request cap while building set {set_id}")
                    keep_existing_after_budget_stop(set_id, set_name)
                    metrics["currentPriceEnStopReason"] = f"request_cap_reached:{exc.provider or 'unknown'}"
                    request_cap_reached = True
                    break
                except ProviderRateLimitError as exc:
                    print(f"  [WARN] Provider rate limit while building EN prices for set {set_id}: {exc}")
                    metrics["currentPriceEnStatus"] = "rate_limited"
                    metrics["currentPriceEnRateLimited"] = True
                    metrics["currentPriceEnStopReason"] = f"rate_limited:{set_id}"
                    rate_limited = True
                    break
                except requests.RequestException as exc:
                    reason = transient_failure_reason(SOURCE_ID_POKEMON_TCG_API, exc)
                    print(f"  [WARN] Failed to build current prices for set {set_id}: {exc}")
                    if isinstance(exc, (requests.Timeout, requests.ReadTimeout, requests.ConnectTimeout)):
                        metrics["fallbackTimeout"] += 1
                    if not config.get("continueOnSetError", True):
                        failed_set_ids.append(set_id)
                        break
                    keep_existing_after_transient_failure(set_id, set_name, reason)
                    prices = []
                    break

                for card in cards:
                    prices.extend(extract_current_price_records(card, ts))

                if prices:
                    current_source = SOURCE_ID_POKEMON_TCG_API
                    print(f"  Pokemon TCG API success for set {set_id}: records={len(prices)}")
                elif fallback_reason == "":
                    fallback_reason = "pokemon_tcg_api_no_price_records"

        if rate_limited or request_cap_reached:
            break

        if require_pokewallet_prices and use_pokewallet_prices and not attempted_pokewallet_for_set:
            raise RuntimeError(f"PokeWallet required for set {set_id} but it was not attempted")

        if set_id in transient_failed_set_ids:
            continue

        prices.sort(key=price_sort_key)
        if not prices:
            metrics["currentPriceEnSkippedNoPriceSets"] += 1
            processed_set_ids.append(set_id)
        else:
            price_path = output_dir / f"{set_id}.json"
            set_next_expected_utc = expected_next_refresh_utc(ts, full_rotation_hours)
            payload = {
                "schemaVersion": SCHEMA_VERSION,
                "generatedAtUtc": ts,
                "game": "pokemon",
                "language": "en",
                "setId": set_id,
                "setName": set_name,
                "source": current_source,
                "currency": CURRENT_PRICE_CURRENCY,
                "priceCount": len(prices),
                "prices": prices,
            }
            if current_source == SOURCE_ID_POKEWALLET and provider_diagnostics is not None:
                payload["providerDiagnostics"] = provider_diagnostics
            enriched_payload = enrich_en_current_set_payload(
                payload,
                now_utc=ts,
                expected_update_interval_minutes=interval_minutes,
                full_rotation_hours=full_rotation_hours,
                force_last_successful_update_utc=ts,
                force_next_expected_update_utc=set_next_expected_utc,
            )
            write_json(price_path, enriched_payload)
            written_files.append((set_id, set_name, price_path))
            metrics["currentPriceEnSetsWritten"] += 1
            metrics["currentPriceEnSetsUpdated"] += 1
            metrics["setsUpdated"] += 1
            metrics["currentPriceEnPriceRecordsWritten"] += len(prices)
            processed_set_ids.append(set_id)
            if current_source not in metrics["currentPriceEnProviderUsed"]:
                metrics["currentPriceEnProviderUsed"].append(current_source)
            source_counts = metrics["providerSourceCounts"]
            source_counts[current_source] = int(source_counts.get(current_source, 0)) + 1
            if used_pokemon_fallback:
                metrics["pokemonTcgApiFallbackSets"] += 1
            if fallback_reason:
                metrics["currentPriceEnFallbackReasons"].append({"setId": set_id, "reason": fallback_reason})
                if not any(
                    (item.get("setId") == set_id and item.get("reason") == fallback_reason)
                    for item in metrics["providerFallbackReasons"]
                    if isinstance(item, dict)
                ):
                    metrics["providerFallbackReasons"].append({"setId": set_id, "reason": fallback_reason})
            if fail_after_set_count > 0 and metrics["currentPriceEnSetsWritten"] >= fail_after_set_count:
                raise RuntimeError(
                    "Intentional failure for local safety testing via "
                    "CARDSCANR_FAIL_AFTER_EN_PRICE_SET_COUNT"
                )

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    if failed_set_ids:
        metrics["currentPriceEnStatus"] = "partial_built"
        metrics["currentPriceEnStopReason"] = "set_error"
        failed_preview = ", ".join(failed_set_ids[:10])
        raise RuntimeError(
            "Failed to build EN current prices for one or more sets; "
            f"leaving existing public current-price cache untouched. sets={failed_preview}"
        )

    metrics["currentPriceEnRequestsUsed"] = int(REQUEST_TRACKER.get("attempted", 0))
    metrics["currentPriceEnProviderRequestCounts"] = provider_request_counts_summary()
    if (
        request_cap_reached
        and int(metrics.get("currentPriceEnSetsUpdated", 0) or 0) <= 0
        and int(metrics.get("currentPriceEnSetsKeptExisting", 0) or 0) <= 0
    ):
        metrics["currentPriceEnStatus"] = "partial_built"
        metrics["currentPriceEnStopReason"] = "request_cap_reached"
        raise RuntimeError(
            "Request cap reached before any safe EN current price output could be produced; "
            "leaving existing public current-price cache untouched."
        )
    if transient_failed_set_ids and int(metrics.get("currentPriceEnSetsUpdated", 0) or 0) <= 0:
        metrics["currentPriceEnStatus"] = "failed_transient"
        metrics["currentPriceEnStopReason"] = "all_sets_failed_transiently"
        failed_preview = ", ".join(transient_failed_set_ids[:10])
        raise RuntimeError(
            "All selected EN current price sets failed transiently; "
            f"leaving existing public current-price cache untouched. sets={failed_preview}"
        )
    if metrics["providerFallbackReasons"]:
        metrics["currentPriceEnFallbackReasons"] = list(metrics["providerFallbackReasons"])
    if metrics["providerFailureReasons"]:
        metrics["currentPriceEnProviderFailureReasons"] = list(metrics["providerFailureReasons"])

    if require_pokewallet_prices and use_pokewallet_prices and metrics["currentPriceEnSetsAttempted"] > 0:
        if int(metrics["pokewalletSetsAttempted"]) <= 0:
            raise RuntimeError(
                "PokeWallet required but no sets attempted via PokeWallet in current_prices mode"
            )

    if metrics["currentPriceEnProviderUsed"]:
        if len(metrics["currentPriceEnProviderUsed"]) == 1:
            metrics["currentPriceEnSource"] = metrics["currentPriceEnProviderUsed"][0]
        else:
            metrics["currentPriceEnSource"] = "mixed"
    elif metrics["currentPriceEnSource"] is None:
        metrics["currentPriceEnSource"] = CURRENT_PRICE_SOURCE
    if request_cap_reached:
        metrics["currentPriceEnStatus"] = "partial_built"
        if not metrics.get("currentPriceEnStopReason"):
            metrics["currentPriceEnStopReason"] = "request_cap_reached"
    elif transient_failed_set_ids:
        metrics["currentPriceEnStatus"] = "partial_built"
        metrics["currentPriceEnStopReason"] = "completed_with_transient_failures"
    elif not rate_limited:
        metrics["currentPriceEnStatus"] = "built"
        metrics["currentPriceEnStopReason"] = "completed"
    metrics["currentPriceEnBatchSetIds"] = selected_set_ids
    metrics["currentPriceEnProcessedSetIds"] = processed_set_ids

    next_state = dict(refresh_state)
    next_state["schemaVersion"] = SCHEMA_VERSION
    if batch_enabled and strategy == "rotating_set_batch" and mode in {"scheduled", "current_prices"}:
        total_sets = len(sets_all)
        processed_count = len(processed_set_ids)
        next_cursor = (cursor_before % total_sets + processed_count) % total_sets
        next_state["enCurrentPriceCursor"] = next_cursor
    else:
        next_state["enCurrentPriceCursor"] = cursor_after
    next_state["lastUpdatedAtUtc"] = ts
    next_state["lastBatchSetIds"] = selected_set_ids
    next_state["lastProcessedSetIds"] = processed_set_ids
    next_state["lastStopReason"] = str(metrics.get("currentPriceEnStopReason") or "completed")
    next_state["lastRateLimited"] = bool(metrics.get("currentPriceEnRateLimited"))

    return written_files, metrics, next_state


def load_existing_current_price_files(language: str = "en") -> list[tuple[str, str, Path]]:
    current_dir = CURRENT_PRICES_EN_DIR if language == "en" else CURRENT_PRICES_JP_DIR
    if not current_dir.exists():
        return []

    files: list[tuple[str, str, Path]] = []
    for path in sorted(current_dir.glob("*.json")):
        if path.name == "status.json":
            continue
        payload = load_json(path)
        if not isinstance(payload, dict):
            continue
        set_id = str(payload.get("setId") or path.stem)
        set_name = str(payload.get("setName") or set_id)
        files.append((set_id, set_name, path))
    return files


def keep_existing_current_price_file(
    *,
    set_id: str,
    output_dir: Path,
    existing_file_by_set_id: dict[str, tuple[str, Path]],
) -> Path | None:
    existing = existing_file_by_set_id.get(set_id)
    if existing is None:
        return None
    _existing_set_name, existing_path = existing
    if not existing_path.exists():
        return None
    destination = output_dir / existing_path.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(existing_path, destination)
    return destination


def build_index_dataset_entry(
    *,
    dataset_id: str,
    file_path: Path,
    dataset_type: str,
    description: str,
    ts: str,
    ttl_seconds: int,
    schema_version: str | None = SCHEMA_VERSION,
    game: str | None = None,
    language: str | None = None,
    extra: dict | None = None,
) -> dict:
    rel_url = f"/v1/{file_path.relative_to(PUBLIC_DIR).as_posix()}"
    entry = {
        "id": dataset_id,
        "url": rel_url,
        "sha256": sha256_file(file_path),
        "type": dataset_type,
        "description": description,
        "updatedAtUtc": ts,
        "recommendedCacheTtlSeconds": ttl_seconds,
    }
    if schema_version is not None:
        entry["schemaVersion"] = schema_version
    if game is not None:
        entry["game"] = game
    if language is not None:
        entry["language"] = language
    if extra:
        entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# Live Price Fetching
# ---------------------------------------------------------------------------
def extract_variant_prices(pricing: dict, variant: str) -> dict | None:
    variant_map = {
        "holo": pricing.get("holofoil"),
        "reverse": pricing.get("reverseHolofoil") or pricing.get("reverse-holofoil"),
        "normal": pricing.get("normal"),
        "first_edition": (
            pricing.get("1stEditionNormal")
            or pricing.get("1stEditionHolofoil")
            or pricing.get("1st-edition")
            or pricing.get("1st-edition-holofoil")
        ),
    }
    chosen = variant_map.get(variant)
    return chosen if isinstance(chosen, dict) else None


def compact_price_info(pricing: dict, source: str, currency: str) -> dict | None:
    market = to_float(pricing.get("marketPrice") or pricing.get("market") or pricing.get("trend"))
    low = to_float(pricing.get("lowPrice") or pricing.get("low"))
    high = to_float(pricing.get("highPrice") or pricing.get("high") or pricing.get("suggested"))

    if market is None:
        return None
    if low is None:
        low = round(market * 0.90, 2)
    if high is None:
        high = round(market * 1.10, 2)

    return {
        "marketPrice": market,
        "lowPrice": low,
        "highPrice": high,
        "currency": currency,
        "source": source,
    }


def find_tcgdex_card_id(card: dict) -> str | None:
    if card.get("tcgdexCardId"):
        return str(card["tcgdexCardId"])

    set_id = str(card.get("setId", ""))
    if not set_id:
        return None

    lang = tcgdex_language(card.get("language", "en"))
    url = f"https://api.tcgdex.net/v2/{lang}/sets/{set_id}/cards"

    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 404:
            return None
        response.raise_for_status()
    except requests.RequestException:
        return None

    cards = response.json()
    if not isinstance(cards, list):
        return None

    target_number = normalize_number(card.get("collectorNumber", ""))
    target_name = normalize_text(card.get("normalizedName", ""))

    strict_matches: list[str] = []
    loose_matches: list[str] = []

    for candidate in cards:
        if not isinstance(candidate, dict):
            continue

        candidate_id = candidate.get("id")
        local_id = candidate.get("localId")
        if not candidate_id or not local_id:
            continue

        if normalize_number(local_id) != target_number:
            continue

        candidate_name = normalize_text(candidate.get("name", ""))
        if candidate_name == target_name:
            strict_matches.append(candidate_id)
        elif target_name and (target_name in candidate_name or candidate_name in target_name):
            loose_matches.append(candidate_id)

    if len(strict_matches) == 1:
        return strict_matches[0]
    if not strict_matches and len(loose_matches) == 1:
        return loose_matches[0]
    return None


def fetch_prices_from_tcgdex(card: dict, diagnostics: dict) -> dict | None:
    diagnostics["tcgdexAttempted"] += 1

    card_id = find_tcgdex_card_id(card)
    if not card_id:
        diagnostics["tcgdexNoMatch"] += 1
        return None

    lang = tcgdex_language(card.get("language", "en"))
    url = f"https://api.tcgdex.net/v2/{lang}/cards/{card_id}"

    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 404:
            diagnostics["tcgdexNoMatch"] += 1
            return None
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"[ERROR] TCGdex request failed: {exc}")
        return None

    diagnostics["tcgdexMatched"] += 1

    card_data = response.json()
    pricing_root = card_data.get("pricing", {})
    if not isinstance(pricing_root, dict):
        return None

    tcgplayer_prices = pricing_root.get("tcgplayer")
    if isinstance(tcgplayer_prices, dict):
        variant_prices = extract_variant_prices(tcgplayer_prices, card.get("variant", "normal"))
        if variant_prices:
            compacted = compact_price_info(variant_prices, SOURCE_ID_TCGDEX_TCGPLAYER, "USD")
            if compacted:
                return compacted

    cardmarket_prices = pricing_root.get("cardmarket")
    if isinstance(cardmarket_prices, dict):
        variant_prices = extract_variant_prices(cardmarket_prices, card.get("variant", "normal"))
        if variant_prices:
            compacted = compact_price_info(variant_prices, SOURCE_ID_TCGDEX_CARDMARKET, "EUR")
            if compacted:
                return compacted

    return None


def fetch_prices_from_pokemon_tcg_api(card: dict) -> dict | None:
    headers = {}
    api_key = os.getenv("POKEMON_TCG_API_KEY")
    if api_key:
        headers["X-Api-Key"] = api_key

    try:
        card_data = None

        if card.get("pokemonTcgApiId"):
            direct_url = f"https://api.pokemontcg.io/v2/cards/{card['pokemonTcgApiId']}"
            response = requests.get(direct_url, headers=headers, timeout=10)
            if response.status_code != 404:
                response.raise_for_status()
                card_data = response.json().get("data")

        if card_data is None:
            query = (
                f"set.id:{card['setId']} number:{card['collectorNumber']} "
                f"name:{card['normalizedName']}"
            )
            response = requests.get(
                "https://api.pokemontcg.io/v2/cards",
                params={"q": query},
                headers=headers,
                timeout=10,
            )
            response.raise_for_status()
            results = response.json().get("data", [])
            if not results:
                return None
            card_data = results[0]

        pricing = card_data.get("tcgplayer", {}).get("prices", {})
        if not isinstance(pricing, dict):
            return None

        variant_prices = extract_variant_prices(pricing, card.get("variant", "normal"))
        if not variant_prices:
            return None

        return compact_price_info(variant_prices, SOURCE_ID_POKEMON_TCG_API, "USD")
    except requests.RequestException as exc:
        print(f"[ERROR] PokemonTCG API request failed: {exc}")
        return None


def fetch_live_price_info(card: dict, diagnostics: dict) -> dict:
    tcgdex_price = fetch_prices_from_tcgdex(card, diagnostics)
    if tcgdex_price:
        return tcgdex_price

    pokemon_price = fetch_prices_from_pokemon_tcg_api(card)
    if pokemon_price:
        return pokemon_price

    diagnostics["manualFallbackCount"] += 1
    print(f"[WARN] No live price found for {card['canonicalId']}, using manual_seed.")
    return manual_seed_price_info(card)


def resolve_build_mode(config: dict) -> str:
    mode, _set_id, _debug_provider_match = parse_build_cli_args()
    mode = mode or os.getenv("CACHE_BUILD_MODE") or config.get("buildMode") or "scheduled"
    mode = str(mode).strip().lower().replace("-", "_")
    allowed_modes = {
        "scheduled",
        "current_prices",
        "full_catalogue",
        "tracked_history",
        "japanese_catalogue",
        "app_catalogue",
    }
    if mode not in allowed_modes:
        raise ValueError(f"Unsupported build mode '{mode}'. Expected one of {sorted(allowed_modes)}")
    return mode


def parse_build_cli_args() -> tuple[str | None, str | None, bool]:
    mode: str | None = None
    set_id: str | None = None
    debug_provider_match = False
    args = sys.argv[1:]
    idx = 0
    while idx < len(args):
        token = str(args[idx] or "").strip()
        if token == "--debug-provider-match":
            debug_provider_match = True
            idx += 1
            continue
        if token in {"--set-id", "--set"}:
            if idx + 1 >= len(args):
                raise ValueError("--set-id requires a value")
            set_id = str(args[idx + 1]).strip()
            idx += 2
            continue
        if token.startswith("--set-id="):
            set_id = token.split("=", 1)[1].strip()
            idx += 1
            continue
        if token.startswith("-"):
            raise ValueError(f"Unsupported argument: {token}")
        if mode is None:
            mode = token
            idx += 1
            continue
        raise ValueError(f"Unexpected positional argument: {token}")
    return mode, set_id, debug_provider_match


def debug_pokewallet_provider_match_for_set(set_id: str) -> int:
    catalog_en = load_existing_catalogue_sets()
    sets = [item for item in catalog_en.get("sets", []) if isinstance(item, dict)] if isinstance(catalog_en, dict) else []
    selected: dict | None = None
    for item in sets:
        candidate_id = str(item.get("id") or "").strip().lower()
        if candidate_id == set_id.strip().lower():
            selected = item
            break
    if selected is None:
        print(f"[provider-match] set not found in EN catalogue: {set_id}")
        return 1

    set_map = load_pokewallet_set_code_map()
    match = resolve_pokewallet_set_match(selected, set_map)
    code = str(match.get("matchedCode") or "").strip()
    reason = str(match.get("reason") or "unknown")

    print("[provider-match] PokeWallet set-code mapping debug")
    print(f"- app setId: {selected.get('id')}")
    print(f"- app setName: {selected.get('name')}")
    print(f"- app ptcgoCode: {selected.get('ptcgoCode')}")
    print(f"- app releaseDate: {selected.get('releaseDate')}")
    print(f"- app printedTotal: {selected.get('printedTotal')}")
    print(f"- match reason: {reason}")
    if code:
        print(f"- matched providerSetCode: {code}")
        return 0

    for diag in build_pokewallet_match_diagnostics(
        str(selected.get("id") or set_id),
        str(selected.get("name") or ""),
        reason,
        [item for item in match.get("candidates", []) if isinstance(item, dict)],
    ):
        print(diag)
    return 1


def should_build_tracked_history(mode: str) -> bool:
    return mode in {"scheduled", "tracked_history", "full_catalogue"}


def should_build_current_prices(mode: str, config: dict) -> bool:
    return mode in {"scheduled", "current_prices", "full_catalogue"} and bool(
        config.get("buildCurrentPricesFromPokemonTcgApi", True)
    )


def should_build_full_catalogue(mode: str, config: dict) -> bool:
    if mode in {"full_catalogue", "app_catalogue"}:
        return True
    if mode == "scheduled" and config.get("rebuildFullCatalogueOnScheduled", False):
        return True
    return False


def should_build_japanese_catalogue(mode: str, config: dict) -> bool:
    if mode == "japanese_catalogue":
        return bool(config.get("buildJapaneseFromTcgdex", True))
    if mode in {"full_catalogue", "app_catalogue"}:
        return bool(config.get("buildJapaneseFromTcgdex", True))
    if mode == "scheduled" and config.get("rebuildFullCatalogueOnScheduled", False):
        return bool(config.get("buildJapaneseFromTcgdex", True)) and bool(
            config.get("scheduledJapaneseCatalogueEnabled", False)
        )
    return False


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------
def build() -> None:
    global CURRENT_PRICE_REQUEST_CAP, CURRENT_PRICE_PROVIDER_REQUEST_CAPS
    ts = now_utc()
    day = ts[:10]
    catalog_config = load_catalog_config()
    cli_mode, cli_set_id, debug_provider_match = parse_build_cli_args()
    mode = resolve_build_mode(catalog_config)
    effective_set_id = str(cli_set_id or os.getenv(CURRENT_PRICE_SET_ID_ENV, "")).strip()
    if debug_provider_match:
        if not effective_set_id:
            print("[provider-match] --debug-provider-match requires --set-id or CARDSCANR_CURRENT_PRICE_SET_ID")
            sys.exit(2)
        sys.exit(debug_pokewallet_provider_match_for_set(effective_set_id))
    refresh_state_path = resolve_state_path(catalog_config)
    refresh_state = load_scheduled_refresh_state(refresh_state_path)
    next_refresh_state: dict | None = None
    fail_after_en_count = parse_positive_int_env("CARDSCANR_FAIL_AFTER_EN_PRICE_SET_COUNT")
    set_id_override = effective_set_id or None
    reset_tmp_build_root(TMP_BUILD_ROOT)
    reset_request_tracker()
    if mode == "current_prices":
        CURRENT_PRICE_REQUEST_CAP = resolve_current_price_request_cap()
        CURRENT_PRICE_PROVIDER_REQUEST_CAPS = resolve_provider_current_price_request_caps()
    else:
        CURRENT_PRICE_REQUEST_CAP = 0
        CURRENT_PRICE_PROVIDER_REQUEST_CAPS = {}
    print(f"[build_price_cache] Starting {mode} build at {ts}")

    cards: list[dict] = load_json(CARDS_PATH).get("cards", [])
    if not cards:
        print("[build_price_cache] No cards found in cards_to_track.json - nothing to do.")
        sys.exit(0)

    groups: dict[tuple[str, str], list[dict]] = {}
    for card in cards:
        key = (card["game"], card["language"])
        groups.setdefault(key, []).append(card)

    diagnostics = {
        "buildStatus": "success",
        "builtAtUtc": ts,
        "cacheVersion": datetime.now(timezone.utc).strftime("%Y.%m.%d.%H%M"),
        "cardsRequested": len(cards),
        "cardsPriced": 0,
        "tcgdexAttempted": 0,
        "tcgdexMatched": 0,
        "tcgdexNoMatch": 0,
        "livePriceCount": 0,
        "manualFallbackCount": 0,
        "noResultCount": 0,
        "errorCount": 0,
        "sourcesUsed": set(),
        "datasetsBuilt": [],
        "trackedHistoryWritten": False,
        "trackedCardsTotal": 0,
        "dailyHistoryFilesWritten": 0,
        "firstTrackedCreatedCount": 0,
        "trackedCardsUpdatedCount": 0,
        "catalogueEnStatus": "not_built_yet",
        "catalogueEnFetchStrategy": "set_by_set",
        "catalogueEnSetCount": 0,
        "catalogueEnSetsAttempted": 0,
        "catalogueEnSetsBuilt": 0,
        "catalogueEnSetsFailed": 0,
        "catalogueEnCardsFetched": 0,
        "catalogueEnFailedSetIds": [],
        "catalogueEnStoppedReason": None,
        "catalogueJpStatus": "not_built_yet",
        "catalogueJpProviderLanguage": "ja",
        "catalogueJpSourceStrategy": "tcgdex_set_details_plus_global_card_list",
        "catalogueJpFetchStrategy": str(catalog_config.get("japaneseCatalogueFetchStrategy", "tcgdex_set_by_set")),
        "catalogueJpSetCount": 0,
        "catalogueJpSetsAttempted": 0,
        "catalogueJpSetsBuilt": 0,
        "catalogueJpSetsFailed": 0,
        "catalogueJpCardsFetched": 0,
        "catalogueJpCardsFromSetDetails": 0,
        "catalogueJpCardsFromGlobalList": 0,
        "catalogueJpCardsMergedTotal": 0,
        "catalogueJpDuplicateCardsRemoved": 0,
        "catalogueJpGlobalCardsFetched": 0,
        "catalogueJpGlobalCardsGrouped": 0,
        "catalogueJpGlobalCardsSkippedUnparseableId": 0,
        "catalogueJpGlobalCardsSkippedUnknownSet": 0,
        "catalogueJpCoverageImprovedByGlobalFallback": False,
        "catalogueJpEndpointExamples": [],
        "catalogueJpEmptySetIds": [],
        "catalogueJpSetsSkippedEmptyCards": 0,
        "catalogueJpSkippedEmptySetIds": [],
        "catalogueJpFailedSetIds": [],
        "catalogueJpStoppedReason": None,
        "currentPriceEnStatus": "not_built_yet",
        "currentPriceEnSetsAttempted": 0,
        "currentPriceEnSetsWritten": 0,
        "currentPriceEnPriceRecordsWritten": 0,
        "currentPriceEnSkippedNoPriceSets": 0,
        "currentPriceEnSource": CURRENT_PRICE_SOURCE,
        "currentPriceEnCurrency": CURRENT_PRICE_CURRENCY,
        "currentPriceJpStatus": "not_built_yet",
        "currentPriceJpSetsWritten": 0,
        "currentPriceJpPriceRecordsWritten": 0,
        "currentPriceJpSkippedNoPriceSets": 0,
            "providerRequestsAttempted": 0,
            "providerRequestCounts": {},
            "providerRequestCaps": {},
            "pokewalletRequestsAttempted": 0,
            "pokemonTcgApiRequestsAttempted": 0,
            "tcgdexRequestsAttempted": 0,
            "providerRequestsSucceeded": 0,
            "providerRequestsFailed": 0,
            "providerRateLimitedCount": 0,
            "stopReason": None,
            "rateLimitStatus": "not_limited",
    }

    cards_by_id: dict[str, dict] = {}
    latest_prices_by_id: dict[str, dict] = {}
    daily_history_files: list[tuple[str, str, Path]] = []
    sample_price_files: list[tuple[str, str, Path]] = []

    pending_json_writes: list[tuple[Path, dict]] = []
    previous_prices_status = load_json(PRICES_STATUS_PATH) if PRICES_STATUS_PATH.exists() else None

    try:
        if should_build_tracked_history(mode):
            for (game, language), group_cards in sorted(groups.items()):
                price_path = PRICES_DIR / game / language / "sample.json"
                history_path = HISTORY_DIR / day / game / language / "tracked.json"

                seen: set[str] = set()
                prices = []
                for card in group_cards:
                    cid = card["canonicalId"]
                    if cid in seen:
                        print(f"  [WARN] Duplicate canonicalId skipped: {cid}")
                        continue

                    seen.add(cid)
                    price_info = fetch_live_price_info(card, diagnostics)
                    diagnostics["sourcesUsed"].add(price_info["source"])

                    if price_info["source"] != SOURCE_ID_MANUAL_SEED:
                        diagnostics["livePriceCount"] += 1

                    diagnostics["cardsPriced"] += 1
                    price_entry = build_price_entry(card, ts, price_info)
                    prices.append(price_entry)
                    cards_by_id[cid] = card
                    latest_prices_by_id[cid] = price_entry

                payload = {
                    "schemaVersion": SCHEMA_VERSION,
                    "generatedAtUtc": ts,
                    "game": game,
                    "language": language,
                    "prices": prices,
                }
                pending_json_writes.append((price_path, payload))

                history_payload = {
                    "schemaVersion": SCHEMA_VERSION,
                    "generatedAtUtc": ts,
                    "date": day,
                    "game": game,
                    "language": language,
                    "prices": prices,
                }
                pending_json_writes.append((history_path, history_payload))
                diagnostics["dailyHistoryFilesWritten"] += 1
                daily_history_files.append((game, language, history_path))
                sample_price_files.append((game, language, price_path))

            tracked_payload, first_created, tracked_updated = update_tracked_cards_history(
                ts=ts,
                cards_by_id=cards_by_id,
                latest_prices_by_id=latest_prices_by_id,
            )
            diagnostics["trackedHistoryWritten"] = True
            diagnostics["trackedCardsTotal"] = len(tracked_payload.get("cards", []))
            diagnostics["firstTrackedCreatedCount"] = first_created
            diagnostics["trackedCardsUpdatedCount"] = tracked_updated
            pending_json_writes.append((TRACKED_CARDS_PATH, tracked_payload))
        else:
            for game, language in sorted(groups.keys()):
                price_path = PRICES_DIR / game / language / "sample.json"
                if price_path.exists():
                    sample_price_files.append((game, language, price_path))
            if TRACKED_CARDS_PATH.exists():
                tracked_payload = load_json(TRACKED_CARDS_PATH)
                if isinstance(tracked_payload, dict):
                    diagnostics["trackedCardsTotal"] = len(tracked_payload.get("cards", []))

        api_manifest = build_api_manifest(ts)
        api_notes = build_api_notes(ts)
        schemas = build_schemas(ts)

        if should_build_full_catalogue(mode, catalog_config):
            catalog_en, catalog_en_card_files, catalog_metrics = build_english_pokemon_catalogue(ts, catalog_config)
            write_json(CATALOG_DIR / "pokemon" / "en" / "sets.json", catalog_en)
        else:
            catalog_en = load_existing_catalogue_sets()
            catalog_en_card_files = load_existing_catalogue_card_files()
            catalog_metrics = {
                "catalogueEnStatus": catalog_en.get("catalogueStatus", "not_built_yet"),
                "catalogueEnFetchStrategy": "set_by_set",
                "catalogueEnSetCount": int(catalog_en.get("setCount", 0) or 0),
                "catalogueEnSetsAttempted": 0,
                "catalogueEnSetsBuilt": 0,
                "catalogueEnSetsFailed": int(catalog_en.get("failedSetCount", 0) or 0),
                "catalogueEnCardsFetched": int(catalog_en.get("cardCount", 0) or 0),
                "catalogueEnFailedSetIds": catalog_en.get("failedSetIds", []),
                "catalogueEnStoppedReason": "not_rebuilt",
            }
        diagnostics.update(catalog_metrics)

        if should_build_current_prices(mode, catalog_config):
            staged_en_current_dir = prepare_empty_dir(TMP_BUILD_ROOT / "prices" / "current" / "pokemon" / "en")
            _staged_price_files, current_price_metrics, next_refresh_state = build_english_current_prices_by_set(
                ts,
                catalog_config,
                catalog_en,
                staged_en_current_dir,
                mode,
                refresh_state,
                fail_after_set_count=fail_after_en_count,
                set_id_override=set_id_override,
            )
            publish_staged_directory(staged_en_current_dir, CURRENT_PRICES_EN_DIR)
            broad_current_price_files = load_existing_current_price_files("en")
        else:
            broad_current_price_files = load_existing_current_price_files("en")
            current_price_metrics = {
                "currentPriceEnStatus": "not_rebuilt",
                "currentPriceEnSetsAttempted": 0,
                "currentPriceEnSetsWritten": len(broad_current_price_files),
                "currentPriceEnPriceRecordsWritten": 0,
                "currentPriceEnSkippedNoPriceSets": 0,
                "currentPriceEnSource": CURRENT_PRICE_SOURCE,
                "currentPriceEnCurrency": CURRENT_PRICE_CURRENCY,
                "currentPriceEnRequestCap": CURRENT_PRICE_REQUEST_CAP,
                "currentPriceEnProviderRequestCaps": dict(CURRENT_PRICE_PROVIDER_REQUEST_CAPS),
                "currentPriceEnRequestsUsed": int(REQUEST_TRACKER.get("attempted", 0)),
                "currentPriceEnProviderRequestCounts": provider_request_counts_summary(),
            }
        diagnostics.update(current_price_metrics)
        diagnostics["requestCap"] = current_price_metrics.get("currentPriceEnRequestCap")
        diagnostics["requestsUsed"] = current_price_metrics.get("currentPriceEnRequestsUsed", int(REQUEST_TRACKER.get("attempted", 0)))
        diagnostics["stopReason"] = current_price_metrics.get("currentPriceEnStopReason") or diagnostics.get("stopReason")
        if str(current_price_metrics.get("currentPriceEnStatus") or "") == "rate_limited":
            diagnostics["buildStatus"] = "rate_limited"
            diagnostics["stopReason"] = str(current_price_metrics.get("currentPriceEnStopReason") or "rate_limited")
            diagnostics["rateLimitStatus"] = "rate_limited"

        if should_build_japanese_catalogue(mode, catalog_config):
            (
                catalog_jp,
                catalog_jp_card_files,
                catalog_jp_metrics,
                jp_current_price_files,
                jp_current_price_metrics,
            ) = build_japanese_pokemon_catalogue(ts, catalog_config)
            write_json(CATALOG_DIR / "pokemon" / "jp" / "sets.json", catalog_jp)
        else:
            catalog_jp = load_existing_japanese_catalogue(ts)
            catalog_jp_card_files = load_existing_japanese_catalogue_card_files()
            jp_current_price_files = load_existing_current_price_files("jp")
            catalog_jp_metrics = {
                "catalogueJpStatus": catalog_jp.get("catalogueStatus", "not_built_yet"),
                "catalogueJpProviderLanguage": "ja",
                "catalogueJpSourceStrategy": "tcgdex_set_details_plus_global_card_list",
                "catalogueJpFetchStrategy": str(
                    catalog_config.get("japaneseCatalogueFetchStrategy", "tcgdex_set_by_set")
                ),
                "catalogueJpSetCount": int(catalog_jp.get("setCount", 0) or 0),
                "catalogueJpSetsAttempted": 0,
                "catalogueJpSetsBuilt": 0,
                "catalogueJpSetsFailed": int(catalog_jp.get("failedSetCount", 0) or 0),
                "catalogueJpCardsFetched": int(catalog_jp.get("cardCount", 0) or 0),
                "catalogueJpCardsFromSetDetails": int(catalog_jp.get("cardCount", 0) or 0),
                "catalogueJpCardsFromGlobalList": 0,
                "catalogueJpCardsMergedTotal": int(catalog_jp.get("cardCount", 0) or 0),
                "catalogueJpDuplicateCardsRemoved": 0,
                "catalogueJpGlobalCardsFetched": 0,
                "catalogueJpGlobalCardsGrouped": 0,
                "catalogueJpGlobalCardsSkippedUnparseableId": 0,
                "catalogueJpGlobalCardsSkippedUnknownSet": 0,
                "catalogueJpCoverageImprovedByGlobalFallback": False,
                "catalogueJpSetsSkippedEmptyCards": int(catalog_jp.get("partialSetCount", 0) or 0),
                "catalogueJpSkippedEmptySetIds": [],
                "catalogueJpEmptySetIds": [],
                "catalogueJpFailedSetIds": catalog_jp.get("failedSetIds", []),
                "catalogueJpStoppedReason": "not_rebuilt",
            }
            jp_current_price_metrics = {
                "currentPriceJpStatus": "not_rebuilt",
                "currentPriceJpSetsWritten": len(jp_current_price_files),
                "currentPriceJpPriceRecordsWritten": 0,
                "currentPriceJpSkippedNoPriceSets": 0,
            }
        diagnostics.update(catalog_jp_metrics)
        diagnostics.update(jp_current_price_metrics)

        diagnostics["providerRequestsAttempted"] = int(REQUEST_TRACKER.get("attempted", 0))
        diagnostics["providerRequestCounts"] = provider_request_counts_summary()
        diagnostics["providerRequestCaps"] = dict(CURRENT_PRICE_PROVIDER_REQUEST_CAPS)
        diagnostics["pokewalletRequestsAttempted"] = provider_request_metric(SOURCE_ID_POKEWALLET, "attempted")
        diagnostics["pokemonTcgApiRequestsAttempted"] = provider_request_metric(SOURCE_ID_POKEMON_TCG_API, "attempted")
        diagnostics["tcgdexRequestsAttempted"] = provider_request_metric(SOURCE_ID_TCGDEX, "attempted")
        diagnostics["providerRequestsSucceeded"] = int(REQUEST_TRACKER.get("succeeded", 0))
        diagnostics["providerRequestsFailed"] = int(REQUEST_TRACKER.get("failed", 0))
        diagnostics["providerRateLimitedCount"] = int(REQUEST_TRACKER.get("rateLimited", 0))
        if diagnostics["providerRateLimitedCount"] > 0 and diagnostics.get("rateLimitStatus") == "not_limited":
            diagnostics["rateLimitStatus"] = "rate_limited"
            if diagnostics.get("stopReason") in (None, ""):
                diagnostics["stopReason"] = "provider_rate_limited"

        state_for_status = next_refresh_state if next_refresh_state is not None else refresh_state
        prices_status, en_prices_status, jp_prices_status = build_public_price_status_payloads(
            ts=ts,
            diagnostics=diagnostics,
            config=catalog_config,
            refresh_state=state_for_status,
            previous_prices_status=previous_prices_status,
        )

        diagnostics["sourcesUsed"] = sorted(diagnostics["sourcesUsed"])

        for path, payload in pending_json_writes:
            write_json(path, payload)

        write_json(API_MANIFEST_PATH, api_manifest)
        write_json(API_NOTES_PATH, api_notes)
        write_json(SCHEMAS_PATH, schemas)
        write_json(PRICES_STATUS_PATH, prices_status)
        write_json(EN_CURRENT_STATUS_PATH, en_prices_status)
        write_json(JP_CURRENT_STATUS_PATH, jp_prices_status)

        supported_languages = build_supported_language_manifest(ts)
        supported_markets = build_supported_market_manifest(ts)
        write_json(SUPPORTED_LANGUAGES_PATH, supported_languages)
        write_json(SUPPORTED_MARKETS_PATH, supported_markets)

        if not (CATALOG_DIR / "pokemon" / "jp" / "sets.json").exists():
            write_json(CATALOG_DIR / "pokemon" / "jp" / "sets.json", catalog_jp)

        index_entries = []
        index_entries.append(
        build_index_dataset_entry(
            dataset_id="app_config",
            file_path=APP_CONFIG_PATH,
            dataset_type="app_config",
            description="CardScanR remote app settings",
            ts=ts,
            ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
            schema_version=None,
        )
    )
        index_entries.append(
        build_index_dataset_entry(
            dataset_id="api_manifest",
            file_path=API_MANIFEST_PATH,
            dataset_type="api_manifest",
            description="CardScanR internal data API manifest",
            ts=ts,
            ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
        )
    )
        index_entries.append(
        build_index_dataset_entry(
            dataset_id="api_notes",
            file_path=API_NOTES_PATH,
            dataset_type="api_notes",
            description="CardScanR internal app data notes",
            ts=ts,
            ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
        )
    )
        index_entries.append(
        build_index_dataset_entry(
            dataset_id="schemas",
            file_path=SCHEMAS_PATH,
            dataset_type="schemas",
            description="Machine-readable CardScanR cache schema docs",
            ts=ts,
            ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
        )
    )
        index_entries.append(
        build_index_dataset_entry(
            dataset_id="supported_languages",
            file_path=SUPPORTED_LANGUAGES_PATH,
            dataset_type="supported_languages",
            description="CardScanR supported language and catalogue availability manifest",
            ts=ts,
            ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
        )
    )
        index_entries.append(
        build_index_dataset_entry(
            dataset_id="supported_markets",
            file_path=SUPPORTED_MARKETS_PATH,
            dataset_type="supported_markets",
            description="CardScanR supported market and pricing availability manifest",
            ts=ts,
            ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
        )
    )
        for game, language, price_path in sample_price_files:
            dataset_id = f"prices_{game}_{language}"
            index_entries.append(
            build_index_dataset_entry(
                dataset_id=dataset_id,
                file_path=price_path,
                dataset_type="price_current",
                description=f"{game.capitalize()} TCG {language.upper()} current tracked prices",
                ts=ts,
                ttl_seconds=PRICE_CACHE_TTL_SECONDS,
                game=game,
                language=language,
            )
        )

        index_entries.append(
        build_index_dataset_entry(
            dataset_id="prices_status",
            file_path=PRICES_STATUS_PATH,
            dataset_type="price_status",
            description="CardScanR app-facing UTC price freshness/status summary",
            ts=ts,
            ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
            game="pokemon",
        )
    )
        index_entries.append(
        build_index_dataset_entry(
            dataset_id="prices_current_pokemon_en_status",
            file_path=EN_CURRENT_STATUS_PATH,
            dataset_type="price_current_status",
            description="CardScanR app-facing UTC price freshness/status for Pokemon EN",
            ts=ts,
            ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
            game="pokemon",
            language="en",
        )
    )
        index_entries.append(
        build_index_dataset_entry(
            dataset_id="prices_current_pokemon_jp_status",
            file_path=JP_CURRENT_STATUS_PATH,
            dataset_type="price_current_status",
            description="CardScanR app-facing UTC price freshness/status for Pokemon JP",
            ts=ts,
            ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
            game="pokemon",
            language="jp",
        )
    )

        for set_id, set_name, price_path in broad_current_price_files:
            index_entries.append(
            build_index_dataset_entry(
                dataset_id=f"prices_current_pokemon_en_{set_id}",
                file_path=price_path,
                dataset_type="price_current",
                description=f"Pokemon TCG EN latest-known current prices for {set_name}",
                ts=ts,
                ttl_seconds=PRICE_CACHE_TTL_SECONDS,
                game="pokemon",
                language="en",
            )
        )

        for set_id, set_name, price_path in jp_current_price_files:
            index_entries.append(
            build_index_dataset_entry(
                dataset_id=f"prices_current_pokemon_jp_{set_id}",
                file_path=price_path,
                dataset_type="price_current",
                description=f"Pokemon TCG JP latest-known current prices for {set_name}",
                ts=ts,
                ttl_seconds=PRICE_CACHE_TTL_SECONDS,
                game="pokemon",
                language="jp",
            )
        )

        if TRACKED_CARDS_PATH.exists():
            index_entries.append(
            build_index_dataset_entry(
                dataset_id="tracked_history",
                file_path=TRACKED_CARDS_PATH,
                dataset_type="tracked_history",
                description="CardScanR tracked price history summary",
                ts=ts,
                ttl_seconds=HISTORY_CACHE_TTL_SECONDS,
            )
        )

        for game, language, history_path in daily_history_files:
            dataset_id = f"daily_tracked_history_{game}_{language}_{day}"
            index_entries.append(
            build_index_dataset_entry(
                dataset_id=dataset_id,
                file_path=history_path,
                dataset_type="daily_tracked_history",
                description=f"CardScanR tracked history snapshot for {game} {language.upper()} on {day}",
                ts=ts,
                ttl_seconds=HISTORY_CACHE_TTL_SECONDS,
                game=game,
                language=language,
                extra={"date": day},
            )
        )

        for game, language, catalog_path in [
            ("pokemon", "en", CATALOG_DIR / "pokemon" / "en" / "sets.json"),
            ("pokemon", "jp", CATALOG_DIR / "pokemon" / "jp" / "sets.json"),
        ]:
            dataset_id = f"catalog_{game}_{language}_sets"
            is_real_en_catalog = game == "pokemon" and language == "en" and catalog_en.get("catalogueStatus") in {
                "built",
                "partial_built",
            }
            is_real_jp_catalog = game == "pokemon" and language == "jp" and catalog_jp.get("catalogueStatus") in {
                "built",
                "partial_built",
            }
            description = (
                "Pokemon TCG EN catalogue sets"
                if is_real_en_catalog
                else (
                    "Pokemon TCG JP catalogue sets"
                    if is_real_jp_catalog
                    else f"{game.capitalize()} TCG {language.upper()} catalogue sets placeholder"
                )
            )
            index_entries.append(
            build_index_dataset_entry(
                dataset_id=dataset_id,
                file_path=catalog_path,
                dataset_type="catalogue_sets",
                description=description,
                ts=ts,
                ttl_seconds=CATALOG_CACHE_TTL_SECONDS,
                game=game,
                language=language,
            )
        )

        for set_id, set_name, card_path in catalog_en_card_files:
            index_entries.append(
            build_index_dataset_entry(
                dataset_id=f"catalog_pokemon_en_cards_{set_id}",
                file_path=card_path,
                dataset_type="catalogue_cards",
                description=f"Pokemon TCG EN catalogue cards for {set_name}",
                ts=ts,
                ttl_seconds=CATALOG_CACHE_TTL_SECONDS,
                game="pokemon",
                language="en",
            )
        )

        for set_id, set_name, card_path in catalog_jp_card_files:
            index_entries.append(
            build_index_dataset_entry(
                dataset_id=f"catalog_pokemon_jp_cards_{set_id}",
                file_path=card_path,
                dataset_type="catalogue_cards",
                description=f"Pokemon TCG JP catalogue cards for {set_name}",
                ts=ts,
                ttl_seconds=CATALOG_CACHE_TTL_SECONDS,
                game="pokemon",
                language="jp",
            )
        )

        deduped_index_entries: dict[str, dict] = {}
        for entry in index_entries:
            deduped_index_entries[entry["id"]] = entry
        index_entries = list(deduped_index_entries.values())
        index_entries.sort(key=lambda entry: entry["id"])
        diagnostics["datasetsBuilt"] = [entry["id"] for entry in index_entries] + ["diagnostics"]

        write_json(DIAG_PATH, diagnostics)

        index_entries.append(
        build_index_dataset_entry(
            dataset_id="diagnostics",
            file_path=DIAG_PATH,
            dataset_type="diagnostics",
            description="Latest CardScanR cache build diagnostics",
            ts=ts,
            ttl_seconds=DIAGNOSTICS_CACHE_TTL_SECONDS,
        )
    )
        index_entries.sort(key=lambda entry: entry["id"])

        index = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAtUtc": ts,
            "cacheVersion": diagnostics["cacheVersion"],
            "datasets": index_entries,
        }
        write_json(INDEX_PATH, index)
        if next_refresh_state is not None:
            save_scheduled_refresh_state(refresh_state_path, next_refresh_state)
        print(f"  Updated {INDEX_PATH}")
        print(f"  Updated {TRACKED_CARDS_PATH}")
        print(f"  Updated {DIAG_PATH}")

        print("[build_price_cache] Build complete.")
    finally:
        cleanup_tmp_build_root(TMP_BUILD_ROOT)


if __name__ == "__main__":
    build()
