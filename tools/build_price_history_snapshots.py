#!/usr/bin/env python3
"""Build tracked-card history snapshots from existing current price files.

This tool is intentionally read-from-cache only. It does not call external
providers and it does not invent unavailable prices.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PUBLIC_DIR = ROOT / "public" / "v1"
CURRENT_PRICES_DIR = PUBLIC_DIR / "prices" / "current" / "pokemon"
HISTORY_ROOT_DIR = PUBLIC_DIR / "history"
HISTORY_DAILY_DIR = HISTORY_ROOT_DIR / "daily"
TRACKED_CARDS_PATH = HISTORY_ROOT_DIR / "tracked-cards.json"
CARDS_TO_TRACK_PATH = DATA_DIR / "cards_to_track.json"
SCHEMA_VERSION = "1.0.0"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_json_if_changed(path: Path, payload: Any) -> bool:
    encoded = json_bytes(payload)
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_bytes(encoded)
    os.replace(tmp_path, path)
    return True


def strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: strip_volatile(item) for key, item in value.items() if key != "generatedAtUtc"}
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    return value


def preserve_generated_at_if_material_same(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return payload
    try:
        previous = load_json(path)
    except (OSError, json.JSONDecodeError):
        return payload
    if not isinstance(previous, dict):
        return payload
    if strip_volatile(previous) != strip_volatile(payload):
        return payload
    previous_generated = previous.get("generatedAtUtc")
    if isinstance(previous_generated, str) and previous_generated:
        payload["generatedAtUtc"] = previous_generated
    return payload


def normalize_languages(raw: str | None) -> list[str]:
    values = [item.strip().lower() for item in str(raw or "en,jp").split(",") if item.strip()]
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def load_cards_to_track(languages: set[str]) -> list[dict[str, Any]]:
    if not CARDS_TO_TRACK_PATH.exists():
        return []
    payload = load_json(CARDS_TO_TRACK_PATH)
    cards = payload.get("cards") if isinstance(payload, dict) else []
    if not isinstance(cards, list):
        return []
    return [
        card
        for card in cards
        if isinstance(card, dict)
        and str(card.get("game") or "") == "pokemon"
        and str(card.get("language") or "").lower() in languages
        and isinstance(card.get("canonicalId"), str)
    ]


def load_current_price_index(languages: set[str]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for language in sorted(languages):
        price_dir = CURRENT_PRICES_DIR / language
        if not price_dir.exists():
            continue
        for path in sorted(price_dir.glob("*.json"), key=lambda item: item.name.lower()):
            if path.name == "status.json":
                continue
            payload = load_json(path)
            prices = payload.get("prices") if isinstance(payload, dict) else None
            if not isinstance(prices, list):
                continue
            for record in prices:
                if not isinstance(record, dict):
                    continue
                canonical_id = record.get("canonicalId")
                if isinstance(canonical_id, str) and canonical_id:
                    records[canonical_id] = record
    return records


def price_snapshot(price: dict[str, Any], fallback_ts: str) -> dict[str, Any]:
    return {
        "currency": price.get("currency"),
        "marketPrice": price.get("marketPrice"),
        "lowPrice": price.get("lowPrice"),
        "highPrice": price.get("highPrice"),
        "source": price.get("source"),
        "fetchedAtUtc": price.get("fetchedAtUtc") or fallback_ts,
    }


def daily_price_entry(card: dict[str, Any], price: dict[str, Any], fallback_ts: str) -> dict[str, Any]:
    snapshot = price_snapshot(price, fallback_ts)
    return {
        "canonicalId": card["canonicalId"],
        "setId": card.get("setId"),
        "collectorNumber": card.get("collectorNumber"),
        "normalizedName": card.get("normalizedName"),
        "variant": card.get("variant"),
        "condition": card.get("condition"),
        "currency": snapshot.get("currency"),
        "marketPrice": snapshot.get("marketPrice"),
        "lowPrice": snapshot.get("lowPrice"),
        "highPrice": snapshot.get("highPrice"),
        "source": snapshot.get("source"),
        "fetchedAtUtc": snapshot.get("fetchedAtUtc"),
    }


def snapshots_equal(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    keys = {"currency", "marketPrice", "lowPrice", "highPrice", "source", "fetchedAtUtc"}
    return {key: left.get(key) for key in keys} == {key: right.get(key) for key in keys}


def build_tracked_cards_payload(
    *,
    ts: str,
    cards: list[dict[str, Any]],
    price_index: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], int, int, int]:
    existing_by_id: dict[str, dict[str, Any]] = {}
    if TRACKED_CARDS_PATH.exists():
        existing = load_json(TRACKED_CARDS_PATH)
        for item in existing.get("cards", []) if isinstance(existing, dict) else []:
            if isinstance(item, dict) and isinstance(item.get("canonicalId"), str):
                existing_by_id[item["canonicalId"]] = item

    tracked_cards: list[dict[str, Any]] = []
    created = 0
    updated = 0
    unchanged = 0

    for card in sorted(cards, key=lambda item: str(item.get("canonicalId") or "")):
        canonical_id = str(card.get("canonicalId") or "")
        price = price_index.get(canonical_id)
        if not price:
            continue
        latest_price = price_snapshot(price, ts)
        existing = existing_by_id.get(canonical_id)

        if existing:
            first_tracked_at = existing.get("firstTrackedAtUtc") or ts
            first_tracked_price = existing.get("firstTrackedPrice") or latest_price
            old_latest = existing.get("latestPrice") if isinstance(existing.get("latestPrice"), dict) else None
            changed = not snapshots_equal(latest_price, old_latest)
            snapshot_count = int(existing.get("trackingStats", {}).get("snapshotCount", 0))
            if changed:
                snapshot_count += 1
                updated += 1
            else:
                unchanged += 1
        else:
            first_tracked_at = ts
            first_tracked_price = latest_price
            snapshot_count = 1
            changed = True
            created += 1

        first_market = to_float(first_tracked_price.get("marketPrice"))
        latest_market = to_float(latest_price.get("marketPrice"))
        prior_stats = existing.get("trackingStats", {}) if isinstance(existing, dict) else {}
        prior_highest = to_float(prior_stats.get("highestSinceTracked"))
        prior_lowest = to_float(prior_stats.get("lowestSinceTracked"))

        if latest_market is None:
            highest_since_tracked = prior_highest if prior_highest is not None else first_market
            lowest_since_tracked = prior_lowest if prior_lowest is not None else first_market
        else:
            highest_values = [item for item in [prior_highest, first_market, latest_market] if item is not None]
            lowest_values = [item for item in [prior_lowest, first_market, latest_market] if item is not None]
            highest_since_tracked = max(highest_values) if highest_values else None
            lowest_since_tracked = min(lowest_values) if lowest_values else None

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
                "lastTrackedAtUtc": ts if changed else existing.get("lastTrackedAtUtc", ts) if existing else ts,
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

    return {"schemaVersion": SCHEMA_VERSION, "generatedAtUtc": ts, "cards": tracked_cards}, created, updated, unchanged


def build_daily_payloads(
    *,
    ts: str,
    day: str,
    cards: list[dict[str, Any]],
    price_index: dict[str, dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for card in cards:
        canonical_id = str(card.get("canonicalId") or "")
        price = price_index.get(canonical_id)
        if not price:
            continue
        game = str(card.get("game") or "pokemon")
        language = str(card.get("language") or "")
        if not language:
            continue
        grouped[(game, language)].append(daily_price_entry(card, price, ts))

    payloads: dict[tuple[str, str], dict[str, Any]] = {}
    for (game, language), prices in sorted(grouped.items()):
        prices.sort(key=lambda item: str(item.get("canonicalId") or ""))
        payloads[(game, language)] = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAtUtc": ts,
            "date": day,
            "game": game,
            "language": language,
            "prices": prices,
        }
    return payloads


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build tracked history snapshots from current price files.")
    parser.add_argument("--languages", default="en,jp", help="Comma-separated languages to process.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be written without changing files.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ts = now_utc()
    day = ts[:10]
    languages = set(normalize_languages(args.languages))
    cards = load_cards_to_track(languages)
    price_index = load_current_price_index(languages)

    tracked_payload, created, updated, unchanged = build_tracked_cards_payload(
        ts=ts,
        cards=cards,
        price_index=price_index,
    )
    daily_payloads = build_daily_payloads(ts=ts, day=day, cards=cards, price_index=price_index)

    matched_count = len(tracked_payload["cards"])
    missing_count = max(0, len(cards) - matched_count)
    changed_files: list[str] = []

    if not args.dry_run and matched_count:
        tracked_payload = preserve_generated_at_if_material_same(TRACKED_CARDS_PATH, tracked_payload)
        if write_json_if_changed(TRACKED_CARDS_PATH, tracked_payload):
            changed_files.append(str(TRACKED_CARDS_PATH.relative_to(ROOT)))

        for (game, language), payload in daily_payloads.items():
            path = HISTORY_DAILY_DIR / day / game / language / "tracked.json"
            payload = preserve_generated_at_if_material_same(path, payload)
            if write_json_if_changed(path, payload):
                changed_files.append(str(path.relative_to(ROOT)))

    report = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "languages": sorted(languages),
        "trackedCardsConfigured": len(cards),
        "trackedCardsMatchedToCurrentPrices": matched_count,
        "trackedCardsMissingCurrentPrices": missing_count,
        "trackedCardsCreated": created,
        "trackedCardsUpdated": updated,
        "trackedCardsUnchanged": unchanged,
        "dailyHistoryFilesPrepared": len(daily_payloads),
        "changedFiles": changed_files,
        "status": "ok" if matched_count else "no_current_price_records",
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
