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
import sys
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
DIAGNOSTICS_DIR = PUBLIC_DIR / "diagnostics"
HISTORY_ROOT_DIR = PUBLIC_DIR / "history"
HISTORY_DIR = HISTORY_ROOT_DIR / "daily"
TRACKED_CARDS_PATH = HISTORY_ROOT_DIR / "tracked-cards.json"
CATALOG_DIR = PUBLIC_DIR / "catalog"
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
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    canonical = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def load_json(path: Path) -> object:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


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
            "Tracked history means history since CardScanR started tracking the card.",
            "Lifetime/all-time market history is not currently provided.",
            "Images are referenced by URL and are not mirrored into this cache yet.",
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
                "notes": ["Placeholder file until full catalogue cache generation is implemented."],
            },
            "catalogue_cards_file": {
                "requiredFields": ["schemaVersion", "generatedAtUtc", "game", "language", "cards"],
                "notes": [
                    "When implemented, catalogue card records should store image URLs only.",
                    "Use imageSmall, imageLarge, imageSource, and imageCached: false.",
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
                "notes": ["Current tracked price cache entry."],
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


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------
def build() -> None:
    ts = now_utc()
    day = ts[:10]
    print(f"[build_price_cache] Starting build at {ts}")

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
    }

    cards_by_id: dict[str, dict] = {}
    latest_prices_by_id: dict[str, dict] = {}
    daily_history_files: list[tuple[str, str, Path]] = []
    current_price_files: list[tuple[str, str, Path]] = []

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
        write_json(price_path, payload)

        history_payload = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAtUtc": ts,
            "date": day,
            "game": game,
            "language": language,
            "prices": prices,
        }
        write_json(history_path, history_payload)
        diagnostics["dailyHistoryFilesWritten"] += 1
        daily_history_files.append((game, language, history_path))
        current_price_files.append((game, language, price_path))

        digest = sha256_file(price_path)
        print(f"  Wrote {price_path}  sha256={digest}")
        print(f"  Wrote {history_path}")

    tracked_payload, first_created, tracked_updated = update_tracked_cards_history(
        ts=ts,
        cards_by_id=cards_by_id,
        latest_prices_by_id=latest_prices_by_id,
    )
    diagnostics["trackedHistoryWritten"] = True
    diagnostics["trackedCardsTotal"] = len(tracked_payload.get("cards", []))
    diagnostics["firstTrackedCreatedCount"] = first_created
    diagnostics["trackedCardsUpdatedCount"] = tracked_updated

    api_manifest = build_api_manifest(ts)
    api_notes = build_api_notes(ts)
    schemas = build_schemas(ts)
    catalog_en = build_catalog_sets_placeholder("pokemon", "en", ts)
    catalog_jp = build_catalog_sets_placeholder("pokemon", "jp", ts)

    diagnostics["sourcesUsed"] = sorted(diagnostics["sourcesUsed"])

    write_json(API_MANIFEST_PATH, api_manifest)
    write_json(API_NOTES_PATH, api_notes)
    write_json(SCHEMAS_PATH, schemas)
    write_json(CATALOG_DIR / "pokemon" / "en" / "sets.json", catalog_en)
    write_json(CATALOG_DIR / "pokemon" / "jp" / "sets.json", catalog_jp)
    write_json(TRACKED_CARDS_PATH, tracked_payload)

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
    for game, language, price_path in current_price_files:
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
        index_entries.append(
            build_index_dataset_entry(
                dataset_id=dataset_id,
                file_path=catalog_path,
                dataset_type="catalogue_sets",
                description=f"{game.capitalize()} TCG {language.upper()} catalogue sets placeholder",
                ts=ts,
                ttl_seconds=CATALOG_CACHE_TTL_SECONDS,
                game=game,
                language=language,
            )
        )

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
    print(f"  Updated {INDEX_PATH}")
    print(f"  Updated {TRACKED_CARDS_PATH}")
    print(f"  Updated {DIAG_PATH}")

    print("[build_price_cache] Build complete.")


if __name__ == "__main__":
    build()
