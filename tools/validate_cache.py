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
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / "public"
V1_DIR = PUBLIC_DIR / "v1"
INDEX_PATH = V1_DIR / "index.json"
SUPPORTED_SOURCES_PATH = V1_DIR / "supported-sources.json"

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
    "canonicalCardId",
    "priceIdentityId",
    "market",
    "country",
    "sourceCurrency",
    "targetCurrency",
    "conversionPolicy",
    "status",
    "confidence",
    "diagnostics",
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
    "cardscanrJpSetsLoaded",
    "catalogueSampleTargetsBuilt",
    "catalogueSearchQueriesBuilt",
    "cataloguePreferredSetIdsUsed",
    "pokewalletSetsFetched",
    "pokewalletJapaneseLikeSets",
    "pokewalletSetLanguagesSeen",
    "samplePokewalletSets",
    "setMatchCandidatesBuilt",
    "pokewalletSetDetailsAttempted",
    "pokewalletSetDetailsSucceeded",
    "pokewalletCardsFetchedFromSetDetails",
    "sampleSetDetailCards",
    "searchFallbackRequestsAttempted",
    "searchFallbackResultsFound",
    "sampleSearchQueries",
    "matchScoreDistribution",
    "skippedNoPrice",
    "skippedLowConfidence",
    "skippedNoCanonicalMatch",
    "skippedNoCurrency",
    "sampleSetMatches",
    "sampleUnmatchedCardScanRSets",
    "sampleUnmatchedPokewalletSets",
    "sampleSearchTargets",
    "sampleMatches",
    "sampleSkipped",
    "blockerReason",
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
REQUIRED_POKEWALLET_PRO_PRICE_PROBE_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "provider",
    "mode",
    "status",
    "apiKeyPresent",
    "proEndpointUsed",
    "requestsAttempted",
    "requestsSucceeded",
    "requestsFailed",
    "setsFetched",
    "languagesSeen",
    "setsSelectedByLanguage",
    "priceResponsesByLanguage",
    "priceRecordsFoundByLanguage",
    "currenciesSeen",
    "sourcesSeen",
    "samplePriceRecords",
    "sampleSkipped",
    "recommendation",
}
ALLOWED_POKEWALLET_PRO_PRICE_PROBE_STATUSES = {
    "dry_run",
    "ok",
    "key_missing",
    "pro_required",
    "error",
}
REQUIRED_POKEWALLET_PRO_TRIAL_DISCOVERY_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "provider",
    "mode",
    "status",
    "apiKeyPresent",
    "requestsAttempted",
    "requestsSucceeded",
    "requestsFailed",
    "setsFetched",
    "languagesSeen",
    "setsByLanguage",
    "sampleSetsByLanguage",
    "setsSelectedTotal",
    "setsProcessedThisRun",
    "setsRemainingAfterRun",
    "endpointCoverage",
    "priceRecordsFoundByLanguage",
    "currenciesSeen",
    "sourcesSeen",
    "imageSamplesChecked",
    "imageSamplesAvailable",
    "priceHistorySamplesChecked",
    "priceHistorySamplesWithData",
    "rateLimit",
    "rateSafety",
    "samplePriceRecords",
    "sampleTrendingRecords",
    "sampleTopCards",
    "sampleImageChecks",
    "sampleSkipped",
    "diagnosticEvents",
    "recommendation",
}
ALLOWED_POKEWALLET_PRO_TRIAL_DISCOVERY_STATUSES = {
    "dry_run",
    "ok",
    "partial",
    "key_missing",
    "pro_required",
    "rate_limited",
    "stopped_rate_limit_safety",
    "error",
}
REQUIRED_POKEWALLET_PRO_TRIAL_STATE_FIELDS = {
    "schemaVersion",
    "updatedAtUtc",
    "mode",
    "completedSetKeys",
    "failedSetKeys",
    "skippedSetKeys",
    "completedEndpointKeys",
    "lastProcessedSetKey",
    "requestsAttemptedTotal",
    "requestsSucceededTotal",
    "requestsFailedTotal",
    "priceRecordsFoundTotal",
    "imageSamplesCheckedTotal",
    "priceHistorySamplesCheckedTotal",
    "languagesCompleted",
    "lastRunId",
}
REQUIRED_POKEWALLET_CATALOG_FULL_STATE_FIELDS = {
    "schemaVersion",
    "updatedAtUtc",
    "mode",
    "completedSetKeys",
    "failedSetKeys",
    "skippedSetKeys",
    "lastProcessedSetKey",
    "requestsAttemptedTotal",
    "requestsSucceededTotal",
    "requestsFailedTotal",
    "cardsWrittenTotal",
    "languagesCompleted",
    "lastRunId",
}
REQUIRED_POKEWALLET_CATALOG_FOUNDATION_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "provider",
    "mode",
    "status",
    "apiKeyPresent",
    "fullCatalogueEnabled",
    "requestsAttempted",
    "requestsSucceeded",
    "requestsFailed",
    "setsFetched",
    "setsProcessedThisRun",
    "setsRemainingAfterRun",
    "cardsWrittenThisRun",
    "cardsWrittenByLanguage",
    "setFilesWritten",
    "languagesSeen",
    "setsSelectedByLanguage",
    "cardsFetchedByLanguage",
    "imageSamplesChecked",
    "imageSamplesAvailable",
    "sampleCards",
    "sampleSkipped",
    "blockerReason",
    "recommendation",
}
ALLOWED_POKEWALLET_CATALOG_FOUNDATION_STATUSES = {
    "dry_run",
    "ok",
    "partial",
    "key_missing",
    "rate_limited",
    "error",
}
REQUIRED_PROVIDER_CATALOG_TOP_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "provider",
    "game",
    "notes",
}
REQUIRED_PROVIDER_CATALOG_STATUS_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "provider",
    "game",
    "status",
    "binaryImagesStored",
    "imageStorageMode",
    "catalogueType",
    "languages",
    "notes",
}
REQUIRED_PROVIDER_CATALOG_CARDS_MANIFEST_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "provider",
    "game",
    "status",
    "totalSetFiles",
    "totalCards",
    "languages",
}
REQUIRED_PROVIDER_CATALOG_MANIFEST_SET_FILE_FIELDS = {
    "providerSetId",
    "providerSetCode",
    "providerSetName",
    "cardScanRLanguage",
    "cardCount",
    "url",
    "sha256",
    "updatedAtUtc",
}
REQUIRED_POKEWALLET_PROVIDER_CARD_FIELDS = {
    "providerCardId",
    "providerSetId",
    "providerSetCode",
    "providerSetName",
    "providerLanguage",
    "cardScanRLanguage",
    "name",
    "cleanName",
    "cardNumber",
    "rarity",
    "variants",
    "providerCanonicalImageKey",
    "cardScanRImageCacheCandidateKey",
    "canonicalImageKey",
    "imageCacheKey",
    "imageCacheIdentityBasis",
    "imageEndpoint",
    "imageEndpointLow",
    "imageEndpointHigh",
    "imageAvailable",
    "imageLowAvailable",
    "imageHighAvailable",
    "imageLastCheckedAtUtc",
    "imageCacheStrategy",
    "hasPriceFields",
    "hasTcgplayerFields",
    "hasCardmarketFields",
    "rawKeys",
}
REQUIRED_POKEWALLET_PROVIDER_SET_FILE_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "provider",
    "game",
    "providerLanguage",
    "cardScanRLanguage",
    "providerSetId",
    "providerSetCode",
    "providerSetName",
    "cardCount",
    "imageReferencesOnly",
    "cards",
}
REQUIRED_POKEWALLET_PROVIDER_SET_CARD_FIELDS = REQUIRED_POKEWALLET_PROVIDER_CARD_FIELDS | {
    "imageEndpointLow",
    "imageEndpointHigh",
}
REQUIRED_IMAGE_CACHE_POLICY_FIELDS = {
    "schemaVersion",
    "generatedAtUtc",
    "status",
    "binaryImagesStored",
    "imageStorageMode",
    "recommendedFutureStorage",
    "cacheKeyRule",
    "defaultPolicy",
    "notes",
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
REQUIRED_SUPPORTED_SOURCES_FIELDS = {"sources"}
REQUIRED_SUPPORTED_SOURCE_ENTRY_FIELDS = {"id", "aliases", "description", "enabled"}
CANONICAL_SUPPORTED_SOURCE_IDS = {
    "pokemon_tcg_api",
    "tcgdex",
    "tcgdex_tcgplayer",
    "tcgdex_cardmarket",
    "pokewallet",
    "ebay_sold_manual",
    "manual",
    "manual_seed",
    "unavailable",
}
REQUIRED_SUPPORTED_SOURCE_ALIASES = {
    "pokemon_tcg_api": {"pokemonTcgApi"},
    "ebay_sold_manual": {"ebaySoldListingsManual"},
}
LEGACY_PRIMARY_SOURCE_IDS = {"pokemonTcgApi", "ebaySoldListingsManual"}
SNAKE_CASE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
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
ALLOWED_RECORD_PRICE_STATUS_VALUES = {
    "priced",
    "no_result",
    "not_configured",
    "rate_limited",
    "network_error",
    "provider_error",
    "stale",
    "unavailable",
    "disabled",
}
ALLOWED_CONVERSION_POLICY_VALUES = {"none", "converted", "unavailable"}
ALLOWED_CONFIDENCE_VALUES = {"high", "medium", "low", "unknown"}
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


def is_lower_snake_case(value: str) -> bool:
    return bool(SNAKE_CASE_PATTERN.fullmatch(value))


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
                if not isinstance(currency, str) or (currency != "mixed" and len(currency) != 3):
                    err(f"{rel}: currency must be a 3-letter string or mixed")

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
                if (
                    isinstance(data.get("currency"), str)
                    and data.get("currency") != "mixed"
                    and isinstance(record_currency, str)
                    and data.get("currency") != record_currency
                ):
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
                    elif not isinstance(provider_ids.get("pokewalletSetId"), str) or not provider_ids.get("pokewalletSetId"):
                        err(f"{rel}: prices[{i}] providerIds.pokewalletSetId must be a non-empty string")
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
                elif current_language == "en":
                    market = entry.get("market")
                    if not isinstance(market, str) or not market or market != market.lower():
                        err(f"{rel}: prices[{i}] market must be a non-empty lowercase string")
                        entry_errors += 1

                    country = entry.get("country")
                    if not isinstance(country, str) or len(country) != 2 or country != country.upper():
                        err(f"{rel}: prices[{i}] country must be a 2-letter uppercase string")
                        entry_errors += 1

                    for field in ["sourceCurrency", "targetCurrency"]:
                        code = entry.get(field)
                        if not isinstance(code, str) or len(code) != 3 or code != code.upper():
                            err(f"{rel}: prices[{i}] {field} must be a 3-letter uppercase string")
                            entry_errors += 1

                    if entry.get("conversionPolicy") not in ALLOWED_CONVERSION_POLICY_VALUES:
                        err(
                            f"{rel}: prices[{i}] conversionPolicy must be one of "
                            f"{sorted(ALLOWED_CONVERSION_POLICY_VALUES)}"
                        )
                        entry_errors += 1

                    if entry.get("status") not in ALLOWED_RECORD_PRICE_STATUS_VALUES:
                        err(
                            f"{rel}: prices[{i}] status must be one of "
                            f"{sorted(ALLOWED_RECORD_PRICE_STATUS_VALUES)}"
                        )
                        entry_errors += 1

                    if entry.get("confidence") not in ALLOWED_CONFIDENCE_VALUES:
                        err(
                            f"{rel}: prices[{i}] confidence must be one of "
                            f"{sorted(ALLOWED_CONFIDENCE_VALUES)}"
                        )
                        entry_errors += 1

                    diagnostics = entry.get("diagnostics")
                    if not isinstance(diagnostics, dict):
                        err(f"{rel}: prices[{i}] diagnostics must be an object")
                        entry_errors += 1

                    expected_canonical_card_id = (
                        f"pokemon|en|{entry.get('setId')}|{entry.get('collectorNumber')}|{entry.get('normalizedName')}"
                    )
                    if entry.get("canonicalCardId") != expected_canonical_card_id:
                        err(f"{rel}: prices[{i}] canonicalCardId does not match expected identity format")
                        entry_errors += 1

                    currency_lower = str(entry.get("currency") or "").lower()
                    expected_price_identity_id = (
                        f"{expected_canonical_card_id}|{entry.get('variant')}|{entry.get('condition')}|"
                        f"{entry.get('market')}|{currency_lower}"
                    )
                    if entry.get("priceIdentityId") != expected_price_identity_id:
                        err(f"{rel}: prices[{i}] priceIdentityId does not match expected identity format")
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
        "cardscanrJpSetsLoaded",
        "catalogueSampleTargetsBuilt",
        "catalogueSearchQueriesBuilt",
        "pokewalletSetsFetched",
        "pokewalletJapaneseLikeSets",
        "pokewalletSetDetailsAttempted",
        "pokewalletSetDetailsSucceeded",
        "pokewalletCardsFetchedFromSetDetails",
        "searchFallbackRequestsAttempted",
        "searchFallbackResultsFound",
        "setMatchCandidatesBuilt",
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
        "pokewalletSetLanguagesSeen",
        "samplePokewalletSets",
        "sampleSetDetailCards",
        "sampleSearchQueries",
        "sampleSetMatches",
        "sampleUnmatchedCardScanRSets",
        "sampleUnmatchedPokewalletSets",
        "sampleSearchTargets",
        "sampleMatches",
        "sampleSkipped",
    ]:
        if not isinstance(data.get(field), list):
            err(f"diagnostics/pokewallet-jp-price-build-latest.json {field} must be a list")

    if not isinstance(data.get("blockerReason"), str):
        err("diagnostics/pokewallet-jp-price-build-latest.json blockerReason must be a string")

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


def check_pokewallet_pro_price_probe_diagnostics() -> None:
    print("\n[6d] Pokewallet Pro price probe diagnostics check")
    path = V1_DIR / "diagnostics" / "pokewallet-pro-price-probe-latest.json"
    if not path.exists():
        warn(f"Pokewallet Pro price probe diagnostics not found: {path.relative_to(ROOT)}")
        return

    data = load_json_file(path)
    if data is None or not isinstance(data, dict):
        err("diagnostics/pokewallet-pro-price-probe-latest.json must be a JSON object")
        return

    if check_required(
        data,
        REQUIRED_POKEWALLET_PRO_PRICE_PROBE_FIELDS,
        "diagnostics/pokewallet-pro-price-probe-latest.json",
    ):
        ok("diagnostics/pokewallet-pro-price-probe-latest.json has required fields")

    if data.get("provider") != "pokewallet":
        err("diagnostics/pokewallet-pro-price-probe-latest.json provider must be pokewallet")
    if data.get("status") not in ALLOWED_POKEWALLET_PRO_PRICE_PROBE_STATUSES:
        err(
            "diagnostics/pokewallet-pro-price-probe-latest.json status must be one of "
            f"{sorted(ALLOWED_POKEWALLET_PRO_PRICE_PROBE_STATUSES)}"
        )
    if data.get("proEndpointUsed") != "/prices/:setCode":
        err("diagnostics/pokewallet-pro-price-probe-latest.json proEndpointUsed must be /prices/:setCode")

    for field in [
        "requestsAttempted",
        "requestsSucceeded",
        "requestsFailed",
        "setsFetched",
        "proRequestsAttempted",
        "proRequestsSucceeded",
        "proRequestsFailed",
    ]:
        value = data.get(field)
        if value is not None and (not isinstance(value, int) or value < 0):
            err(f"diagnostics/pokewallet-pro-price-probe-latest.json {field} must be a non-negative integer")

    if not isinstance(data.get("apiKeyPresent"), bool):
        err("diagnostics/pokewallet-pro-price-probe-latest.json apiKeyPresent must be boolean")

    for field in [
        "languagesSeen",
        "setsSelectedByLanguage",
        "priceResponsesByLanguage",
        "priceRecordsFoundByLanguage",
    ]:
        if not isinstance(data.get(field), dict):
            err(f"diagnostics/pokewallet-pro-price-probe-latest.json {field} must be an object")

    for field in ["currenciesSeen", "sourcesSeen", "samplePriceRecords", "sampleSkipped"]:
        if not isinstance(data.get(field), list):
            err(f"diagnostics/pokewallet-pro-price-probe-latest.json {field} must be a list")

    if "AUD" in set(data.get("currenciesSeen", [])):
        err("diagnostics/pokewallet-pro-price-probe-latest.json must not infer AUD currency")
    if not isinstance(data.get("recommendation"), str) or not data.get("recommendation"):
        err("diagnostics/pokewallet-pro-price-probe-latest.json recommendation must be a non-empty string")


def check_pokewallet_pro_trial_discovery_diagnostics() -> None:
    print("\n[6e] Pokewallet Pro trial discovery diagnostics check")
    path = V1_DIR / "diagnostics" / "pokewallet-pro-trial-discovery-latest.json"
    if not path.exists():
        warn(f"Pokewallet Pro trial discovery diagnostics not found: {path.relative_to(ROOT)}")
        return

    data = load_json_file(path)
    if data is None or not isinstance(data, dict):
        err("diagnostics/pokewallet-pro-trial-discovery-latest.json must be a JSON object")
        return

    if check_required(
        data,
        REQUIRED_POKEWALLET_PRO_TRIAL_DISCOVERY_FIELDS,
        "diagnostics/pokewallet-pro-trial-discovery-latest.json",
    ):
        ok("diagnostics/pokewallet-pro-trial-discovery-latest.json has required fields")

    if data.get("provider") != "pokewallet":
        err("diagnostics/pokewallet-pro-trial-discovery-latest.json provider must be pokewallet")
    if data.get("mode") != "pro_trial_discovery":
        err("diagnostics/pokewallet-pro-trial-discovery-latest.json mode must be pro_trial_discovery")
    if data.get("status") not in ALLOWED_POKEWALLET_PRO_TRIAL_DISCOVERY_STATUSES:
        err(
            "diagnostics/pokewallet-pro-trial-discovery-latest.json status must be one of "
            f"{sorted(ALLOWED_POKEWALLET_PRO_TRIAL_DISCOVERY_STATUSES)}"
        )
    if not isinstance(data.get("apiKeyPresent"), bool):
        err("diagnostics/pokewallet-pro-trial-discovery-latest.json apiKeyPresent must be boolean")

    for field in [
        "requestsAttempted",
        "requestsSucceeded",
        "requestsFailed",
        "setsFetched",
        "setsSelectedTotal",
        "setsProcessedThisRun",
        "setsRemainingAfterRun",
        "imageSamplesChecked",
        "imageSamplesAvailable",
        "priceHistorySamplesChecked",
        "priceHistorySamplesWithData",
    ]:
        value = data.get(field)
        if not isinstance(value, int) or value < 0:
            err(f"diagnostics/pokewallet-pro-trial-discovery-latest.json {field} must be a non-negative integer")

    for field in [
        "languagesSeen",
        "setsByLanguage",
        "sampleSetsByLanguage",
        "endpointCoverage",
        "priceRecordsFoundByLanguage",
        "rateLimit",
        "rateSafety",
    ]:
        if not isinstance(data.get(field), dict):
            err(f"diagnostics/pokewallet-pro-trial-discovery-latest.json {field} must be an object")

    rate_safety = data.get("rateSafety")
    if isinstance(rate_safety, dict):
        for field in ["configuredMinHour", "configuredMinDay", "effectiveMinHour", "effectiveMinDay", "maxRequests"]:
            value = rate_safety.get(field)
            if not isinstance(value, int) or value < 0:
                err(f"diagnostics/pokewallet-pro-trial-discovery-latest.json rateSafety.{field} must be a non-negative integer")
        for field in ["forceSmallProTest", "tinyProTestAllowed"]:
            if not isinstance(rate_safety.get(field), bool):
                err(f"diagnostics/pokewallet-pro-trial-discovery-latest.json rateSafety.{field} must be boolean")

    for endpoint in ["prices", "statistics", "completionValue", "trending", "topCards", "priceHistory", "images"]:
        coverage = data.get("endpointCoverage", {})
        if isinstance(coverage, dict) and endpoint not in coverage:
            err(f"diagnostics/pokewallet-pro-trial-discovery-latest.json endpointCoverage missing {endpoint}")

    for field in [
        "currenciesSeen",
        "sourcesSeen",
        "samplePriceRecords",
        "sampleTrendingRecords",
        "sampleTopCards",
        "sampleImageChecks",
        "sampleSkipped",
        "diagnosticEvents",
    ]:
        if not isinstance(data.get(field), list):
            err(f"diagnostics/pokewallet-pro-trial-discovery-latest.json {field} must be a list")

    events = data.get("diagnosticEvents")
    if isinstance(events, list):
        allowed_events = {
            "tiny_pro_test_allowed",
            "stopped_rate_limit_safety",
            "pro_endpoint_success",
            "pro_required_403",
            "rate_limited_429",
            "numeric_prices_found",
            "no_numeric_prices_found",
        }
        for idx, event in enumerate(events):
            if not isinstance(event, dict):
                err(f"diagnostics/pokewallet-pro-trial-discovery-latest.json diagnosticEvents[{idx}] must be an object")
                continue
            event_name = event.get("event")
            if not isinstance(event_name, str) or not event_name:
                err(f"diagnostics/pokewallet-pro-trial-discovery-latest.json diagnosticEvents[{idx}].event must be a non-empty string")
            elif event_name not in allowed_events:
                err(f"diagnostics/pokewallet-pro-trial-discovery-latest.json diagnosticEvents[{idx}].event is not recognized")

    if "AUD" in set(data.get("currenciesSeen", [])):
        err("diagnostics/pokewallet-pro-trial-discovery-latest.json must not infer AUD currency")
    if not isinstance(data.get("recommendation"), str) or not data.get("recommendation"):
        err("diagnostics/pokewallet-pro-trial-discovery-latest.json recommendation must be a non-empty string")


def check_pokewallet_pro_trial_discovery_state() -> None:
    print("\n[6f] Pokewallet Pro trial discovery state check")
    path = ROOT / "data" / "pokewallet_pro_trial_discovery_state.json"
    if not path.exists():
        warn(f"Pokewallet Pro trial discovery state not found: {path.relative_to(ROOT)}")
        return

    data = load_json_file(path)
    if data is None or not isinstance(data, dict):
        err("data/pokewallet_pro_trial_discovery_state.json must be a JSON object")
        return

    if check_required(data, REQUIRED_POKEWALLET_PRO_TRIAL_STATE_FIELDS, "data/pokewallet_pro_trial_discovery_state.json"):
        ok("data/pokewallet_pro_trial_discovery_state.json has required fields")
    if data.get("mode") != "trial_discovery":
        err("data/pokewallet_pro_trial_discovery_state.json mode must be trial_discovery")

    for field in ["completedSetKeys", "failedSetKeys", "skippedSetKeys", "completedEndpointKeys"]:
        if not isinstance(data.get(field), list):
            err(f"data/pokewallet_pro_trial_discovery_state.json {field} must be a list")
    for field in [
        "requestsAttemptedTotal",
        "requestsSucceededTotal",
        "requestsFailedTotal",
        "priceRecordsFoundTotal",
        "imageSamplesCheckedTotal",
        "priceHistorySamplesCheckedTotal",
    ]:
        value = data.get(field)
        if not isinstance(value, int) or value < 0:
            err(f"data/pokewallet_pro_trial_discovery_state.json {field} must be a non-negative integer")
    if not isinstance(data.get("languagesCompleted"), dict):
        err("data/pokewallet_pro_trial_discovery_state.json languagesCompleted must be an object")


def check_pokewallet_catalog_full_state() -> None:
    print("\n[6g] Pokewallet catalogue full state check")
    path = ROOT / "data" / "pokewallet_catalog_full_state.json"
    if not path.exists():
        warn(f"Pokewallet catalogue full state not found: {path.relative_to(ROOT)}")
        return

    data = load_json_file(path)
    if data is None or not isinstance(data, dict):
        err("data/pokewallet_catalog_full_state.json must be a JSON object")
        return

    if check_required(data, REQUIRED_POKEWALLET_CATALOG_FULL_STATE_FIELDS, "data/pokewallet_catalog_full_state.json"):
        ok("data/pokewallet_catalog_full_state.json has required fields")
    if data.get("mode") != "full_catalogue":
        err("data/pokewallet_catalog_full_state.json mode must be full_catalogue")

    for field in ["completedSetKeys", "failedSetKeys", "skippedSetKeys"]:
        if not isinstance(data.get(field), list):
            err(f"data/pokewallet_catalog_full_state.json {field} must be a list")
    for field in [
        "requestsAttemptedTotal",
        "requestsSucceededTotal",
        "requestsFailedTotal",
        "cardsWrittenTotal",
    ]:
        value = data.get(field)
        if not isinstance(value, int) or value < 0:
            err(f"data/pokewallet_catalog_full_state.json {field} must be a non-negative integer")
    if not isinstance(data.get("languagesCompleted"), dict):
        err("data/pokewallet_catalog_full_state.json languagesCompleted must be an object")


def check_pokewallet_catalog_foundation_diagnostics() -> None:
    print("\n[6h] Pokewallet catalogue foundation diagnostics check")
    path = V1_DIR / "diagnostics" / "pokewallet-catalog-foundation-latest.json"
    if not path.exists():
        warn(f"Pokewallet catalogue foundation diagnostics not found: {path.relative_to(ROOT)}")
        return

    data = load_json_file(path)
    if data is None or not isinstance(data, dict):
        err("diagnostics/pokewallet-catalog-foundation-latest.json must be a JSON object")
        return

    if check_required(
        data,
        REQUIRED_POKEWALLET_CATALOG_FOUNDATION_FIELDS,
        "diagnostics/pokewallet-catalog-foundation-latest.json",
    ):
        ok("diagnostics/pokewallet-catalog-foundation-latest.json has required fields")

    if data.get("provider") != "pokewallet":
        err("diagnostics/pokewallet-catalog-foundation-latest.json provider must be pokewallet")
    if data.get("mode") not in {"catalogue_foundation", "full_catalogue"}:
        err("diagnostics/pokewallet-catalog-foundation-latest.json mode must be catalogue_foundation or full_catalogue")
    if data.get("status") not in ALLOWED_POKEWALLET_CATALOG_FOUNDATION_STATUSES:
        err(
            "diagnostics/pokewallet-catalog-foundation-latest.json status must be one of "
            f"{sorted(ALLOWED_POKEWALLET_CATALOG_FOUNDATION_STATUSES)}"
        )
    if not isinstance(data.get("apiKeyPresent"), bool):
        err("diagnostics/pokewallet-catalog-foundation-latest.json apiKeyPresent must be boolean")
    if not isinstance(data.get("fullCatalogueEnabled"), bool):
        err("diagnostics/pokewallet-catalog-foundation-latest.json fullCatalogueEnabled must be boolean")

    for field in [
        "requestsAttempted",
        "requestsSucceeded",
        "requestsFailed",
        "setsFetched",
        "setsProcessedThisRun",
        "setsRemainingAfterRun",
        "cardsWrittenThisRun",
        "setFilesWritten",
        "imageSamplesChecked",
        "imageSamplesAvailable",
    ]:
        value = data.get(field)
        if not isinstance(value, int) or value < 0:
            err(f"diagnostics/pokewallet-catalog-foundation-latest.json {field} must be a non-negative integer")

    for field in ["languagesSeen", "setsSelectedByLanguage", "cardsFetchedByLanguage", "cardsWrittenByLanguage"]:
        if not isinstance(data.get(field), dict):
            err(f"diagnostics/pokewallet-catalog-foundation-latest.json {field} must be an object")
    for field in ["sampleCards", "sampleSkipped"]:
        if not isinstance(data.get(field), list):
            err(f"diagnostics/pokewallet-catalog-foundation-latest.json {field} must be a list")
    if not isinstance(data.get("blockerReason"), str):
        err("diagnostics/pokewallet-catalog-foundation-latest.json blockerReason must be a string")
    if not isinstance(data.get("recommendation"), str) or not data.get("recommendation"):
        err("diagnostics/pokewallet-catalog-foundation-latest.json recommendation must be a non-empty string")


def check_pokewallet_provider_catalog() -> None:
    print("\n[6i] Pokewallet provider catalogue files check")
    root = V1_DIR / "provider-catalog" / "pokewallet"
    if not root.exists():
        warn(f"Pokewallet provider catalogue directory not found: {root.relative_to(ROOT)}")
        return

    disallowed_suffixes = {".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in disallowed_suffixes:
            err(f"Provider catalogue must not contain image binary files: {path.relative_to(ROOT)}")

    required_files = {
        "sets-summary.json",
        "languages-summary.json",
        "cards-sample.json",
        "image-availability-sample.json",
        "status.json",
        "cards-manifest.json",
    }
    existing_files = {path.name for path in root.glob("*.json")}
    missing = required_files - existing_files
    if missing:
        err(f"Pokewallet provider catalogue is missing files: {sorted(missing)}")

    status_data = None
    manifest_data = None
    for filename in sorted(required_files & existing_files):
        path = root / filename
        data = load_json_file(path)
        if data is None or not isinstance(data, dict):
            err(f"{path.relative_to(ROOT)} must be a JSON object")
            continue
        if filename == "status.json":
            required = REQUIRED_PROVIDER_CATALOG_STATUS_FIELDS
        elif filename == "cards-manifest.json":
            required = REQUIRED_PROVIDER_CATALOG_CARDS_MANIFEST_FIELDS
        else:
            required = REQUIRED_PROVIDER_CATALOG_TOP_FIELDS
        if check_required(data, required, str(path.relative_to(ROOT))):
            ok(f"{path.relative_to(ROOT)} has required top-level fields")
        if data.get("provider") != "pokewallet":
            err(f"{path.relative_to(ROOT)} provider must be pokewallet")
        if data.get("game") != "pokemon":
            err(f"{path.relative_to(ROOT)} game must be pokemon")
        if "notes" in required and not isinstance(data.get("notes"), list):
            err(f"{path.relative_to(ROOT)} notes must be a list")

        if filename == "status.json":
            status_data = data
            if data.get("status") not in {"partial", "complete", "not_available"}:
                err(f"{path.relative_to(ROOT)} status must be partial, complete, or not_available")
            if data.get("binaryImagesStored") is not False:
                err(f"{path.relative_to(ROOT)} binaryImagesStored must be false")
            if data.get("imageStorageMode") != "provider_reference_only":
                err(f"{path.relative_to(ROOT)} imageStorageMode must be provider_reference_only")
            if data.get("catalogueType") != "provider_metadata":
                err(f"{path.relative_to(ROOT)} catalogueType must be provider_metadata")
            languages = data.get("languages")
            if not isinstance(languages, dict):
                err(f"{path.relative_to(ROOT)} languages must be an object")
            else:
                for language, payload in sorted(languages.items()):
                    if not isinstance(payload, dict):
                        err(f"{path.relative_to(ROOT)} languages.{language} must be an object")
                        continue
                    for field in ["available", "complete"]:
                        if not isinstance(payload.get(field), bool):
                            err(f"{path.relative_to(ROOT)} languages.{language}.{field} must be boolean")
                    for field in ["setFileCount", "cardCount"]:
                        value = payload.get(field)
                        if not isinstance(value, int) or value < 0:
                            err(f"{path.relative_to(ROOT)} languages.{language}.{field} must be a non-negative integer")
        elif filename == "cards-manifest.json":
            manifest_data = data
            if data.get("status") not in {"partial", "complete", "not_available"}:
                err(f"{path.relative_to(ROOT)} status must be partial, complete, or not_available")
            for field in ["totalSetFiles", "totalCards"]:
                value = data.get(field)
                if not isinstance(value, int) or value < 0:
                    err(f"{path.relative_to(ROOT)} {field} must be a non-negative integer")
            languages = data.get("languages")
            if not isinstance(languages, dict):
                err(f"{path.relative_to(ROOT)} languages must be an object")
            else:
                total_manifest_files = 0
                total_manifest_cards = 0
                for language, payload in sorted(languages.items()):
                    if not isinstance(payload, dict):
                        err(f"{path.relative_to(ROOT)} languages.{language} must be an object")
                        continue
                    set_files = payload.get("setFiles")
                    if not isinstance(set_files, list):
                        err(f"{path.relative_to(ROOT)} languages.{language}.setFiles must be a list")
                        continue
                    total_manifest_files += len(set_files)
                    for i, item in enumerate(set_files):
                        item_label = f"{path.relative_to(ROOT)} languages.{language}.setFiles[{i}]"
                        if not isinstance(item, dict):
                            err(f"{item_label} must be an object")
                            continue
                        missing_fields = REQUIRED_PROVIDER_CATALOG_MANIFEST_SET_FILE_FIELDS - set(item.keys())
                        if missing_fields:
                            err(f"{item_label} missing fields: {sorted(missing_fields)}")
                        if item.get("cardScanRLanguage") != language:
                            err(f"{item_label} cardScanRLanguage must match language key")
                        card_count = item.get("cardCount")
                        if not isinstance(card_count, int) or card_count < 0:
                            err(f"{item_label} cardCount must be a non-negative integer")
                            card_count = 0
                        total_manifest_cards += card_count
                        url = item.get("url")
                        if not isinstance(url, str) or not url.startswith("/provider-catalog/pokewallet/cards/"):
                            err(f"{item_label} url must be a provider catalogue cards URL")
                            continue
                        target = V1_DIR / url.lstrip("/")
                        if not target.exists():
                            err(f"{item_label} url target does not exist: {target.relative_to(ROOT)}")
                            continue
                        actual_sha = sha256_file(target)
                        if item.get("sha256") != actual_sha:
                            err(f"{item_label} sha256 mismatch for {target.relative_to(ROOT)}")
                        target_data = load_json_file(target)
                        if isinstance(target_data, dict) and target_data.get("cardCount") != card_count:
                            err(f"{item_label} cardCount must match target file")
                if data.get("totalSetFiles") != total_manifest_files:
                    err(f"{path.relative_to(ROOT)} totalSetFiles must equal manifest set file count")
                if data.get("totalCards") != total_manifest_cards:
                    err(f"{path.relative_to(ROOT)} totalCards must equal manifest card count")
        elif filename == "sets-summary.json":
            if not isinstance(data.get("sets"), list):
                err(f"{path.relative_to(ROOT)} sets must be a list")
            if not isinstance(data.get("languagesSeen"), dict):
                err(f"{path.relative_to(ROOT)} languagesSeen must be an object")
        elif filename == "languages-summary.json":
            if not isinstance(data.get("languages"), list):
                err(f"{path.relative_to(ROOT)} languages must be a list")
        elif filename == "cards-sample.json":
            cards = data.get("cards")
            if not isinstance(cards, list):
                err(f"{path.relative_to(ROOT)} cards must be a list")
                continue
            if data.get("cardCount") != len(cards):
                err(f"{path.relative_to(ROOT)} cardCount must equal cards length")
            for i, card in enumerate(cards):
                if not isinstance(card, dict):
                    err(f"{path.relative_to(ROOT)} cards[{i}] must be an object")
                    continue
                missing_fields = REQUIRED_POKEWALLET_PROVIDER_CARD_FIELDS - set(card.keys())
                if missing_fields:
                    err(f"{path.relative_to(ROOT)} cards[{i}] missing fields: {sorted(missing_fields)}")
                if card.get("imageEndpoint") is not None and not str(card.get("imageEndpoint")).startswith("/images/"):
                    err(f"{path.relative_to(ROOT)} cards[{i}] imageEndpoint must be an /images/ endpoint or null")
                for field in ["imageEndpointLow", "imageEndpointHigh"]:
                    value = card.get(field)
                    if value is not None and not str(value).startswith("/images/"):
                        err(f"{path.relative_to(ROOT)} cards[{i}] {field} must be an /images/ endpoint or null")
                if card.get("imageAvailable") is not None and not isinstance(card.get("imageAvailable"), bool):
                    err(f"{path.relative_to(ROOT)} cards[{i}] imageAvailable must be boolean or null")
                if not isinstance(card.get("imageCacheIdentityBasis"), dict):
                    err(f"{path.relative_to(ROOT)} cards[{i}] imageCacheIdentityBasis must be an object")
                if not isinstance(card.get("imageCacheKey"), str) or "|" not in card.get("imageCacheKey", ""):
                    err(f"{path.relative_to(ROOT)} cards[{i}] imageCacheKey must be a pipe-delimited string")
                if card.get("imageCacheStrategy") != "cache_once_recheck_on_failure":
                    err(f"{path.relative_to(ROOT)} cards[{i}] imageCacheStrategy is invalid")
                if not isinstance(card.get("rawKeys"), list):
                    err(f"{path.relative_to(ROOT)} cards[{i}] rawKeys must be a list")
                for field in ["hasPriceFields", "hasTcgplayerFields", "hasCardmarketFields"]:
                    if not isinstance(card.get(field), bool):
                        err(f"{path.relative_to(ROOT)} cards[{i}] {field} must be boolean")
        elif filename == "image-availability-sample.json":
            if not isinstance(data.get("samples"), list):
                err(f"{path.relative_to(ROOT)} samples must be a list")
            for field in ["imageSamplesChecked", "imageSamplesAvailable"]:
                value = data.get(field)
                if not isinstance(value, int) or value < 0:
                    err(f"{path.relative_to(ROOT)} {field} must be a non-negative integer")

    if isinstance(status_data, dict) and isinstance(manifest_data, dict):
        status_languages = status_data.get("languages")
        manifest_languages = manifest_data.get("languages")
        if isinstance(status_languages, dict) and isinstance(manifest_languages, dict):
            if set(status_languages) != set(manifest_languages):
                err("provider-catalog/pokewallet/status.json languages must match cards-manifest.json languages")
            for language, status_payload in sorted(status_languages.items()):
                manifest_payload = manifest_languages.get(language)
                if not isinstance(status_payload, dict) or not isinstance(manifest_payload, dict):
                    continue
                set_files = manifest_payload.get("setFiles")
                if not isinstance(set_files, list):
                    continue
                manifest_card_count = sum(item.get("cardCount", 0) for item in set_files if isinstance(item, dict) and isinstance(item.get("cardCount"), int))
                if status_payload.get("setFileCount") != len(set_files):
                    err(f"provider-catalog/pokewallet/status.json languages.{language}.setFileCount must match manifest")
                if status_payload.get("cardCount") != manifest_card_count:
                    err(f"provider-catalog/pokewallet/status.json languages.{language}.cardCount must match manifest")

    cards_root = root / "cards"
    if not cards_root.exists():
        return

    for path in sorted(cards_root.rglob("*.json")):
        data = load_json_file(path)
        label = str(path.relative_to(ROOT))
        if data is None or not isinstance(data, dict):
            err(f"{label} must be a JSON object")
            continue
        if check_required(data, REQUIRED_POKEWALLET_PROVIDER_SET_FILE_FIELDS, label):
            ok(f"{label} has required top-level fields")
        if data.get("provider") != "pokewallet":
            err(f"{label} provider must be pokewallet")
        if data.get("game") != "pokemon":
            err(f"{label} game must be pokemon")
        if data.get("imageReferencesOnly") is not True:
            err(f"{label} imageReferencesOnly must be true")
        for disallowed_key in ["data", "results", "rawResponse", "set", "card_info", "tcgplayer", "cardmarket"]:
            if disallowed_key in data:
                err(f"{label} must not contain raw provider field {disallowed_key}")
        cards = data.get("cards")
        if not isinstance(cards, list):
            err(f"{label} cards must be a list")
            continue
        if data.get("cardCount") != len(cards):
            err(f"{label} cardCount must equal cards length")
        for i, card in enumerate(cards):
            card_label = f"{label} cards[{i}]"
            if not isinstance(card, dict):
                err(f"{card_label} must be an object")
                continue
            missing_fields = REQUIRED_POKEWALLET_PROVIDER_SET_CARD_FIELDS - set(card.keys())
            if missing_fields:
                err(f"{card_label} missing fields: {sorted(missing_fields)}")
            for disallowed_key in ["data", "results", "rawResponse", "card_info", "tcgplayer", "cardmarket", "prices"]:
                if disallowed_key in card:
                    err(f"{card_label} must not contain raw provider field {disallowed_key}")
            for field in ["imageEndpoint", "imageEndpointLow", "imageEndpointHigh"]:
                value = card.get(field)
                if value is not None and not str(value).startswith("/images/"):
                    err(f"{card_label} {field} must be an /images/ endpoint or null")
            for field in ["imageLowAvailable", "imageHighAvailable"]:
                if card.get(field) is not None and not isinstance(card.get(field), bool):
                    err(f"{card_label} {field} must be boolean or null")
            if card.get("imageAvailable") is not None and not isinstance(card.get("imageAvailable"), bool):
                err(f"{card_label} imageAvailable must be boolean or null")
            if not isinstance(card.get("imageCacheIdentityBasis"), dict):
                err(f"{card_label} imageCacheIdentityBasis must be an object")
            if not isinstance(card.get("imageCacheKey"), str) or "|" not in card.get("imageCacheKey", ""):
                err(f"{card_label} imageCacheKey must be a pipe-delimited string")
            if card.get("imageCacheStrategy") != "cache_once_recheck_on_failure":
                err(f"{card_label} imageCacheStrategy is invalid")
            if not isinstance(card.get("rawKeys"), list):
                err(f"{card_label} rawKeys must be a list")
            for field in ["hasPriceFields", "hasTcgplayerFields", "hasCardmarketFields"]:
                if not isinstance(card.get(field), bool):
                    err(f"{card_label} {field} must be boolean")


def check_image_cache_policy() -> None:
    print("\n[6j] Image cache policy check")
    images_root = V1_DIR / "images"
    path = images_root / "cache-policy.json"
    if not path.exists():
        err(f"Image cache policy file not found: {path.relative_to(ROOT)}")
        return

    disallowed_suffixes = {".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
    for image_path in sorted(images_root.rglob("*")):
        if image_path.is_file() and image_path.suffix.lower() in disallowed_suffixes:
            err(f"public/v1/images must not contain image binary files: {image_path.relative_to(ROOT)}")

    data = load_json_file(path)
    if data is None or not isinstance(data, dict):
        err("images/cache-policy.json must be a JSON object")
        return
    if check_required(data, REQUIRED_IMAGE_CACHE_POLICY_FIELDS, "images/cache-policy.json"):
        ok("images/cache-policy.json has required fields")
    if data.get("binaryImagesStored") is not False:
        err("images/cache-policy.json binaryImagesStored must be false")
    if data.get("imageStorageMode") != "provider_reference_only":
        err("images/cache-policy.json imageStorageMode must be provider_reference_only")
    if not isinstance(data.get("cacheKeyRule"), str) or "{game}" not in data.get("cacheKeyRule", ""):
        err("images/cache-policy.json cacheKeyRule must describe the card identity key")
    if not isinstance(data.get("recommendedFutureStorage"), list):
        err("images/cache-policy.json recommendedFutureStorage must be a list")
    if not isinstance(data.get("defaultPolicy"), dict):
        err("images/cache-policy.json defaultPolicy must be an object")
    if not isinstance(data.get("notes"), list):
        err("images/cache-policy.json notes must be a list")


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


def check_supported_sources() -> None:
    print("\n[10] Supported sources check")
    if not SUPPORTED_SOURCES_PATH.exists():
        err(f"Supported sources file not found: {SUPPORTED_SOURCES_PATH.relative_to(ROOT)}")
        return

    data = load_json_file(SUPPORTED_SOURCES_PATH)
    if data is None or not isinstance(data, dict):
        err("supported-sources.json must be a JSON object")
        return

    if check_required(data, REQUIRED_SUPPORTED_SOURCES_FIELDS, "supported-sources.json"):
        ok("supported-sources.json has required top-level fields")

    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        err("supported-sources.json sources must be a non-empty list")
        return

    seen_ids: set[str] = set()
    aliases_by_id: dict[str, set[str]] = {}
    alias_to_id: dict[str, str] = {}

    for i, entry in enumerate(sources):
        label = f"supported-sources.json sources[{i}]"
        if not isinstance(entry, dict):
            err(f"{label} must be an object")
            continue

        missing = REQUIRED_SUPPORTED_SOURCE_ENTRY_FIELDS - set(entry.keys())
        if missing:
            err(f"{label} missing fields: {sorted(missing)}")

        source_id = entry.get("id")
        if not isinstance(source_id, str) or not source_id:
            err(f"{label} id must be a non-empty string")
            source_id = None
        else:
            if source_id in seen_ids:
                err(f"supported-sources.json duplicate id: {source_id}")
            else:
                seen_ids.add(source_id)
            if source_id in LEGACY_PRIMARY_SOURCE_IDS:
                err(f"{label} id must be canonical snake_case, got legacy id: {source_id}")
            if not is_lower_snake_case(source_id):
                err(f"{label} id must be lowercase snake_case")
            if source_id not in CANONICAL_SUPPORTED_SOURCE_IDS:
                err(f"{label} id must be one of {sorted(CANONICAL_SUPPORTED_SOURCE_IDS)}")

        description = entry.get("description")
        if not isinstance(description, str) or not description.strip():
            err(f"{label} description must be a non-empty string")

        if not isinstance(entry.get("enabled"), bool):
            err(f"{label} enabled must be boolean")

        aliases_raw = entry.get("aliases")
        entry_aliases: set[str] = set()
        if not isinstance(aliases_raw, list):
            err(f"{label} aliases must be an array")
        else:
            for j, alias in enumerate(aliases_raw):
                alias_label = f"{label} aliases[{j}]"
                if not isinstance(alias, str) or not alias:
                    err(f"{alias_label} must be a non-empty string")
                    continue
                if source_id is not None and alias == source_id:
                    err(f"{alias_label} must not duplicate id")
                if alias in entry_aliases:
                    err(f"{label} aliases must not contain duplicates: {alias}")
                    continue
                entry_aliases.add(alias)
                previous_id = alias_to_id.get(alias)
                if previous_id is not None and source_id is not None and previous_id != source_id:
                    err(f"alias '{alias}' is assigned to multiple ids: {previous_id}, {source_id}")
                elif source_id is not None:
                    alias_to_id[alias] = source_id

        if source_id is not None:
            aliases_by_id[source_id] = entry_aliases

    missing_canonical_ids = CANONICAL_SUPPORTED_SOURCE_IDS - seen_ids
    if missing_canonical_ids:
        err(f"supported-sources.json is missing canonical ids: {sorted(missing_canonical_ids)}")

    for canonical_id, required_aliases in REQUIRED_SUPPORTED_SOURCE_ALIASES.items():
        source_aliases = aliases_by_id.get(canonical_id)
        if source_aliases is None:
            if canonical_id in seen_ids:
                err(f"supported-sources.json id '{canonical_id}' must define an aliases array")
            continue
        if not required_aliases.issubset(source_aliases):
            err(
                f"supported-sources.json id '{canonical_id}' is missing required aliases: "
                f"{sorted(required_aliases - source_aliases)}"
            )


def check_catalogues() -> None:
    print("\n[11] Catalogue check")
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
    print("\n[12] Tracked history check")
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
    check_pokewallet_pro_price_probe_diagnostics()
    check_pokewallet_pro_trial_discovery_diagnostics()
    check_pokewallet_pro_trial_discovery_state()
    check_pokewallet_catalog_full_state()
    check_pokewallet_catalog_foundation_diagnostics()
    check_pokewallet_provider_catalog()
    check_image_cache_policy()
    check_api_manifest()
    check_api_notes()
    check_schemas()
    check_supported_sources()
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
