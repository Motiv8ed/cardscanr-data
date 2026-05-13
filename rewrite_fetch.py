#!/usr/bin/env python3
"""
Rewrite fetch_english_catalogue to use set-by-set strategy
"""
import re

# Read the file
with open('tools/build_price_cache.py', 'r') as f:
    content = f.read()

# Find the start and end of fetch_english_catalogue function
fetch_en_start = content.find('def fetch_english_catalogue(')
if fetch_en_start == -1:
    print("ERROR: Could not find fetch_english_catalogue")
    exit(1)

# Find the start of the next function (fetch_japanese_catalogue)
fetch_jp_start = content.find('def fetch_japanese_catalogue(', fetch_en_start)
if fetch_jp_start == -1:
    print("ERROR: Could not find fetch_japanese_catalogue")
    exit(1)

# Extract the part before fetch_english_catalogue, and after fetch_japanese_catalogue
before = content[:fetch_en_start]
after = content[fetch_jp_start:]

# The helper function PLUS the new set-by-set implementation
new_functions = '''def _fetch_cards_for_set(
    set_id: str,
    set_meta: dict,
    headers: dict,
    page_size: int,
    max_pages_per_set: int,
    ts: str,
    include_current_prices: bool,
) -> tuple[list[dict], list[dict], dict[str, object]]:
    """Fetch all cards for a single set from PokemonTCG API with error handling."""
    status = {
        "success": False,
        "cardCount": 0,
        "pagesFetched": 0,
        "reason": None,
    }
    cards = []
    current_price_rows = []
    seen_base_ids: set[str] = set()

    page = 1
    while page <= max_pages_per_set:
        try:
            response = requests.get(
                "https://api.pokemontcg.io/v2/cards",
                params={"q": f"set.id:{set_id}", "page": page, "pageSize": page_size, "orderBy": "number"},
                headers=headers,
                timeout=25,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            status_code = getattr(exc.response, 'status_code', None) if hasattr(exc, 'response') else 'error'
            status["reason"] = f"http_{status_code}"
            break

        rows = payload.get("data", [])
        if not isinstance(rows, list) or not rows:
            status["success"] = True
            break

        # Process each row
        for row in rows:
            if not isinstance(row, dict):
                continue
            card = build_card_record(row, "pokemon", "en", "pokemon_tcg_api", include_current_prices)
            if card:
                cards.append(card)
                base_id = card.get("canonicalBaseId", "")
                if base_id:
                    seen_base_ids.add(base_id)
                if include_current_prices:
                    price_row = extract_current_price_row(card, row)
                    if price_row:
                        current_price_rows.append(price_row)

        status["pagesFetched"] += 1
        status["cardCount"] += len(rows)
        page += 1
        time.sleep(0.05)

    if not status["reason"]:
        status["success"] = True
        status["reason"] = "completed"

    return cards, current_price_rows, status


def fetch_english_catalogue(
    *,
    ts: str,
    config: dict,
    allow_full_fetch: bool,
    include_current_prices: bool,
) -> tuple[dict | None, dict[str, dict], dict[str, dict], int, int, dict[str, object]]:
    if not allow_full_fetch:
        return None, {}, {}, 0, 0, {"catalogueEnStatus": "not_built_yet"}

    if not config.get("buildEnglishFromPokemonTcgApi", True):
        return None, {}, {}, 0, 0, {"catalogueEnStatus": "not_built_yet"}

    page_size = int(config.get("pageSize", 250))
    max_pages_per_set = int(config.get("maxPagesPerSet", 50))
    max_sets = int(config.get("maxSetsPerRun", 9999))
    continue_on_set_error = bool(config.get("continueOnSetError", True))
    skip_sets_on_error = bool(config.get("skipSetsOnError", True))
    catalogue_sleep = float(config.get("catalogueRequestSleepSeconds", 0.15))

    headers = {}
    api_key = os.getenv("POKEMON_TCG_API_KEY")
    if api_key:
        headers["X-Api-Key"] = api_key

    diagnostics = {
        "catalogueEnFetchStrategy": "set_by_set",
        "catalogueEnStatus": "not_built_yet",
        "catalogueEnSetCount": 0,
        "catalogueEnSetsAttempted": 0,
        "catalogueEnSetsBuilt": 0,
        "catalogueEnSetsPartial": 0,
        "catalogueEnSetsFailed": 0,
        "catalogueEnCardsFetched": 0,
        "catalogueEnExpectedTotalFromSets": None,
        "catalogueEnFailedSetIds": [],
        "catalogueEnPartialSetIds": [],
        "catalogueEnStoppedReason": None,
        "catalogueEnPartialReason": None,
    }

    # Fetch set metadata first.
    sets_by_id: dict[str, dict] = {}
    page = 1
    max_pages = int(config.get("maxPagesPerRun", 1000))
    while page <= max_pages:
        try:
            response = requests.get(
                "https://api.pokemontcg.io/v2/sets",
                params={"page": page, "pageSize": page_size, "orderBy": "id"},
                headers=headers,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            print(f"[WARN] EN sets fetch failed on page {page}: {exc}")
            diagnostics["catalogueEnPartialReason"] = f"sets_fetch_failed: {type(exc).__name__}"
            break

        rows = payload.get("data", [])
        if not isinstance(rows, list) or not rows:
            break

        for row in rows:
            if not isinstance(row, dict):
                continue
            set_id = str(row.get("id", "")).strip()
            if not set_id:
                continue
            sets_by_id[set_id] = {
                "setId": set_id,
                "name": row.get("name"),
                "series": row.get("series"),
                "printedTotal": row.get("printedTotal"),
                "total": row.get("total"),
                "releaseDate": row.get("releaseDate"),
                "updatedAt": row.get("updatedAt"),
                "source": "pokemon_tcg_api",
            }

        total_count = payload.get("totalCount")
        count = payload.get("count", len(rows))
        print(f"  [catalog en] sets page {page} fetched ({count} rows)")

        if isinstance(total_count, int) and total_count > 0 and len(sets_by_id) >= total_count:
            break
        if int(count) <= 0:
            break
        page += 1
        time.sleep(0.06)

    diagnostics["catalogueEnSetCount"] = len(sets_by_id)
    if not sets_by_id:
        diagnostics["catalogueEnStatus"] = "not_built_yet"
        return None, {}, {}, 0, 0, diagnostics

    # Fetch cards set by set.
    card_files: dict[str, dict] = {}
    current_prices_by_set: dict[str, list[dict]] = {}
    sets_built: list[str] = []
    sets_partial: list[str] = []
    sets_failed: list[str] = []
    total_card_count = 0

    for i, set_id in enumerate(sorted(sets_by_id.keys())[:max_sets], start=1):
        diagnostics["catalogueEnSetsAttempted"] += 1
        set_meta = sets_by_id[set_id]

        cards, price_rows, status = _fetch_cards_for_set(
            set_id=set_id,
            set_meta=set_meta,
            headers=headers,
            page_size=page_size,
            max_pages_per_set=max_pages_per_set,
            ts=ts,
            include_current_prices=include_current_prices,
        )

        if not cards:
            if status["success"]:
                print(f"  [catalog en] set {set_id}: no cards found")
                sets_partial.append(set_id)
                diagnostics["catalogueEnSetsPartial"] += 1
            else:
                if skip_sets_on_error:
                    print(f"  [catalog en] set {set_id}: FAILED ({status['reason']})")
                    sets_failed.append(set_id)
                    diagnostics["catalogueEnSetsFailed"] += 1
                    if not continue_on_set_error:
                        diagnostics["catalogueEnStoppedReason"] = f"set_{set_id}_failed"
                        break
                else:
                    sets_partial.append(set_id)
                    diagnostics["catalogueEnSetsPartial"] += 1
        else:
            if status["success"] and status["reason"] == "completed":
                sets_built.append(set_id)
                diagnostics["catalogueEnSetsBuilt"] += 1
                print(f"  [catalog en] set {set_id}: {len(cards)} cards")
            else:
                sets_partial.append(set_id)
                diagnostics["catalogueEnSetsPartial"] += 1
                print(f"  [catalog en] set {set_id}: {len(cards)} cards (partial, {status['reason']})")

            # Sort cards and create card file for this set.
            cards_sorted = sorted(
                cards,
                key=lambda c: (normalize_number(c.get("collectorNumber", "0")), str(c.get("collectorNumber", ""))),
            )
            card_files[set_id] = {
                "schemaVersion": SCHEMA_VERSION,
                "generatedAtUtc": ts,
                "game": "pokemon",
                "language": "en",
                "setId": set_id,
                "setName": set_meta.get("name"),
                "source": "pokemon_tcg_api",
                "catalogueStatus": "built" if status["success"] and status["reason"] == "completed" else "partial_built",
                "cardCount": len(cards_sorted),
                "cards": cards_sorted,
            }

            if price_rows:
                unique: dict[str, dict] = {}
                for row in price_rows:
                    unique[row["canonicalId"]] = row
                rows_sorted = sorted(unique.values(), key=lambda r: r["canonicalId"])
                current_prices_by_set[set_id] = rows_sorted

            total_card_count += len(cards)

        if i % 50 == 0:
            print(f"  [catalog en] processed {i}/{len(sets_by_id)} sets so far...")
        time.sleep(catalogue_sleep)

    diagnostics["catalogueEnCardsFetched"] = total_card_count
    diagnostics["catalogueEnFailedSetIds"] = sets_failed
    diagnostics["catalogueEnPartialSetIds"] = sets_partial

    # Determine overall status.
    if diagnostics["catalogueEnSetsFailed"] == 0 and diagnostics["catalogueEnSetsPartial"] == 0:
        diagnostics["catalogueEnStatus"] = "built"
    elif diagnostics["catalogueEnSetsBuilt"] > 0:
        diagnostics["catalogueEnStatus"] = "partial_built"
    else:
        diagnostics["catalogueEnStatus"] = "not_built_yet"

    if not card_files:
        return None, {}, {}, 0, 0, diagnostics

    # Build current price files.
    current_price_files: dict[str, dict] = {}
    for set_id, price_rows in current_prices_by_set.items():
        rows_sorted = sorted(price_rows, key=lambda r: r["canonicalId"])
        current_price_files[set_id] = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAtUtc": ts,
            "game": "pokemon",
            "language": "en",
            "setId": set_id,
            "source": "pokemon_tcg_api",
            "priceType": "latest_known_current",
            "prices": rows_sorted,
        }

    # Build sets.json with updated metadata.
    sets_rows = [sets_by_id[sid] for sid in sorted(sets_by_id.keys()) if sid in card_files]
    sets_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAtUtc": ts,
        "game": "pokemon",
        "language": "en",
        "catalogueStatus": diagnostics["catalogueEnStatus"],
        "cardsAvailable": True,
        "source": "pokemon_tcg_api",
        "setCount": len(sets_rows),
        "cardCount": total_card_count,
        "partialSetCount": len(sets_partial),
        "failedSetCount": len(sets_failed),
        "sets": sets_rows,
    }

    return (
        sets_payload,
        card_files,
        current_price_files,
        len(sets_rows),
        total_card_count,
        diagnostics,
    )


'''

# Write the new file
with open('tools/build_price_cache.py', 'w') as f:
    f.write(before + new_functions + after)

print("Successfully rewrote fetch_english_catalogue with _fetch_cards_for_set helper")
