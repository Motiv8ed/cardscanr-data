#!/usr/bin/env python3
"""Gap-fill English + Japanese production catalogues for post-Chaos-Rising / post-M5 releases.

EN: Pokemon TCG API set IDs me5, me55, me55c (canonical CardScanR IDs).
JP: PokéWallet provider sets m6 / m6a (matching existing m4/m5 promotion identity).

Does not enable new languages. Does not fabricate pricing.
Partial sets are written with catalogueStatus=partial_built and never marked built/complete.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.build_price_cache import (  # noqa: E402
    SCHEMA_VERSION,
    SOURCE_ID_POKEMON_TCG_API,
    build_catalog_card_record,
    build_catalog_set_record,
    catalogue_card_sort_key,
)
from tools.build_pokewallet_catalog_foundation import (  # noqa: E402
    ProviderSet,
    fetch_set_cards,
)
from tools.promote_provider_catalog_to_app_catalog import (  # noqa: E402
    PROMOTION_DETAIL_SOURCE,
    PROMOTION_SOURCE,
    ProviderRecord,
    build_app_card,
    build_candidate,
    make_identity_key,
    normalize_number,
    safe_set_id,
)
from dataclasses import replace  # noqa: E402

CATALOG_ROOT = ROOT / "public" / "v1" / "catalog" / "pokemon"
PROVIDER_CARDS = ROOT / "public" / "v1" / "provider-catalog" / "pokewallet" / "cards"
REPORT_PATH = ROOT / "reports" / "en_jp_catalogue_freshness_gapfill_latest.json"

POKEMON_TCG_API_BASE = "https://api.pokemontcg.io/v2"
POKEWALLET_BASE = "https://api.pokewallet.io"


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def http_json(url: str, *, headers: dict[str, str] | None = None, retries: int = 5) -> Any:
    last: Exception | None = None
    req_headers = {"User-Agent": "CardScanR-FreshnessGapfill", "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.25 * (attempt + 1))
    assert last is not None
    raise last


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(encoded)
    os.replace(tmp, path)


def fetch_pokemon_tcg_set(set_id: str) -> dict[str, Any]:
    data = http_json(f"{POKEMON_TCG_API_BASE}/sets/{urllib.parse.quote(set_id)}")
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        raise ValueError(f"Unexpected set payload for {set_id}")
    return data["data"]


def fetch_pokemon_tcg_cards(set_id: str) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    page = 1
    total: int | None = None
    while True:
        q = urllib.parse.quote(f"set.id:{set_id}")
        url = f"{POKEMON_TCG_API_BASE}/cards?q={q}&pageSize=50&page={page}"
        data = http_json(url)
        batch = data.get("data") if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise ValueError(f"Unexpected cards payload for {set_id} page {page}")
        total = data.get("totalCount") if isinstance(data.get("totalCount"), int) else total
        cards.extend(batch)
        if not batch or (total is not None and len(cards) >= total):
            break
        page += 1
        time.sleep(0.2)
    return cards


def image_coverage(cards: list[dict[str, Any]]) -> dict[str, int]:
    small = sum(1 for c in cards if c.get("imageSmall") or c.get("imageUrlSmall"))
    large = sum(1 for c in cards if c.get("imageLarge") or c.get("imageUrlLarge"))
    missing = sum(
        1
        for c in cards
        if not (c.get("imageSmall") or c.get("imageUrlSmall") or c.get("imageLarge") or c.get("imageUrlLarge"))
    )
    return {"total": len(cards), "withSmall": small, "withLarge": large, "missing": missing}


def merge_set_into_sets_json(
    *,
    language: str,
    set_record: dict[str, Any],
    card_count_delta: int,
    source: str,
) -> dict[str, Any]:
    sets_path = CATALOG_ROOT / language / "sets.json"
    payload = json.loads(sets_path.read_text(encoding="utf-8"))
    sets = payload.get("sets") if isinstance(payload.get("sets"), list) else []
    set_id = str(set_record.get("id") or "")
    replaced = False
    for idx, existing in enumerate(sets):
        if str(existing.get("id") or "") == set_id:
            sets[idx] = set_record
            replaced = True
            break
    if not replaced:
        sets.append(set_record)
    sets.sort(key=lambda item: (str(item.get("releaseDate") or ""), str(item.get("id") or "")))
    payload["sets"] = sets
    payload["setCount"] = len(sets)
    payload["cardCount"] = int(payload.get("cardCount") or 0) + (0 if replaced else card_count_delta)
    if replaced:
        # Recompute cardCount from card files for accuracy when replacing.
        cards_dir = CATALOG_ROOT / language / "cards"
        total_cards = 0
        for path in cards_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                total_cards += int(data.get("cardCount") or len(data.get("cards") or []))
            except Exception:  # noqa: BLE001
                continue
        payload["cardCount"] = total_cards
    payload["generatedAtUtc"] = now_utc()
    payload["source"] = payload.get("source") or source
    write_json(sets_path, payload)
    return payload


def import_en_set(set_id: str, *, expected_total: int | None, force_partial: bool) -> dict[str, Any]:
    ts = now_utc()
    try:
        set_data = fetch_pokemon_tcg_set(set_id)
    except Exception as exc:  # noqa: BLE001
        # Set detail endpoint can 500 while cards query works.
        cards = fetch_pokemon_tcg_cards(set_id)
        if not cards:
            raise
        set_meta = cards[0].get("set") if isinstance(cards[0].get("set"), dict) else {}
        set_data = {
            "id": set_id,
            "name": set_meta.get("name") or set_id,
            "series": set_meta.get("series"),
            "printedTotal": set_meta.get("printedTotal"),
            "total": set_meta.get("total") or len(cards),
            "releaseDate": set_meta.get("releaseDate"),
            "updatedAt": set_meta.get("updatedAt"),
            "ptcgoCode": set_meta.get("ptcgoCode"),
            "images": set_meta.get("images") if isinstance(set_meta.get("images"), dict) else {},
        }
        print(f"  [WARN] set detail failed for {set_id} ({exc}); used card-embedded set meta")
    else:
        cards = fetch_pokemon_tcg_cards(set_id)

    set_name = str(set_data.get("name") or set_id)
    records = [build_catalog_card_record(card, set_id, set_name) for card in cards]
    records.sort(key=catalogue_card_sort_key)

    provider_total = set_data.get("total")
    complete = (
        not force_partial
        and isinstance(provider_total, int)
        and len(records) == provider_total
        and (expected_total is None or len(records) >= expected_total)
    )
    status = "built" if complete else "partial_built"

    card_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "game": "pokemon",
        "language": "en",
        "setId": set_id,
        "setName": set_name,
        "source": SOURCE_ID_POKEMON_TCG_API,
        "catalogueStatus": status,
        "cardCount": len(records),
        "cards": records,
        "notes": [
            "Imported by tools/import_en_jp_catalogue_freshness.py from Pokemon TCG API.",
            f"completeness={status}",
        ],
    }
    card_path = CATALOG_ROOT / "en" / "cards" / f"{set_id}.json"
    write_json(card_path, card_payload)

    set_record = build_catalog_set_record(set_data)
    set_record["promotionSource"] = "en_jp_catalogue_freshness_gapfill"
    merge_set_into_sets_json(
        language="en",
        set_record=set_record,
        card_count_delta=len(records),
        source=SOURCE_ID_POKEMON_TCG_API,
    )

    identities = [c.get("canonicalBaseId") for c in records]
    collisions = len(identities) - len(set(identities))
    return {
        "language": "en",
        "setId": set_id,
        "setName": set_name,
        "releaseDate": set_data.get("releaseDate"),
        "source": SOURCE_ID_POKEMON_TCG_API,
        "catalogueStatus": status,
        "imported": len(records),
        "providerTotal": provider_total,
        "printedTotal": set_data.get("printedTotal"),
        "expectedFloor": expected_total,
        "images": image_coverage(records),
        "collisions": collisions,
        "cardPath": str(card_path.relative_to(ROOT)).replace("\\", "/"),
    }


def pokewallet_provider_set(set_id: str, set_code: str, name: str, language: str, app_language: str) -> ProviderSet:
    return ProviderSet(
        set_id=set_id,
        set_code=set_code,
        name=name,
        language=language,
        app_language=app_language,
        card_count=None,
        release_date=None,
    )


def import_jp_pokewallet_set(
    *,
    provider_set_id: str,
    set_code: str,
    set_name: str,
    expected_total: int | None,
    api_key: str,
) -> dict[str, Any]:
    ts = now_utc()
    set_item = pokewallet_provider_set(provider_set_id, set_code, set_name, "jap", "jp")
    diag: dict[str, Any] = {
        "requestsAttempted": 0,
        "requestsSucceeded": 0,
        "requestsFailed": 0,
        "sampleSkipped": [],
        "status": "ok",
        "blockerReason": None,
    }
    config = {"requestSleepSeconds": 0.15, "fullCatalogue": {"requestSleepSeconds": 0.15}}

    # Seed request counter compatible with foundation helpers.
    cards, rate_limited = fetch_set_cards(
        api_key=api_key,
        set_item=set_item,
        config=config,
        diag=diag,
        max_requests=200,
        full=True,
    )
    if rate_limited:
        raise RuntimeError(f"PokéWallet rate limited while fetching {set_code}")
    if not cards:
        raise RuntimeError(f"PokéWallet returned no cards for {set_code} ({provider_set_id})")

    # Write provider file for lineage (same shape as foundation export).
    provider_payload = {
        "schemaVersion": "1.0.0",
        "generatedAtUtc": ts,
        "game": "pokemon",
        "provider": "pokewallet",
        "cardScanRLanguage": "jp",
        "providerLanguage": "jap",
        "providerSetId": provider_set_id,
        "providerSetCode": set_code,
        "providerSetName": set_name,
        "cardCount": len(cards),
        "cards": cards,
        "imageReferencesOnly": True,
    }
    provider_path = PROVIDER_CARDS / "jp" / f"{set_code}.json"
    write_json(provider_path, provider_payload)

    app_set_id = safe_set_id(set_code, language="jp")
    app_cards: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    seen_identity: set[str] = set()
    collisions = 0

    for card in cards:
        record = ProviderRecord(
            language="jp",
            path=provider_path,
            file_set_code=set_code,
            file_set_id=provider_set_id,
            file_set_name=set_name,
            card=card,
        )
        candidate, reason = build_candidate(
            record,
            app_set_map={app_set_id: app_set_id, set_code.lower(): app_set_id, set_code: app_set_id},
            enabled_languages={"jp"},
        )
        if candidate is None:
            blocked.append(
                {
                    "reason": reason,
                    "providerCardId": card.get("providerCardId"),
                    "cardNumber": card.get("cardNumber"),
                    "name": card.get("name"),
                }
            )
            continue
        # Force set identity to provider set code (m6 / m6a).
        identity_key = make_identity_key(
            "jp",
            app_set_id,
            candidate.collector_number,
            candidate.normalized_name,
            candidate.variant_key,
        )
        candidate = replace(
            candidate,
            app_set_id=app_set_id,
            app_set_name=set_name,
            identity_key=identity_key,
            canonical_base_id=f"pokemon|jp|{app_set_id}|{candidate.collector_number}|{candidate.normalized_name}",
        )
        if candidate.identity_key in seen_identity:
            collisions += 1
            continue
        seen_identity.add(candidate.identity_key)
        app_card = build_app_card(candidate)
        app_card["setName"] = set_name
        app_cards.append(app_card)

    app_cards.sort(key=lambda c: (normalize_number(c.get("collectorNumber")), str(c.get("name") or "")))

    complete = (
        expected_total is not None
        and len(app_cards) >= expected_total
        and not blocked
    )
    # Prefer conservative: if expected known and short, partial; if no expected, partial unless blocked empty and cards match provider.
    if expected_total is None:
        complete = len(blocked) == 0 and len(app_cards) == len(cards)
    status = "built" if complete else "partial_built"

    card_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "game": "pokemon",
        "language": "jp",
        "setId": app_set_id,
        "setName": set_name,
        "source": PROMOTION_SOURCE,
        "catalogueStatus": status,
        "cardCount": len(app_cards),
        "cards": app_cards,
        "notes": [
            "Imported by tools/import_en_jp_catalogue_freshness.py from PokéWallet.",
            f"providerSetId={provider_set_id}",
            f"completeness={status}",
            f"blocked={len(blocked)}",
        ],
    }
    card_path = CATALOG_ROOT / "jp" / "cards" / f"{app_set_id}.json"
    write_json(card_path, card_payload)

    set_record = {
        "id": app_set_id,
        "name": set_name,
        "series": None,
        "printedTotal": None,
        "total": len(app_cards),
        "releaseDate": None,
        "updatedAt": None,
        "ptcgoCode": set_code,
        "symbolUrl": None,
        "logoUrl": None,
        "imageSource": PROMOTION_SOURCE,
        "imageCached": False,
        "promotionSource": PROMOTION_DETAIL_SOURCE,
    }
    # Attach known release dates for auditability.
    if app_set_id == "m6":
        set_record["releaseDate"] = "2026/07/31"
        set_record["name"] = "ストームエメラルダ"
        set_record["printedTotal"] = 76
    elif app_set_id == "m6a":
        set_record["releaseDate"] = "2026/09/16"
        set_record["name"] = "30th CELEBRATION"
    merge_set_into_sets_json(
        language="jp",
        set_record=set_record,
        card_count_delta=len(app_cards),
        source=PROMOTION_SOURCE,
    )

    return {
        "language": "jp",
        "setId": app_set_id,
        "setName": set_record["name"],
        "providerSetId": provider_set_id,
        "providerSetCode": set_code,
        "source": PROMOTION_SOURCE,
        "catalogueStatus": status,
        "imported": len(app_cards),
        "providerCards": len(cards),
        "blocked": len(blocked),
        "blockedSample": blocked[:20],
        "expectedFloor": expected_total,
        "images": image_coverage(app_cards),
        "collisions": collisions,
        "cardPath": str(card_path.relative_to(ROOT)).replace("\\", "/"),
        "providerPath": str(provider_path.relative_to(ROOT)).replace("\\", "/"),
        "rateLimited": rate_limited,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import EN/JP catalogue freshness gaps.")
    parser.add_argument("--en-only", action="store_true")
    parser.add_argument("--jp-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and report without writing catalogue files.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    do_en = not args.jp_only
    do_jp = not args.en_only
    report: dict[str, Any] = {
        "generatedAtUtc": now_utc(),
        "dryRun": bool(args.dry_run),
        "en": [],
        "jp": [],
        "errors": [],
    }

    if args.dry_run:
        print("[dry-run] fetch-only mode is not fully implemented; refusing to skip writes. Use without --dry-run.")
        return 2

    if do_en:
        print("Importing EN me5 (Pitch Black)")
        report["en"].append(import_en_set("me5", expected_total=120, force_partial=False))
        print("Importing EN me55 (30th Celebration)")
        # Official expansion 199 includes classic(30)+energy(8). me55 alone should be 161; mark partial vs full expansion 169 without classic.
        report["en"].append(import_en_set("me55", expected_total=169, force_partial=True))
        print("Importing EN me55c (30th Classic Collection)")
        report["en"].append(import_en_set("me55c", expected_total=30, force_partial=False))

    if do_jp:
        api_key = os.environ.get("POKEWALLET_API_KEY", "").strip()
        if not api_key:
            report["errors"].append("POKEWALLET_API_KEY missing")
            print("ERROR: POKEWALLET_API_KEY missing", file=sys.stderr)
            write_json(REPORT_PATH, report)
            return 1
        print("Importing JP m6 (Storm Emeralda)")
        report["jp"].append(
            import_jp_pokewallet_set(
                provider_set_id="24791",
                set_code="m6",
                set_name="Storm Emeralda",
                expected_total=113,  # TCGdex total; PokéWallet may be higher with variants
                api_key=api_key,
            )
        )
        print("Importing JP m6a (30th CELEBRATION)")
        report["jp"].append(
            import_jp_pokewallet_set(
                provider_set_id="24721",
                set_code="m6a",
                set_name="30th Celebration - Japanese",
                expected_total=176,  # press/community JP total; treat shortfalls as partial
                api_key=api_key,
            )
        )

    write_json(REPORT_PATH, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
