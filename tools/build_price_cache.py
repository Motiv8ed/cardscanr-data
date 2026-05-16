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
from datetime import datetime, timezone
from pathlib import Path

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
BASE_URL = "https://cardscanr-cache.pages.dev/v1"
DEFAULT_CACHE_TTL_SECONDS = 86400
PRICE_CACHE_TTL_SECONDS = 43200
DIAGNOSTICS_CACHE_TTL_SECONDS = 900
HISTORY_CACHE_TTL_SECONDS = 86400
CATALOG_CACHE_TTL_SECONDS = 86400
POKEMON_TCG_API_BASE = "https://api.pokemontcg.io/v2"
CURRENT_PRICE_SOURCE = "pokemon_tcg_api"
CURRENT_PRICE_CURRENCY = "USD"
CURRENT_PRICE_VARIANTS = [
    ("normal", "normal"),
    ("holofoil", "holo"),
    ("reverseHolofoil", "reverse"),
    ("1stEditionHolofoil", "first_edition_holo"),
    ("1stEditionNormal", "first_edition_normal"),
]
TMP_BUILD_ROOT = ROOT / ".cache_build_tmp"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
SCHEMA_VERSION = "1.0.0"

LANGUAGE_TO_TCGDEX = {
    "en": "en",
    "jp": "ja",
    "ja": "ja",
}


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
        "source": "manual_seed",
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
                ],
                "notes": [
                    "Current price cache entry.",
                    "Per-set current price files are latest-known snapshots and may be overwritten each build.",
                    "At least one of marketPrice, lowPrice, or highPrice should be numeric when present.",
                    "Currency is stored per price record.",
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
                    "priceCount",
                    "prices",
                ],
                "notes": [
                    "English Pokemon current prices by set are built from PokemonTCG API card pricing fields.",
                    "Japanese Pokemon current prices by set are optional and only appear when official/free source pricing is available.",
                    "These files are latest-known current snapshots, not lifetime/all-time price history.",
                    "Tracked historical movement remains limited to CardScanR-tracked cards.",
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
    response = requests.get(
        f"{POKEMON_TCG_API_BASE}/{endpoint.lstrip('/')}",
        params=params or {},
        headers=pokemon_tcg_headers(),
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
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
        "imageSource": "pokemon_tcg_api",
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
        "subtypes": card.get("subtypes") if isinstance(card.get("subtypes"), list) else [],
        "types": card.get("types") if isinstance(card.get("types"), list) else [],
        "hp": card.get("hp"),
        "artist": card.get("artist"),
        "imageSmall": images.get("small"),
        "imageLarge": images.get("large"),
        "imageSource": "pokemon_tcg_api",
        "imageCached": False,
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
        "imageSource": "tcgdex",
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
        "imageSmall": build_tcgdex_card_image_url("ja", serie_id, set_id, collector_number, "low"),
        "imageLarge": build_tcgdex_card_image_url("ja", serie_id, set_id, collector_number, "high"),
        "imageSource": "tcgdex",
        "imageCached": False,
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
        "imageSmall": image,
        "imageLarge": image,
        "imageSource": "tcgdex",
        "imageCached": False,
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
                "source": "tcgdex",
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
        "source": "tcgdex",
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
                "source": "pokemon_tcg_api",
                "catalogueStatus": "built",
                "cardCount": len(card_records),
                "cards": card_records,
            }
            card_path = cards_dir / f"{set_id}.json"
            write_json(card_path, card_payload)
            card_files.append((set_id, set_name, card_path))
            metrics["catalogueEnSetsBuilt"] += 1
            metrics["catalogueEnCardsFetched"] += len(card_records)
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
        "source": "pokemon_tcg_api",
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


def build_current_price_record(card: dict, variant: str, pricing: dict, ts: str) -> dict | None:
    compacted = compact_current_price(pricing)
    if compacted is None:
        return None

    set_data = card.get("set") if isinstance(card.get("set"), dict) else {}
    set_id = str(set_data.get("id") or "")
    collector_number = str(card.get("number") or "")
    normalized_name = normalize_catalog_name(card.get("name", ""))

    return {
        "canonicalId": f"pokemon|en|{set_id}|{collector_number}|{normalized_name}|{variant}|near_mint",
        "setId": set_id,
        "collectorNumber": collector_number,
        "normalizedName": normalized_name,
        "variant": variant,
        "condition": "near_mint",
        "currency": CURRENT_PRICE_CURRENCY,
        "marketPrice": compacted["marketPrice"],
        "lowPrice": compacted["lowPrice"],
        "highPrice": compacted["highPrice"],
        "source": CURRENT_PRICE_SOURCE,
        "fetchedAtUtc": ts,
    }


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


def build_english_current_prices_by_set(
    ts: str,
    config: dict,
    catalog_sets: dict,
    output_dir: Path,
    mode: str,
    refresh_state: dict,
    fail_after_set_count: int = 0,
) -> tuple[list[tuple[str, str, Path]], dict, dict]:
    metrics = {
        "currentPriceEnStatus": "not_built_yet",
        "currentPriceEnSetsAttempted": 0,
        "currentPriceEnSetsWritten": 0,
        "currentPriceEnPriceRecordsWritten": 0,
        "currentPriceEnSkippedNoPriceSets": 0,
        "currentPriceEnSource": CURRENT_PRICE_SOURCE,
        "currentPriceEnCurrency": CURRENT_PRICE_CURRENCY,
    }

    if not config.get("buildCurrentPricesFromPokemonTcgApi", True):
        metrics["currentPriceEnStatus"] = "disabled_by_config"
        return [], metrics

    if not isinstance(catalog_sets, dict) or catalog_sets.get("catalogueStatus") not in {"built", "partial_built"}:
        metrics["currentPriceEnStatus"] = "skipped_no_built_catalogue"
        return [], metrics, refresh_state

    sets_all = [item for item in catalog_sets.get("sets", []) if isinstance(item, dict) and item.get("id")]
    sets_all.sort(key=lambda item: str(item.get("id") or ""))
    if not sets_all:
        metrics["currentPriceEnStatus"] = "skipped_no_sets"
        return [], metrics, refresh_state

    strategy = str(
        config.get("scheduledCurrentPriceRefreshStrategy")
        or config.get("localUpdaterRefreshStrategy")
        or "rotating_set_batch"
    ).strip().lower()
    batch_enabled = bool(config.get("scheduledCurrentPriceBatchEnabled", True))

    selected_sets = list(sets_all)
    cursor_before = int(refresh_state.get("enCurrentPriceCursor", 0) or 0)
    cursor_after = cursor_before
    selected_set_ids: list[str] = [str(item.get("id") or "") for item in selected_sets]

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

    prepare_empty_dir(output_dir)

    existing_current_files = load_existing_current_price_files("en")
    selected_set_id_lookup = {str(item.get("id") or "") for item in selected_sets}
    for existing_set_id, _existing_set_name, existing_path in existing_current_files:
        if existing_set_id in selected_set_id_lookup:
            continue
        destination = output_dir / existing_path.name
        shutil.copy2(existing_path, destination)

    page_size = int(config.get("pageSize", 250))
    max_pages_per_set = int(config.get("maxPagesPerSet", 50))
    sleep_seconds = float(config.get("catalogueRequestSleepSeconds", 0.15))
    written_files: list[tuple[str, str, Path]] = []
    failed_set_ids: list[str] = []

    for set_data in selected_sets:
        set_id = str(set_data.get("id"))
        set_name = str(set_data.get("name") or set_id)
        metrics["currentPriceEnSetsAttempted"] += 1
        print(f"  Fetching current prices for set {set_id} ({set_name})")

        try:
            cards, _total_cards, _pages = fetch_pokemon_tcg_paginated(
                "cards",
                base_params={"q": f"set.id:{set_id}"},
                page_size=page_size,
                max_pages=max_pages_per_set,
                sleep_seconds=sleep_seconds,
            )
        except (requests.RequestException, ValueError) as exc:
            print(f"  [WARN] Failed to build current prices for set {set_id}: {exc}")
            failed_set_ids.append(set_id)
            if not config.get("continueOnSetError", True):
                break
            continue

        prices: list[dict] = []
        for card in cards:
            prices.extend(extract_current_price_records(card, ts))

        prices.sort(key=price_sort_key)
        if not prices:
            metrics["currentPriceEnSkippedNoPriceSets"] += 1
        else:
            price_path = output_dir / f"{set_id}.json"
            payload = {
                "schemaVersion": SCHEMA_VERSION,
                "generatedAtUtc": ts,
                "game": "pokemon",
                "language": "en",
                "setId": set_id,
                "setName": set_name,
                "source": CURRENT_PRICE_SOURCE,
                "currency": CURRENT_PRICE_CURRENCY,
                "priceCount": len(prices),
                "prices": prices,
            }
            write_json(price_path, payload)
            written_files.append((set_id, set_name, price_path))
            metrics["currentPriceEnSetsWritten"] += 1
            metrics["currentPriceEnPriceRecordsWritten"] += len(prices)
            if fail_after_set_count > 0 and metrics["currentPriceEnSetsWritten"] >= fail_after_set_count:
                raise RuntimeError(
                    "Intentional failure for local safety testing via "
                    "CARDSCANR_FAIL_AFTER_EN_PRICE_SET_COUNT"
                )

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    if failed_set_ids:
        metrics["currentPriceEnStatus"] = "partial_built"
        failed_preview = ", ".join(failed_set_ids[:10])
        raise RuntimeError(
            "Failed to build EN current prices for one or more sets; "
            f"leaving existing public current-price cache untouched. sets={failed_preview}"
        )

    metrics["currentPriceEnStatus"] = "built"
    metrics["currentPriceEnBatchSetIds"] = selected_set_ids

    next_state = dict(refresh_state)
    next_state["schemaVersion"] = SCHEMA_VERSION
    next_state["enCurrentPriceCursor"] = cursor_after
    next_state["lastUpdatedAtUtc"] = ts
    next_state["lastBatchSetIds"] = selected_set_ids

    return written_files, metrics, next_state


def load_existing_current_price_files(language: str = "en") -> list[tuple[str, str, Path]]:
    current_dir = CURRENT_PRICES_EN_DIR if language == "en" else CURRENT_PRICES_JP_DIR
    if not current_dir.exists():
        return []

    files: list[tuple[str, str, Path]] = []
    for path in sorted(current_dir.glob("*.json")):
        payload = load_json(path)
        if not isinstance(payload, dict):
            continue
        set_id = str(payload.get("setId") or path.stem)
        set_name = str(payload.get("setName") or set_id)
        files.append((set_id, set_name, path))
    return files


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
            compacted = compact_price_info(variant_prices, "tcgdex_tcgplayer", "USD")
            if compacted:
                return compacted

    cardmarket_prices = pricing_root.get("cardmarket")
    if isinstance(cardmarket_prices, dict):
        variant_prices = extract_variant_prices(cardmarket_prices, card.get("variant", "normal"))
        if variant_prices:
            compacted = compact_price_info(variant_prices, "tcgdex_cardmarket", "EUR")
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

        return compact_price_info(variant_prices, "pokemon_tcg_api", "USD")
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
    mode = None
    if len(sys.argv) > 1:
        mode = sys.argv[1]
    mode = mode or os.getenv("CACHE_BUILD_MODE") or config.get("buildMode") or "scheduled"
    mode = str(mode).strip().lower().replace("-", "_")
    allowed_modes = {"scheduled", "current_prices", "full_catalogue", "tracked_history", "japanese_catalogue"}
    if mode not in allowed_modes:
        raise ValueError(f"Unsupported build mode '{mode}'. Expected one of {sorted(allowed_modes)}")
    return mode


def should_build_tracked_history(mode: str) -> bool:
    return mode in {"scheduled", "tracked_history", "full_catalogue"}


def should_build_current_prices(mode: str, config: dict) -> bool:
    return mode in {"scheduled", "current_prices", "full_catalogue"} and bool(
        config.get("buildCurrentPricesFromPokemonTcgApi", True)
    )


def should_build_full_catalogue(mode: str, config: dict) -> bool:
    if mode == "full_catalogue":
        return True
    if mode == "scheduled" and config.get("rebuildFullCatalogueOnScheduled", False):
        return True
    return False


def should_build_japanese_catalogue(mode: str, config: dict) -> bool:
    if mode == "japanese_catalogue":
        return bool(config.get("buildJapaneseFromTcgdex", True))
    if mode == "full_catalogue":
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
    ts = now_utc()
    day = ts[:10]
    catalog_config = load_catalog_config()
    mode = resolve_build_mode(catalog_config)
    refresh_state_path = resolve_state_path(catalog_config)
    refresh_state = load_scheduled_refresh_state(refresh_state_path)
    next_refresh_state: dict | None = None
    fail_after_en_count = parse_positive_int_env("CARDSCANR_FAIL_AFTER_EN_PRICE_SET_COUNT")
    reset_tmp_build_root(TMP_BUILD_ROOT)
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
    }

    cards_by_id: dict[str, dict] = {}
    latest_prices_by_id: dict[str, dict] = {}
    daily_history_files: list[tuple[str, str, Path]] = []
    sample_price_files: list[tuple[str, str, Path]] = []

    pending_json_writes: list[tuple[Path, dict]] = []

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

                    if price_info["source"] != "manual_seed":
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
            }
        diagnostics.update(current_price_metrics)

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

        diagnostics["sourcesUsed"] = sorted(diagnostics["sourcesUsed"])

        for path, payload in pending_json_writes:
            write_json(path, payload)

        write_json(API_MANIFEST_PATH, api_manifest)
        write_json(API_NOTES_PATH, api_notes)
        write_json(SCHEMAS_PATH, schemas)
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
