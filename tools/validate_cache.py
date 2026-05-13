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
REQUIRED_PRICE_ENTRY_FIELDS = {
    "canonicalId",
    "setId",
    "collectorNumber",
    "normalizedName",
    "variant",
    "condition",
    "currency",
    "marketPrice",
    "source",
    "fetchedAtUtc",
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
errors: list[str] = []
warnings: list[str] = []


def err(msg: str) -> None:
    errors.append(f"ERROR: {msg}")
    print(f"  ✗ {msg}")


def warn(msg: str) -> None:
    warnings.append(f"WARNING: {msg}")
    print(f"  ⚠ {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def sha256_file(path: Path) -> str:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    canonical = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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

        if not check_required(data, REQUIRED_PRICE_FIELDS, str(rel)):
            continue

        prices = data.get("prices", [])
        if not isinstance(prices, list):
            err(f"{rel}: 'prices' must be a list")
            continue

        seen_ids: set[str] = set()
        dupes: list[str] = []
        entry_errors = 0
        for i, entry in enumerate(prices):
            if not isinstance(entry, dict):
                err(f"{rel}: prices[{i}] is not an object")
                entry_errors += 1
                continue
            missing = REQUIRED_PRICE_ENTRY_FIELDS - set(entry.keys())
            if missing:
                err(f"{rel}: prices[{i}] missing fields: {sorted(missing)}")
                entry_errors += 1
            cid = entry.get("canonicalId", "")
            if cid in seen_ids:
                dupes.append(cid)
            else:
                seen_ids.add(cid)

        if dupes:
            err(f"{rel}: duplicate canonicalId values: {dupes}")
        elif entry_errors == 0:
            ok(f"{rel}: {len(prices)} entries, no duplicates, all fields present")


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


def check_catalog_placeholders() -> None:
    print("\n[10] Catalogue placeholder check")
    catalog_files = sorted((V1_DIR / "catalog").rglob("sets.json")) if (V1_DIR / "catalog").exists() else []
    if not catalog_files:
        warn("No catalogue placeholder files found under public/v1/catalog/")
        return
    for path in catalog_files:
        rel = path.relative_to(ROOT)
        data = load_json_file(path)
        if data is None or not isinstance(data, dict):
            err(f"{rel} must be a JSON object")
            continue
        if check_required(data, REQUIRED_PLACEHOLDER_CATALOG_FIELDS, str(rel)):
            ok(f"{rel} has required placeholder fields")
        if data.get("catalogueStatus") != "not_built_yet":
            err(f"{rel} catalogueStatus must be not_built_yet for the placeholder catalogue")
        if data.get("cardsAvailable") is not False:
            err(f"{rel} cardsAvailable must be false for the placeholder catalogue")
        sets = data.get("sets", [])
        if not isinstance(sets, list):
            err(f"{rel} sets must be a list")


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
    print("=" * 60)
    print("CardScanR cache validation")
    print("=" * 60)

    check_all_json_syntax()
    check_index()
    check_price_files()
    check_diagnostics()
    check_api_manifest()
    check_api_notes()
    check_schemas()
    check_catalog_placeholders()
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
