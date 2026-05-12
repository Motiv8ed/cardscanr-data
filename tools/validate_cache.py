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
REQUIRED_DIAGNOSTICS_FIELDS = {"buildStatus", "builtAtUtc", "cacheVersion", "datasetsBuilt"}

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
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


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
    for ds in datasets:
        if not check_required(ds, REQUIRED_DATASET_FIELDS, f"dataset entry {ds.get('id', '?')}"):
            continue

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
