#!/usr/bin/env python3
"""
build_price_cache.py

Builds the static price cache files under public/v1/prices/ from the cards
listed in data/cards_to_track.json.

For each unique (game, language) combination it writes a sample.json file
containing placeholder AUD prices sourced from the manual_seed fallback.
After writing all price files it regenerates public/v1/index.json with fresh
sha256 hashes and public/v1/diagnostics/latest-build.json.

Optional environment variables
-------------------------------
POKEMON_TCG_API_KEY  – Pokémon TCG API key (unused in seed mode, reserved for
                       future live-fetch integration).
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PUBLIC_DIR = ROOT / "public" / "v1"
PRICES_DIR = PUBLIC_DIR / "prices"
DIAGNOSTICS_DIR = PUBLIC_DIR / "diagnostics"
INDEX_PATH = PUBLIC_DIR / "index.json"
DIAG_PATH = DIAGNOSTICS_DIR / "latest-build.json"
CARDS_PATH = DATA_DIR / "cards_to_track.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
SCHEMA_VERSION = "1.0.0"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def load_json(path: Path) -> object:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Seed price generation
# ---------------------------------------------------------------------------
# Condition priority (best → worst)
CONDITIONS = ["near_mint", "lightly_played", "moderately_played", "heavily_played"]

# Rough condition multipliers relative to near_mint market price
CONDITION_MULTIPLIER = {
    "near_mint": 1.0,
    "lightly_played": 0.70,
    "moderately_played": 0.50,
    "heavily_played": 0.30,
}

# Very rough seed prices (AUD) for well-known cards
SEED_PRICES: dict[str, float] = {
    "charizard": 450.0,
    "blastoise": 210.0,
    "venusaur": 185.0,
    "rizaadon": 380.0,   # JP Charizard
    "kamekkusu": 175.0,  # JP Blastoise
    "fushigibana": 160.0,  # JP Venusaur
}

DEFAULT_SEED_PRICE = 10.0


def seed_price(normalizedName: str, condition: str) -> tuple[float, float, float]:
    """Return (marketPrice, lowPrice, highPrice) in AUD for a seed card."""
    base = SEED_PRICES.get(normalizedName.lower(), DEFAULT_SEED_PRICE)
    mult = CONDITION_MULTIPLIER.get(condition, 1.0)
    market = round(base * mult, 2)
    low = round(market * 0.85, 2)
    high = round(market * 1.30, 2)
    return market, low, high


def build_price_entry(card: dict, ts: str) -> dict:
    market, low, high = seed_price(card["normalizedName"], card["condition"])
    return {
        "canonicalId": card["canonicalId"],
        "setId": card["setId"],
        "collectorNumber": card["collectorNumber"],
        "normalizedName": card["normalizedName"],
        "variant": card["variant"],
        "condition": card["condition"],
        "currency": "AUD",
        "marketPrice": market,
        "lowPrice": low,
        "highPrice": high,
        "source": "manual_seed",
        "fetchedAtUtc": ts,
    }


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------
def build() -> None:
    ts = now_utc()
    print(f"[build_price_cache] Starting build at {ts}")

    cards: list[dict] = load_json(CARDS_PATH).get("cards", [])
    if not cards:
        print("[build_price_cache] No cards found in cards_to_track.json – nothing to do.")
        sys.exit(0)

    # Group cards by (game, language)
    groups: dict[tuple[str, str], list[dict]] = {}
    for card in cards:
        key = (card["game"], card["language"])
        groups.setdefault(key, []).append(card)

    datasets = []

    for (game, language), group_cards in sorted(groups.items()):
        price_path = PRICES_DIR / game / language / "sample.json"

        # Check for duplicate canonicalIds in this group
        seen: set[str] = set()
        prices = []
        for card in group_cards:
            cid = card["canonicalId"]
            if cid in seen:
                print(f"  [WARN] Duplicate canonicalId skipped: {cid}")
                continue
            seen.add(cid)
            prices.append(build_price_entry(card, ts))

        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAtUtc": ts,
            "game": game,
            "language": language,
            "prices": prices,
        }

        write_json(price_path, payload)
        digest = sha256_file(price_path)
        rel_url = f"/v1/prices/{game}/{language}/sample.json"
        dataset_id = f"prices_{game}_{language}"

        datasets.append({
            "id": dataset_id,
            "description": f"{game.capitalize()} TCG {language.upper()} card prices (AUD)",
            "url": rel_url,
            "sha256": digest,
        })
        print(f"  Wrote {price_path}  sha256={digest}")

    # Update cacheVersion to use the current UTC timestamp in the format YYYY.MM.DD.HHMM
    cache_version = datetime.now(timezone.utc).strftime("%Y.%m.%d.%H%M")

    # Update index.json
    index = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "cacheVersion": cache_version,
        "datasets": datasets,
    }
    write_json(INDEX_PATH, index)
    print(f"  Updated {INDEX_PATH} with cacheVersion={cache_version}")

    # Update diagnostics
    diag = {
        "buildStatus": "success",
        "builtAtUtc": ts,
        "cacheVersion": cache_version,
        "datasetsBuilt": [d["id"] for d in datasets],
        "notes": "Built by build_price_cache.py",
    }
    write_json(DIAG_PATH, diag)
    print(f"  Updated {DIAG_PATH}")

    print("[build_price_cache] Build complete.")


if __name__ == "__main__":
    build()
