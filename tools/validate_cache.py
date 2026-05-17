#!/usr/bin/env python3
"""
validate_cache.py

Validates the static cache under public/v1/ before it is deployed to
Cloudflare Pages.

Checks performed
----------------
1. JSON syntax  – every .json file under public/ must parse without error.
2. Required fields  – index.json, price files, and diagnostics must contain
   the expected top-level keys.
3. Dataset URLs  – every dataset listed in index.json must exist on disk.
4. SHA-256 values  – the sha256 in index.json must match the actual file
   content on disk.
5. Duplicate canonicalId values  – each price file must not contain
   duplicate canonicalId entries.

Exit code
---------
0  all checks passed
1  one or more checks failed (details printed to stdout)
"""

import hashlib
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / "public"
V1_DIR = PUBLIC_DIR / "v1"
INDEX_PATH = V1_DIR / "index.json"

# ---------------------------------------------------------------------------
# Required fields per file type
# ---------------------------------------------------------------------------
REQUIRED_INDEX_FIELDS = {"schemaVersion", "generatedAtUtc", "cacheVersion", "datasets"}
REQUIRED_DATASET_FIELDS = {"id", "url", "sha256"}
REQUIRED_PRICE_FIELDS = {"schemaVersion", "generatedAtUtc", "game", "language", "prices"}
REQUIRED_CURRENT_PRICE_SET_FIELDS = REQUIRED_PRICE_FIELDS | {
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
}
REQUIRED_PRICE_ENTRY_FIELDS = {
    "canonicalId",
    "setId",
    "collectorNumber",
    "normalizedName",
    "variant",
    "condition",
    "currency",
    "source",
    "fetchedAtUtc",
}
REQUIRED_CURRENT_PRICE_ENTRY_FIELDS = REQUIRED_PRICE_ENTRY_FIELDS | {
    "nextExpectedPriceUpdateAtUtc",
    "staleness",
}
REQUIRED_DIAGNOSTICS_FIELDS = {
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
    "catalogueEnStatus",
    "catalogueEnFetchStrategy",
    "catalogueEnSetCount",
    "catalogueEnSetsAttempted",
    "catalogueEnSetsBuilt",
    "catalogueEnSetsFailed",
    "catalogueEnCardsFetched",
    "catalogueEnFailedSetIds",
    "catalogueEnStoppedReason",
    "currentPriceEnStatus",
    "currentPriceEnSetsAttempted",
    "currentPriceEnSetsWritten",
    "currentPriceEnPriceRecordsWritten",
    "currentPriceEnSkippedNoPriceSets",
    "currentPriceEnSource",
    "currentPriceEnCurrency",
}

OPTIONAL_JP_DIAGNOSTICS_FIELDS = {
    "catalogueJpStatus",
    "catalogueJpSourceStrategy",
    "catalogueJpProviderLanguage",
    "catalogueJpFetchStrategy",
    "catalogueJpSetCount",
    "catalogueJpSetsAttempted",
    "catalogueJpSetsBuilt",
    "catalogueJpSetsFailed",
    "catalogueJpCardsFetched",
    "catalogueJpCardsFromSetDetails",
    "catalogueJpCardsFromGlobalList",
    "catalogueJpCardsMergedTotal",
    "catalogueJpDuplicateCardsRemoved",
    "catalogueJpGlobalCardsFetched",
    "catalogueJpGlobalCardsGrouped",
    "catalogueJpGlobalCardsSkippedUnparseableId",
    "catalogueJpGlobalCardsSkippedUnknownSet",
    "catalogueJpCoverageImprovedByGlobalFallback",
    "catalogueJpSetsSkippedEmptyCards",
    "catalogueJpEmptySetIds",
    "catalogueJpFailedSetIds",
    "catalogueJpSkippedEmptySetIds",
    "catalogueJpStoppedReason",
    "catalogueJpEndpointExamples",
    "currentPriceJpStatus",
    "currentPriceJpSetsWritten",
    "currentPriceJpPriceRecordsWritten",
    "currentPriceJpSkippedNoPriceSets",
}
REQUIRED_POKEWALLET_PROBE_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "provider",
    "status",
    "apiKeyPresent",
    "requestsAttempted",
    "requestsSucceeded",
    "requestsFailed",
    "searchTermsTested",
    "totalResultsFound",
    "possibleJapaneseResults",
    "priceResultsFound",
    "sampleResults",
    "coverageSignals",
    "recommendation",
}
REQUIRED_POKEWALLET_COVERAGE_SIGNAL_FIELDS = {
    "hasJapaneseCards",
    "hasPrices",
    "hasImages",
    "hasSetCodes",
    "canMapToCanonicalId",
}
REQUIRED_POKEWALLET_SAMPLE_FIELDS = {
    "providerId",
    "name",
    "setName",
    "setCode",
    "number",
    "language",
    "imagePresent",
    "pricePresent",
    "currency",
    "rawKeys",
}
REQUIRED_POKEWALLET_JP_BUILD_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "provider",
    "mode",
    "apiKeyPresent",
    "requestsAttempted",
    "requestsSucceeded",
    "requestsFailed",
    "searchTargetsTested",
    "resultsFound",
    "possibleJapaneseResults",
    "confidentMatches",
    "lowConfidenceMatches",
    "unmappedResults",
    "priceRecordsWritten",
    "priceFilesWritten",
    "currenciesSeen",
    "catalogueCardsLoaded",
    "catalogueSampleTargetsBuilt",
    "catalogueSearchQueriesBuilt",
    "cataloguePreferredSetIdsUsed",
    "matchScoreDistribution",
    "skippedNoPrice",
    "skippedLowConfidence",
    "skippedNoCanonicalMatch",
    "skippedNoCurrency",
    "sampleSearchTargets",
    "sampleMatches",
    "sampleSkipped",
    "recommendation",
}
ALLOWED_POKEWALLET_PROBE_STATUSES = {
    "key_missing",
    "ok",
    "partial",
    "error",
    "endpoint_mapping_required",
    "disabled",
}

REQUIRED_TRACKED_CARDS_FIELDS = {"schemaVersion", "generatedAtUtc", "cards"}
REQUIRED_TRACKED_CARD_ENTRY_FIELDS = {
    "canonicalId",
    "firstTrackedAtUtc",
    "lastTrackedAtUtc",
    "firstTrackedPrice",
    "latestPrice",
    "trackingStats",
}
REQUIRED_DAILY_HISTORY_FIELDS = {"schemaVersion", "generatedAtUtc", "date", "game", "language", "prices"}
REQUIRED_API_MANIFEST_FIELDS = {
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
}
REQUIRED_API_NOTES_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "intendedConsumer",
    "publicDeveloperApi",
    "thirdPartyUseSupported",
    "notes",
}
REQUIRED_SCHEMAS_FIELDS = {"schemaVersion", "generatedAtUtc", "schemas"}
REQUIRED_PRICES_STATUS_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "cacheVersion",
    "status",
    "languages",
}
REQUIRED_LANGUAGE_STATUS_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "game",
    "language",
    "status",
    "staleness",
}
ALLOWED_PRICE_STATUS_VALUES = {
    "ok",
    "partial",
    "stale",
    "very_stale",
    "unavailable",
    "not_available",
    "catalogue_only",
}
ALLOWED_STALENESS_VALUES = {"fresh", "stale", "very_stale", "unavailable"}
ALLOWED_SET_PRICE_STATUS_VALUES = {"ok", "partial", "stale", "very_stale", "unavailable"}
REQUIRED_PLACEHOLDER_CATALOG_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "game",
    "language",
    "catalogueStatus",
    "cardsAvailable",
    "sets",
    "source",
    "notes",
}
REQUIRED_BUILT_CATALOG_FIELDS = REQUIRED_PLACEHOLDER_CATALOG_FIELDS | {
    "setCount",
    "cardCount",
    "partialSetCount",
    "failedSetCount",
    "failedSetIds",
}
REQUIRED_EN_CATALOG_FIELDS = REQUIRED_BUILT_CATALOG_FIELDS
REQUIRED_JP_CATALOG_FIELDS = REQUIRED_BUILT_CATALOG_FIELDS
REQUIRED_CATALOG_CARD_FIELDS = {
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
}
REQUIRED_EN_CATALOG_CARD_ENTRY_FIELDS = {
    "canonicalBaseId",
    "game",
    "language",
    "setId",
    "setName",
    "collectorNumber",
    "name",
    "normalizedName",
    "rarity",
    "supertype",
    "subtypes",
    "types",
    "hp",
    "artist",
    "imageSmall",
    "imageLarge",
    "imageSource",
    "imageCached",
    "externalIds",
    "availableVariants",
}
REQUIRED_JP_CATALOG_CARD_ENTRY_FIELDS = {
    "canonicalBaseId",
    "game",
    "language",
    "setId",
    "setName",
    "collectorNumber",
    "name",
    "normalizedName",
    "rarity",
    "category",
    "illustrator",
    "imageSmall",
    "imageLarge",
    "imageSource",
    "imageCached",
    "externalIds",
    "availableVariants",
}
REQUIRED_CATALOG_EXTERNAL_ID_FIELDS = {
    "pokemonTcgApiId",
    "tcgdexCardId",
    "tcgplayerProductId",
    "pricechartingId",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
errors: list[str] = []
warnings: list[str] = []
QUIET = False


def parse_bool_env(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def err(msg: str) -> None:
    errors.append(f"ERROR: {msg}")
    print(f"  [x] {msg}")


def warn(msg: str) -> None:
    warnings.append(f"WARNING: {msg}")
    print(f"  [!] {msg}")


def ok(msg: str) -> None:
    if not QUIET:
        print(f"  [ok] {msg}")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json_file(path: Path) -> object | None:
    """Load JSON; record an error and return None on failure."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        err(f"Invalid JSON in {path.relative_to(ROOT)}: {exc}")
        return None
    except OSError as exc:
        err(f"Cannot read {path.relative_to(ROOT)}: {exc}")
        return None


def check_required(data: dict, fields: set[str], label: str) -> bool:
    missing = fields - set(data.keys())
    if missing:
        err(f"{label} is missing required fields: {sorted(missing)}")
        return False
    return True


# ---------------------------------------------------------------------------
# Check 1: JSON syntax for every .json file
# ---------------------------------------------------------------------------
def check_all_json_syntax() -> None:
    print("\n[1] JSON syntax check")
    json_files = sorted(PUBLIC_DIR.rglob("*.json"))
    if not json_files:
        warn("No .json files found under public/")
        return
    for path in json_files:
        result = load_json_file(path)
        if result is not None:
            ok(str(path.relative_to(ROOT)))


# ---------------------------------------------------------------------------
# Check 2 + 3 + 4: index.json integrity
# ---------------------------------------------------------------------------
def check_index() -> list[dict]:
    """Returns the parsed list of dataset entries (may be empty on error)."""
    print("\n[2] index.json required fields")
    if not INDEX_PATH.exists():
        err(f"index.json not found at {INDEX_PATH.relative_to(ROOT)}")
        return []

    data = load_json_file(INDEX_PATH)
    if data is None:
        return []

    if not check_required(data, REQUIRED_INDEX_FIELDS, "index.json"):
        return []
    ok("index.json has all required top-level fields")

    # Add validation for cacheVersion, generatedAtUtc, and datasets
    if not data.get("cacheVersion"):
        err("index.json 'cacheVersion' must be present and non-empty")
    if not data.get("generatedAtUtc"):
        err("index.json 'generatedAtUtc' must be present")
    datasets = data.get("datasets", [])
    if not isinstance(datasets, list) or not datasets:
        err("index.json 'datasets' must be a non-empty list")
    seen_dataset_ids: set[str] = set()
    for ds in datasets:
        if not check_required(ds, REQUIRED_DATASET_FIELDS, f"dataset entry {ds.get('id', '?')}"):
            continue
        missing_meta = {"type", "description", "updatedAtUtc", "recommendedCacheTtlSeconds"} - set(ds.keys())
        if missing_meta:
            err(f"dataset entry {ds.get('id', '?')} is missing rich metadata fields: {sorted(missing_meta)}")
        ds_id = ds.get("id", "")
        if ds_id in seen_dataset_ids:
            err(f"index.json has duplicate dataset id: {ds_id}")
        elif ds_id:
            seen_dataset_ids.add(ds_id)

    required_dataset_ids = {
        "app_config",
        "api_manifest",
        "api_notes",
        "schemas",
        "diagnostics",
        "tracked_history",
        "prices_pokemon_en",
        "prices_pokemon_jp",
        "prices_status",
        "prices_current_pokemon_en_status",
        "prices_current_pokemon_jp_status",
        "catalog_pokemon_en_sets",
        "catalog_pokemon_jp_sets",
    }
    missing_ids = required_dataset_ids - seen_dataset_ids
    if missing_ids:
        err(f"index.json is missing required dataset ids: {sorted(missing_ids)}")

    print("\n[3] Dataset URL existence check")
    print("\n[4] SHA-256 integrity check")
    valid_datasets = []
    for ds in datasets:
        rel_url: str = ds["url"]
        # Convert URL path to local file path
        local_path = PUBLIC_DIR / rel_url.lstrip("/")

        if not local_path.exists():
            err(f"Dataset '{ds['id']}' URL {rel_url} does not exist on disk ({local_path.relative_to(ROOT)})")
            continue
        ok(f"Dataset '{ds['id']}' file exists: {local_path.relative_to(ROOT)}")

        actual_hash = sha256_file(local_path)
        expected_hash = ds["sha256"]
        if actual_hash != expected_hash:
            err(
                f"SHA-256 mismatch for '{ds['id']}':\n"
                f"    expected: {expected_hash}\n"
                f"    actual:   {actual_hash}"
            )
        else:
            ok(f"SHA-256 matches for '{ds['id']}'")

        valid_datasets.append(ds)

    return valid_datasets


# ---------------------------------------------------------------------------
# Check 5: Duplicate canonicalId within each price file
# ---------------------------------------------------------------------------
def check_price_files() -> None:
    print("\n[5] Price file required fields + duplicate canonicalId check")
    price_files = sorted((V1_DIR / "prices").rglob("*.json")) if (V1_DIR / "prices").exists() else []
    if not price_files:
        warn("No price files found under public/v1/prices/")
        return

    for path in price_files:
        rel = path.relative_to(ROOT)
        data = load_json_file(path)
        if data is None:
            continue

        parts = path.relative_to(V1_DIR).parts
        if parts == ("prices", "status.json") or (
            len(parts) == 5 and parts[:3] == ("prices", "current", "pokemon") and parts[4] == "status.json"
        ):
            continue
        is_current_set_file = len(parts) == 5 and parts[:3] == ("prices", "current", "pokemon")
        current_language = parts[3] if is_current_set_file else None
        required_top_fields = REQUIRED_CURRENT_PRICE_SET_FIELDS if is_current_set_file else REQUIRED_PRICE_FIELDS
        if not check_required(data, required_top_fields, str(rel)):
            continue
        if is_current_set_file:
            if current_language == "en":
                if data.get("source") != "pokemon_tcg_api":
                    err(f"{rel}: source must be pokemon_tcg_api")
                if data.get("currency") != "USD":
                    err(f"{rel}: currency must be USD")
            elif current_language == "jp":
                if not isinstance(data.get("source"), str) or not data.get("source"):
                    err(f"{rel}: source must be a non-empty string")
                currency = data.get("currency")
                if not isinstance(currency, str) or len(currency) != 3:
                    err(f"{rel}: currency must be a 3-letter string")

            if data.get("status") not in ALLOWED_SET_PRICE_STATUS_VALUES:
                err(f"{rel}: status must be one of {sorted(ALLOWED_SET_PRICE_STATUS_VALUES)}")
            if data.get("isLivePricing") is not False:
                err(f"{rel}: isLivePricing must be false")
            interval = data.get("expectedUpdateIntervalMinutes")
            if current_language == "en":
                if not isinstance(interval, int) or interval <= 0:
                    err(f"{rel}: expectedUpdateIntervalMinutes must be a positive integer for EN")
            elif current_language == "jp":
                if interval is not None and (not isinstance(interval, int) or interval <= 0):
                    err(f"{rel}: expectedUpdateIntervalMinutes must be null or a positive integer for JP")

            for ts_field in ["generatedAtUtc", "lastSuccessfulPriceUpdateAtUtc", "nextExpectedPriceUpdateAtUtc"]:
                ts_value = data.get(ts_field)
                if ts_value is not None and (not isinstance(ts_value, str) or not ts_value.endswith("Z")):
                    err(f"{rel}: {ts_field} must be a UTC string ending with 'Z' or null")

            set_staleness = data.get("staleness")
            if not isinstance(set_staleness, dict):
                err(f"{rel}: staleness must be an object")
            else:
                if set_staleness.get("status") not in ALLOWED_STALENESS_VALUES:
                    err(f"{rel}: staleness.status must be one of {sorted(ALLOWED_STALENESS_VALUES)}")
                age_value = set_staleness.get("ageSeconds")
                if age_value is not None and not (isinstance(age_value, int) and age_value >= 0):
                    err(f"{rel}: staleness.ageSeconds must be null or a non-negative integer")
                for number_field in ["freshForSeconds", "staleAfterSeconds"]:
                    number_value = set_staleness.get(number_field)
                    if not isinstance(number_value, int) or number_value <= 0:
                        err(f"{rel}: staleness.{number_field} must be a positive integer")

        prices = data.get("prices", [])
        if not isinstance(prices, list):
            err(f"{rel}: 'prices' must be a list")
            continue
        if is_current_set_file and data.get("priceCount") != len(prices):
            err(f"{rel}: priceCount must equal prices length")

        seen_ids: set[str] = set()
        dupes: list[str] = []
        entry_errors = 0
        for i, entry in enumerate(prices):
            if not isinstance(entry, dict):
                err(f"{rel}: prices[{i}] is not an object")
                entry_errors += 1
                continue
            required_entry_fields = REQUIRED_CURRENT_PRICE_ENTRY_FIELDS if is_current_set_file else REQUIRED_PRICE_ENTRY_FIELDS
            missing = required_entry_fields - set(entry.keys())
            if missing:
                err(f"{rel}: prices[{i}] missing fields: {sorted(missing)}")
                entry_errors += 1
            cid = entry.get("canonicalId", "")
            if cid in seen_ids:
                dupes.append(cid)
            else:
                seen_ids.add(cid)

            useful_price_found = False
            for field in ["marketPrice", "lowPrice", "highPrice"]:
                if field not in entry:
                    continue
                value = entry.get(field)
                if value is None:
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    useful_price_found = True
                else:
                    err(f"{rel}: prices[{i}] field '{field}' must be numeric or null")
                    entry_errors += 1
            if not useful_price_found:
                err(f"{rel}: prices[{i}] must include at least one numeric marketPrice, lowPrice, or highPrice")
                entry_errors += 1

            fetched_at = entry.get("fetchedAtUtc")
            if not isinstance(fetched_at, str) or not fetched_at.endswith("Z"):
                err(f"{rel}: prices[{i}] fetchedAtUtc must be a UTC string ending with 'Z'")
                entry_errors += 1

            if is_current_set_file:
                next_expected = entry.get("nextExpectedPriceUpdateAtUtc")
                if next_expected is not None and (not isinstance(next_expected, str) or not next_expected.endswith("Z")):
                    err(f"{rel}: prices[{i}] nextExpectedPriceUpdateAtUtc must be null or UTC 'Z' string")
                    entry_errors += 1
                record_currency = entry.get("currency")
                if not isinstance(record_currency, str) or len(record_currency) != 3 or record_currency.upper() != record_currency:
                    err(f"{rel}: prices[{i}] currency must be a 3-letter uppercase string")
                    entry_errors += 1
                if isinstance(data.get("currency"), str) and isinstance(record_currency, str) and data.get("currency") != record_currency:
                    err(f"{rel}: prices[{i}] currency must match top-level currency")
                    entry_errors += 1
                entry_staleness = entry.get("staleness")
                if not isinstance(entry_staleness, dict):
                    err(f"{rel}: prices[{i}] staleness must be an object")
                    entry_errors += 1
                else:
                    if entry_staleness.get("status") not in ALLOWED_STALENESS_VALUES:
                        err(
                            f"{rel}: prices[{i}] staleness.status must be one of {sorted(ALLOWED_STALENESS_VALUES)}"
                        )
                        entry_errors += 1
                    entry_age = entry_staleness.get("ageSeconds")
                    if entry_age is not None and not (isinstance(entry_age, int) and entry_age >= 0):
                        err(f"{rel}: prices[{i}] staleness.ageSeconds must be null or non-negative integer")
                        entry_errors += 1

                if current_language == "jp":
                    provider_ids = entry.get("providerIds")
                    if not isinstance(provider_ids, dict):
                        err(f"{rel}: prices[{i}] providerIds must be an object for JP entries")
                        entry_errors += 1
                    elif not isinstance(provider_ids.get("pokewalletId"), str) or not provider_ids.get("pokewalletId"):
                        err(f"{rel}: prices[{i}] providerIds.pokewalletId must be a non-empty string")
                        entry_errors += 1

                    match_confidence = entry.get("matchConfidence")
                    if not isinstance(match_confidence, (int, float)) or isinstance(match_confidence, bool):
                        err(f"{rel}: prices[{i}] matchConfidence must be numeric for JP entries")
                        entry_errors += 1
                    elif match_confidence < 0 or match_confidence > 1:
                        err(f"{rel}: prices[{i}] matchConfidence must be between 0 and 1")
                        entry_errors += 1

                    match_signals = entry.get("matchSignals")
                    if not isinstance(match_signals, list):
                        err(f"{rel}: prices[{i}] matchSignals must be a list for JP entries")
                        entry_errors += 1

        if dupes:
            err(f"{rel}: duplicate canonicalId values: {dupes}")
        elif entry_errors == 0:
            ok(f"{rel}: {len(prices)} entries, no duplicates, all fields present")


def check_price_status_files() -> None:
    print("\n[5b] Price status files check")
    prices_status_path = V1_DIR / "prices" / "status.json"
    en_status_path = V1_DIR / "prices" / "current" / "pokemon" / "en" / "status.json"
    jp_status_path = V1_DIR / "prices" / "current" / "pokemon" / "jp" / "status.json"

    for path in [prices_status_path, en_status_path, jp_status_path]:
        if not path.exists():
            err(f"Price status file not found: {path.relative_to(ROOT)}")
            return

    prices_status = load_json_file(prices_status_path)
    en_status = load_json_file(en_status_path)
    jp_status = load_json_file(jp_status_path)
    if not isinstance(prices_status, dict) or not isinstance(en_status, dict) or not isinstance(jp_status, dict):
        err("One or more price status files are not JSON objects")
        return

    if check_required(prices_status, REQUIRED_PRICES_STATUS_FIELDS, "prices/status.json"):
        ok("prices/status.json has required fields")

    top_level_status = prices_status.get("status")
    if top_level_status not in ALLOWED_PRICE_STATUS_VALUES:
        err(f"prices/status.json status must be one of {sorted(ALLOWED_PRICE_STATUS_VALUES)}")

    languages = prices_status.get("languages")
    if not isinstance(languages, dict):
        err("prices/status.json languages must be an object")
    else:
        if "en" not in languages or "jp" not in languages:
            err("prices/status.json languages must include en and jp")

    for label, payload, language in [
        ("prices/current/pokemon/en/status.json", en_status, "en"),
        ("prices/current/pokemon/jp/status.json", jp_status, "jp"),
    ]:
        if check_required(payload, REQUIRED_LANGUAGE_STATUS_FIELDS, label):
            ok(f"{label} has required fields")
        if payload.get("language") != language:
            err(f"{label} language must be {language}")

        status_value = payload.get("status")
        if status_value not in ALLOWED_PRICE_STATUS_VALUES:
            err(f"{label} status must be one of {sorted(ALLOWED_PRICE_STATUS_VALUES)}")

        staleness = payload.get("staleness")
        if not isinstance(staleness, dict):
            err(f"{label} staleness must be an object")
        else:
            staleness_status = staleness.get("status")
            if staleness_status not in ALLOWED_STALENESS_VALUES:
                err(f"{label} staleness.status must be one of {sorted(ALLOWED_STALENESS_VALUES)}")
            for num_field in ["ageSeconds", "freshForSeconds", "staleAfterSeconds"]:
                value = staleness.get(num_field)
                if value is not None and not (isinstance(value, int) and value >= 0):
                    err(f"{label} staleness.{num_field} must be null or a non-negative integer")

        for timestamp_field in [
            "generatedAtUtc",
            "lastSuccessfulPriceUpdateAtUtc",
            "lastSuccessfulPushAtUtc",
            "lastBatchStartedAtUtc",
            "lastBatchFinishedAtUtc",
            "nextExpectedPriceUpdateAtUtc",
        ]:
            if timestamp_field not in payload:
                continue
            ts_value = payload.get(timestamp_field)
            if ts_value is not None and not isinstance(ts_value, str):
                err(f"{label} {timestamp_field} must be a UTC string or null")
            if isinstance(ts_value, str) and not ts_value.endswith("Z"):
                err(f"{label} {timestamp_field} must end with 'Z' (UTC)")

        for num_field in [
            "currentPriceSetFileCount",
            "currentPriceRecordCount",
            "lastBatchSize",
            "lastBatchDurationSeconds",
            "expectedUpdateIntervalMinutes",
            "fullRotationEstimatedHours",
        ]:
            if num_field not in payload:
                continue
            num_value = payload.get(num_field)
            if num_value is not None and not (isinstance(num_value, int) and num_value >= 0):
                err(f"{label} {num_field} must be null or a non-negative integer")

    ok("Price status files validated")


# ---------------------------------------------------------------------------
# Check: diagnostics file
# ---------------------------------------------------------------------------
def check_diagnostics() -> None:
    print("\n[6] Diagnostics file check")
    diag_path = V1_DIR / "diagnostics" / "latest-build.json"
    if not diag_path.exists():
        warn(f"Diagnostics file not found: {diag_path.relative_to(ROOT)}")
        return
    data = load_json_file(diag_path)
    if data is None:
        return
    if check_required(data, REQUIRED_DIAGNOSTICS_FIELDS, "diagnostics/latest-build.json"):
        ok("diagnostics/latest-build.json has all required fields")
    if any(field in data for field in OPTIONAL_JP_DIAGNOSTICS_FIELDS):
        if check_required(data, OPTIONAL_JP_DIAGNOSTICS_FIELDS, "diagnostics/latest-build.json"):
            ok("diagnostics/latest-build.json has all JP diagnostics fields")


def check_provider_probe_diagnostics() -> None:
    print("\n[6b] Provider probe diagnostics check")
    path = V1_DIR / "diagnostics" / "pokewallet-probe-latest.json"
    if not path.exists():
        warn(f"PokéWallet probe diagnostics not found: {path.relative_to(ROOT)}")
        return
    data = load_json_file(path)
    if data is None or not isinstance(data, dict):
        err("diagnostics/pokewallet-probe-latest.json must be a JSON object")
        return
    if check_required(data, REQUIRED_POKEWALLET_PROBE_FIELDS, "diagnostics/pokewallet-probe-latest.json"):
        ok("diagnostics/pokewallet-probe-latest.json has all required fields")
    if data.get("provider") != "pokewallet":
        err("diagnostics/pokewallet-probe-latest.json provider must be pokewallet")
    if data.get("status") not in ALLOWED_POKEWALLET_PROBE_STATUSES:
        err(
            "diagnostics/pokewallet-probe-latest.json status must be one of "
            f"{sorted(ALLOWED_POKEWALLET_PROBE_STATUSES)}"
        )
    for field in [
        "requestsAttempted",
        "requestsSucceeded",
        "requestsFailed",
        "totalResultsFound",
        "possibleJapaneseResults",
        "priceResultsFound",
    ]:
        if not isinstance(data.get(field), int) or data.get(field) < 0:
            err(f"diagnostics/pokewallet-probe-latest.json {field} must be a non-negative integer")
    if not isinstance(data.get("searchTermsTested"), list):
        err("diagnostics/pokewallet-probe-latest.json searchTermsTested must be a list")
    signals = data.get("coverageSignals")
    if not isinstance(signals, dict):
        err("diagnostics/pokewallet-probe-latest.json coverageSignals must be an object")
    elif check_required(signals, REQUIRED_POKEWALLET_COVERAGE_SIGNAL_FIELDS, "diagnostics/pokewallet-probe-latest.json coverageSignals"):
        for key in REQUIRED_POKEWALLET_COVERAGE_SIGNAL_FIELDS:
            if not isinstance(signals.get(key), bool):
                err(f"diagnostics/pokewallet-probe-latest.json coverageSignals.{key} must be boolean")
    samples = data.get("sampleResults")
    if not isinstance(samples, list):
        err("diagnostics/pokewallet-probe-latest.json sampleResults must be a list")
        return
    for index, sample in enumerate(samples):
        label = f"diagnostics/pokewallet-probe-latest.json sampleResults[{index}]"
        if not isinstance(sample, dict):
            err(f"{label} must be an object")
            continue
        if check_required(sample, REQUIRED_POKEWALLET_SAMPLE_FIELDS, label):
            extra_keys = set(sample.keys()) - REQUIRED_POKEWALLET_SAMPLE_FIELDS
            if extra_keys:
                err(f"{label} includes unexpected fields: {sorted(extra_keys)}")
            if not isinstance(sample.get("imagePresent"), bool):
                err(f"{label} imagePresent must be boolean")
            if not isinstance(sample.get("pricePresent"), bool):
                err(f"{label} pricePresent must be boolean")
            if not isinstance(sample.get("rawKeys"), list):
                err(f"{label} rawKeys must be a list")
    ok(f"diagnostics/pokewallet-probe-latest.json: {len(samples)} sample result(s) validated")


def check_pokewallet_jp_build_diagnostics() -> None:
    print("\n[6c] Pokewallet JP build diagnostics check")
    path = V1_DIR / "diagnostics" / "pokewallet-jp-price-build-latest.json"
    if not path.exists():
        warn(f"Pokewallet JP build diagnostics not found: {path.relative_to(ROOT)}")
        return

    data = load_json_file(path)
    if data is None or not isinstance(data, dict):
        err("diagnostics/pokewallet-jp-price-build-latest.json must be a JSON object")
        return

    if check_required(data, REQUIRED_POKEWALLET_JP_BUILD_FIELDS, "diagnostics/pokewallet-jp-price-build-latest.json"):
        ok("diagnostics/pokewallet-jp-price-build-latest.json has required fields")

    if data.get("provider") != "pokewallet":
        err("diagnostics/pokewallet-jp-price-build-latest.json provider must be pokewallet")

    for field in [
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
        "catalogueCardsLoaded",
        "catalogueSampleTargetsBuilt",
        "catalogueSearchQueriesBuilt",
        "skippedNoPrice",
        "skippedLowConfidence",
        "skippedNoCanonicalMatch",
        "skippedNoCurrency",
    ]:
        value = data.get(field)
        if not isinstance(value, int) or value < 0:
            err(f"diagnostics/pokewallet-jp-price-build-latest.json {field} must be a non-negative integer")

    for field in [
        "searchTargetsTested",
        "currenciesSeen",
        "cataloguePreferredSetIdsUsed",
        "sampleSearchTargets",
        "sampleMatches",
        "sampleSkipped",
    ]:
        if not isinstance(data.get(field), list):
            err(f"diagnostics/pokewallet-jp-price-build-latest.json {field} must be a list")

    distribution = data.get("matchScoreDistribution")
    if not isinstance(distribution, dict):
        err("diagnostics/pokewallet-jp-price-build-latest.json matchScoreDistribution must be an object")
    else:
        required_buckets = {"0.90-1.00", "0.80-0.89", "0.70-0.79", "0.60-0.69", "0.00-0.59"}
        missing = required_buckets - set(distribution.keys())
        if missing:
            err(
                "diagnostics/pokewallet-jp-price-build-latest.json matchScoreDistribution missing buckets: "
                f"{sorted(missing)}"
            )
        for bucket, value in distribution.items():
            if not isinstance(value, int) or value < 0:
                err(
                    "diagnostics/pokewallet-jp-price-build-latest.json "
                    f"matchScoreDistribution.{bucket} must be a non-negative integer"
                )

    if not isinstance(data.get("apiKeyPresent"), bool):
        err("diagnostics/pokewallet-jp-price-build-latest.json apiKeyPresent must be boolean")
    if not isinstance(data.get("mode"), str) or not data.get("mode"):
        err("diagnostics/pokewallet-jp-price-build-latest.json mode must be a non-empty string")
    if not isinstance(data.get("recommendation"), str) or not data.get("recommendation"):
        err("diagnostics/pokewallet-jp-price-build-latest.json recommendation must be a non-empty string")


def check_api_manifest() -> None:
    print("\n[7] API manifest check")
    path = V1_DIR / "api-manifest.json"
    if not path.exists():
        err(f"API manifest file not found: {path.relative_to(ROOT)}")
        return
    data = load_json_file(path)
    if data is None or not isinstance(data, dict):
        err("api-manifest.json must be a JSON object")
        return
    if check_required(data, REQUIRED_API_MANIFEST_FIELDS, "api-manifest.json"):
        ok("api-manifest.json has all required fields")
    if data.get("intendedConsumer") != "cardscanr_app":
        err("api-manifest.json intendedConsumer must be cardscanr_app")
    if data.get("publicDeveloperApi") is not False:
        err("api-manifest.json publicDeveloperApi must be false")
    if data.get("thirdPartyUseSupported") is not False:
        err("api-manifest.json thirdPartyUseSupported must be false")
    endpoints = data.get("endpoints", [])
    if not isinstance(endpoints, list) or not endpoints:
        err("api-manifest.json endpoints must be a non-empty list")


def check_api_notes() -> None:
    print("\n[8] API notes check")
    path = V1_DIR / "api-notes.json"
    if not path.exists():
        err(f"API notes file not found: {path.relative_to(ROOT)}")
        return
    data = load_json_file(path)
    if data is None or not isinstance(data, dict):
        err("api-notes.json must be a JSON object")
        return
    if check_required(data, REQUIRED_API_NOTES_FIELDS, "api-notes.json"):
        ok("api-notes.json has all required fields")
    if data.get("publicDeveloperApi") is not False:
        err("api-notes.json publicDeveloperApi must be false")
    if data.get("thirdPartyUseSupported") is not False:
        err("api-notes.json thirdPartyUseSupported must be false")
    notes = data.get("notes", [])
    if not isinstance(notes, list) or not notes:
        err("api-notes.json notes must be a non-empty list")


def check_schemas() -> None:
    print("\n[9] Schema docs check")
    path = V1_DIR / "schemas.json"
    if not path.exists():
        err(f"Schemas file not found: {path.relative_to(ROOT)}")
        return
    data = load_json_file(path)
    if data is None or not isinstance(data, dict):
        err("schemas.json must be a JSON object")
        return
    if check_required(data, REQUIRED_SCHEMAS_FIELDS, "schemas.json"):
        ok("schemas.json has all required top-level fields")
    schemas = data.get("schemas", {})
    if not isinstance(schemas, dict) or not schemas:
        err("schemas.json schemas must be a non-empty object")


def check_catalogues() -> None:
    print("\n[10] Catalogue check")
    catalog_files = sorted((V1_DIR / "catalog").rglob("sets.json")) if (V1_DIR / "catalog").exists() else []
    if not catalog_files:
        warn("No catalogue files found under public/v1/catalog/")
        return
    for path in catalog_files:
        rel = path.relative_to(ROOT)
        data = load_json_file(path)
        if data is None or not isinstance(data, dict):
            err(f"{rel} must be a JSON object")
            continue

        is_pokemon_catalogue = data.get("game") == "pokemon" and data.get("language") in {"en", "jp"}
        if is_pokemon_catalogue and data.get("language") == "en":
            if check_required(data, REQUIRED_EN_CATALOG_FIELDS, str(rel)):
                ok(f"{rel} has required EN catalogue fields")
            if data.get("catalogueStatus") not in {"built", "partial_built", "not_built_yet"}:
                err(f"{rel} catalogueStatus must be built, partial_built, or not_built_yet")
            if data.get("catalogueStatus") in {"built", "partial_built"}:
                if data.get("source") != "pokemon_tcg_api":
                    err(f"{rel} source must be pokemon_tcg_api when built")
                if data.get("cardsAvailable") is not True:
                    err(f"{rel} cardsAvailable must be true when EN catalogue card files exist")
            if data.get("setCount") != len(data.get("sets", [])):
                err(f"{rel} setCount must equal sets length")
            failed_set_ids = data.get("failedSetIds", [])
            if not isinstance(failed_set_ids, list):
                err(f"{rel} failedSetIds must be a list")
            elif data.get("failedSetCount") != len(failed_set_ids):
                err(f"{rel} failedSetCount must equal failedSetIds length")
            if data.get("catalogueStatus") == "built" and data.get("failedSetCount") != 0:
                err(f"{rel} built catalogue must not have failed sets")
        elif is_pokemon_catalogue and data.get("language") == "jp":
            if data.get("catalogueStatus") in {"built", "partial_built"}:
                if check_required(data, REQUIRED_JP_CATALOG_FIELDS, str(rel)):
                    ok(f"{rel} has required JP catalogue fields")
                if data.get("source") != "tcgdex":
                    err(f"{rel} source must be tcgdex when JP catalogue is built")
                if data.get("cardsAvailable") is not True:
                    err(f"{rel} cardsAvailable must be true when JP catalogue card files exist")
                if data.get("setCount") != len(data.get("sets", [])):
                    err(f"{rel} setCount must equal sets length")
                failed_set_ids = data.get("failedSetIds", [])
                if not isinstance(failed_set_ids, list):
                    err(f"{rel} failedSetIds must be a list")
                elif data.get("failedSetCount") != len(failed_set_ids):
                    err(f"{rel} failedSetCount must equal failedSetIds length")
                if data.get("catalogueStatus") == "built" and data.get("failedSetCount") != 0:
                    err(f"{rel} built catalogue must not have failed sets")
            else:
                if check_required(data, REQUIRED_PLACEHOLDER_CATALOG_FIELDS, str(rel)):
                    ok(f"{rel} has required placeholder fields")
                if data.get("catalogueStatus") != "not_built_yet":
                    err(f"{rel} catalogueStatus must be not_built_yet for the placeholder catalogue")
                if data.get("cardsAvailable") is not False:
                    err(f"{rel} cardsAvailable must be false for the placeholder catalogue")
        else:
            if check_required(data, REQUIRED_PLACEHOLDER_CATALOG_FIELDS, str(rel)):
                ok(f"{rel} has required placeholder fields")
            if data.get("catalogueStatus") != "not_built_yet":
                err(f"{rel} catalogueStatus must be not_built_yet for the placeholder catalogue")
            if data.get("cardsAvailable") is not False:
                err(f"{rel} cardsAvailable must be false for the placeholder catalogue")

        sets = data.get("sets", [])
        if not isinstance(sets, list):
            err(f"{rel} sets must be a list")

    check_catalog_card_files()


def check_catalog_card_files() -> None:
    for language, expected_source, expected_image_source in [
        ("en", "pokemon_tcg_api", "pokemon_tcg_api"),
        ("jp", "tcgdex", "tcgdex"),
    ]:
        cards_dir = V1_DIR / "catalog" / "pokemon" / language / "cards"
        card_files = sorted(cards_dir.glob("*.json")) if cards_dir.exists() else []
        if not card_files:
            warn(f"No {language.upper()} catalogue card files found under {cards_dir.relative_to(ROOT)}")
            continue

        required_entry_fields = (
            REQUIRED_EN_CATALOG_CARD_ENTRY_FIELDS if language == "en" else REQUIRED_JP_CATALOG_CARD_ENTRY_FIELDS
        )

        for path in card_files:
            rel = path.relative_to(ROOT)
            data = load_json_file(path)
            if data is None or not isinstance(data, dict):
                err(f"{rel} must be a JSON object")
                continue
            if not check_required(data, REQUIRED_CATALOG_CARD_FIELDS, str(rel)):
                continue
            if data.get("game") != "pokemon":
                err(f"{rel} game must be pokemon")
            if data.get("language") != language:
                err(f"{rel} language must be {language}")
            if data.get("source") != expected_source:
                err(f"{rel} source must be {expected_source}")
            if data.get("catalogueStatus") != "built":
                err(f"{rel} catalogueStatus must be built")

            cards = data.get("cards", [])
            if not isinstance(cards, list):
                err(f"{rel} cards must be a list")
                continue
            if data.get("cardCount") != len(cards):
                err(f"{rel} cardCount must equal cards length")

            seen_base_ids: set[str] = set()
            seen_tcgdex_ids: set[str] = set()
            entry_errors = 0
            for i, card in enumerate(cards):
                label = f"{rel} cards[{i}]"
                if not isinstance(card, dict):
                    err(f"{label} is not an object")
                    entry_errors += 1
                    continue
                missing = required_entry_fields - set(card.keys())
                if missing:
                    err(f"{label} missing fields: {sorted(missing)}")
                    entry_errors += 1
                canonical_base_id = card.get("canonicalBaseId")
                if canonical_base_id in seen_base_ids:
                    err(f"{rel}: duplicate canonicalBaseId: {canonical_base_id}")
                    entry_errors += 1
                elif canonical_base_id:
                    seen_base_ids.add(canonical_base_id)
                if card.get("imageCached") is not False:
                    err(f"{label} imageCached must be false")
                    entry_errors += 1
                if "imageSmall" not in card or "imageLarge" not in card:
                    err(f"{label} imageSmall and imageLarge fields must exist")
                    entry_errors += 1
                if card.get("imageSource") != expected_image_source:
                    err(f"{label} imageSource must be {expected_image_source}")
                    entry_errors += 1
                external_ids = card.get("externalIds")
                if not isinstance(external_ids, dict):
                    err(f"{label} externalIds must be an object")
                    entry_errors += 1
                else:
                    missing_external = REQUIRED_CATALOG_EXTERNAL_ID_FIELDS - set(external_ids.keys())
                    if missing_external:
                        err(f"{label} externalIds missing fields: {sorted(missing_external)}")
                        entry_errors += 1
                    if language == "jp":
                        tcgdex_id = external_ids.get("tcgdexCardId")
                        if not isinstance(tcgdex_id, str) or not tcgdex_id:
                            err(f"{label} externalIds.tcgdexCardId must be a non-empty string for JP")
                            entry_errors += 1
                        elif tcgdex_id in seen_tcgdex_ids:
                            err(f"{rel}: duplicate tcgdexCardId: {tcgdex_id}")
                            entry_errors += 1
                        else:
                            seen_tcgdex_ids.add(tcgdex_id)
                if language == "en":
                    if not isinstance(card.get("subtypes"), list):
                        err(f"{label} subtypes must be a list")
                        entry_errors += 1
                    if not isinstance(card.get("types"), list):
                        err(f"{label} types must be a list")
                        entry_errors += 1
                if not isinstance(card.get("availableVariants"), list):
                    err(f"{label} availableVariants must be a list")
                    entry_errors += 1

            if entry_errors == 0:
                ok(f"{rel}: {len(cards)} catalogue cards validated")


# ---------------------------------------------------------------------------
# Check: tracked cards history and daily history files
# ---------------------------------------------------------------------------
def check_history() -> None:
    print("\n[11] Tracked history check")
    history_root = V1_DIR / "history"
    tracked_cards_path = history_root / "tracked-cards.json"

    if not tracked_cards_path.exists():
        err(f"Tracked cards file not found: {tracked_cards_path.relative_to(ROOT)}")
        return

    tracked_data = load_json_file(tracked_cards_path)
    if tracked_data is None or not isinstance(tracked_data, dict):
        err("history/tracked-cards.json must be a JSON object")
        return

    if check_required(tracked_data, REQUIRED_TRACKED_CARDS_FIELDS, "history/tracked-cards.json"):
        ok("history/tracked-cards.json has required top-level fields")

    tracked_cards = tracked_data.get("cards", [])
    if not isinstance(tracked_cards, list):
        err("history/tracked-cards.json 'cards' must be a list")
        tracked_cards = []

    seen_tracked_ids: set[str] = set()
    for i, entry in enumerate(tracked_cards):
        label = f"history/tracked-cards.json cards[{i}]"
        if not isinstance(entry, dict):
            err(f"{label} is not an object")
            continue
        missing = REQUIRED_TRACKED_CARD_ENTRY_FIELDS - set(entry.keys())
        if missing:
            err(f"{label} missing fields: {sorted(missing)}")
        cid = entry.get("canonicalId", "")
        if cid in seen_tracked_ids:
            err(f"history/tracked-cards.json duplicate canonicalId: {cid}")
        elif cid:
            seen_tracked_ids.add(cid)

        latest_price = entry.get("latestPrice")
        if latest_price is not None and not isinstance(latest_price, dict):
            err(f"{label} latestPrice must be an object when present")
        first_price = entry.get("firstTrackedPrice")
        if first_price is not None and not isinstance(first_price, dict):
            err(f"{label} firstTrackedPrice must be an object when present")
        tracking_stats = entry.get("trackingStats")
        if tracking_stats is not None and not isinstance(tracking_stats, dict):
            err(f"{label} trackingStats must be an object when present")

    daily_root = history_root / "daily"
    if not daily_root.exists():
        warn("No daily history directory found under public/v1/history/daily")
        return

    daily_files = sorted(daily_root.rglob("tracked.json"))
    if not daily_files:
        warn("No daily tracked history files found")
        return

    for path in daily_files:
        rel = path.relative_to(ROOT)
        data = load_json_file(path)
        if data is None or not isinstance(data, dict):
            err(f"{rel} must be a JSON object")
            continue

        if not check_required(data, REQUIRED_DAILY_HISTORY_FIELDS, str(rel)):
            continue

        prices = data.get("prices", [])
        if not isinstance(prices, list):
            err(f"{rel}: 'prices' must be a list")
            continue

        seen_daily_ids: set[str] = set()
        for i, entry in enumerate(prices):
            if not isinstance(entry, dict):
                err(f"{rel}: prices[{i}] is not an object")
                continue

            cid = entry.get("canonicalId")
            if not cid:
                err(f"{rel}: prices[{i}] missing canonicalId")
                continue
            if cid in seen_daily_ids:
                err(f"{rel}: duplicate canonicalId values include '{cid}'")
            else:
                seen_daily_ids.add(cid)

            # Price fields are validated when available in history snapshots.
            for field in ["currency", "marketPrice", "source", "fetchedAtUtc"]:
                if field in entry and entry.get(field) in (None, ""):
                    err(f"{rel}: prices[{i}] field '{field}' is present but empty")

        ok(f"{rel}: {len(prices)} tracked snapshots validated")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    global QUIET
    QUIET = "--quiet" in sys.argv[1:] or parse_bool_env("CARDSCANR_VALIDATE_QUIET")

    print("=" * 60)
    print("CardScanR cache validation")
    if QUIET:
        print("Mode: quiet")
    print("=" * 60)

    check_all_json_syntax()
    check_index()
    check_price_files()
    check_price_status_files()
    check_diagnostics()
    check_provider_probe_diagnostics()
    check_pokewallet_jp_build_diagnostics()
    check_api_manifest()
    check_api_notes()
    check_schemas()
    check_catalogues()
    check_history()

    print("\n" + "=" * 60)
    if errors:
        print(f"FAILED – {len(errors)} error(s) found:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        if warnings:
            print(f"PASSED with {len(warnings)} warning(s):")
            for w in warnings:
                print(f"  {w}")
        else:
            print("PASSED – all checks succeeded.")
        sys.exit(0)


if __name__ == "__main__":
    main()
