#!/usr/bin/env python3
"""Lightweight tests for local updater budget/rate-limit helpers."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_local_price_update as updater  # noqa: E402
import build_price_cache as builder  # noqa: E402
import validate_cache as validator  # noqa: E402


def test_budget_usage_and_stop_logic() -> None:
    now = datetime.now(timezone.utc)
    state = {
        "requestLedger": [
            {"timestampUtc": (now - timedelta(minutes=20)).isoformat().replace("+00:00", "Z"), "requests": 40},
            {"timestampUtc": (now - timedelta(minutes=50)).isoformat().replace("+00:00", "Z"), "requests": 30},
            {"timestampUtc": (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z"), "requests": 200},
        ]
    }
    budget = {"hourlyTarget": 90, "dailyTarget": 950}
    should_stop, reason, hourly_rem, daily_rem = updater.should_stop_for_budget(state, budget)
    assert should_stop is False
    assert reason == "none"
    assert hourly_rem == 20
    assert daily_rem == 680


def test_cycle_request_cap_uses_safety_buffer() -> None:
    now = datetime.now(timezone.utc)
    state = {
        "requestLedger": [
            {"timestampUtc": (now - timedelta(minutes=20)).isoformat().replace("+00:00", "Z"), "requests": 15},
            {"timestampUtc": (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z"), "requests": 30},
        ]
    }
    budget = {
        "hourlyTarget": 90,
        "dailyTarget": 950,
        "safetyBuffer": 10,
    }
    cycle_cap, hourly_remaining, daily_remaining = updater.calculate_cycle_request_cap(state, budget)
    assert cycle_cap == 65
    assert hourly_remaining == 65
    assert daily_remaining == 895


def test_updater_does_not_start_cycle_when_cap_is_exhausted() -> None:
    assert updater.should_start_current_price_cycle(0) is False
    assert updater.should_start_current_price_cycle(-3) is False


def test_updater_passes_request_cap_to_builder_env() -> None:
    env = updater.build_current_price_builder_env({"A": "1"}, 5, 17)
    assert env["A"] == "1"
    assert env["CARDSCANR_CURRENT_PRICE_BATCH_SIZE"] == "5"
    assert env["CARDSCANR_CURRENT_PRICE_REQUEST_CAP"] == "17"


def test_pokewallet_api_key_resolution_prefers_cardscanr_alias() -> None:
    original_cardscanr = os.environ.get("CARDSCANR_POKEWALLET_API_KEY")
    original_primary = os.environ.get("POKEWALLET_API_KEY")
    try:
        os.environ["CARDSCANR_POKEWALLET_API_KEY"] = "alias-key"
        os.environ["POKEWALLET_API_KEY"] = "primary-key"
        assert builder.resolve_pokewallet_api_key() == "alias-key"
    finally:
        if original_cardscanr is None:
            os.environ.pop("CARDSCANR_POKEWALLET_API_KEY", None)
        else:
            os.environ["CARDSCANR_POKEWALLET_API_KEY"] = original_cardscanr
        if original_primary is None:
            os.environ.pop("POKEWALLET_API_KEY", None)
        else:
            os.environ["POKEWALLET_API_KEY"] = original_primary


def test_price_provider_priority_prefers_pokewallet_when_configured() -> None:
    original_priority = os.environ.get("CARDSCANR_PRICE_PROVIDER_PRIORITY")
    original_use_flag = os.environ.get("CARDSCANR_USE_POKEWALLET_PRICES")
    try:
        os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = "pokewallet,pokemon_tcg_api"
        os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = "true"
        priority = builder.resolve_price_provider_priority({})
        assert priority[0] == "pokewallet"
        assert builder.should_use_pokewallet_prices({}) is True
    finally:
        if original_priority is None:
            os.environ.pop("CARDSCANR_PRICE_PROVIDER_PRIORITY", None)
        else:
            os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = original_priority
        if original_use_flag is None:
            os.environ.pop("CARDSCANR_USE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = original_use_flag


def test_validate_cache_accepts_pokewallet_for_en_current_prices() -> None:
    original_errors = list(validator.errors)
    try:
        validator.errors.clear()
        validator.validate_en_current_price_source(
            "pokewallet",
            "public/v1/prices/current/pokemon/en/bwp.json",
            {"pokemon_tcg_api", "pokewallet"},
        )
        assert validator.errors == []
    finally:
        validator.errors[:] = original_errors


def test_validate_cache_rejects_unknown_en_current_price_source() -> None:
    original_errors = list(validator.errors)
    original_err = validator.err
    try:
        validator.errors.clear()
        validator.err = lambda msg: validator.errors.append(f"ERROR: {msg}")
        validator.validate_en_current_price_source(
            "not_a_source",
            "public/v1/prices/current/pokemon/en/bwp.json",
            {"pokemon_tcg_api", "pokewallet"},
        )
        assert any("source must be one of" in item for item in validator.errors)
    finally:
        validator.err = original_err
        validator.errors[:] = original_errors


def test_validate_cache_accepts_pokemon_tcg_api_fallback_source() -> None:
    original_errors = list(validator.errors)
    try:
        validator.errors.clear()
        validator.validate_en_current_price_source(
            "pokemon_tcg_api",
            "public/v1/prices/current/pokemon/en/base1.json",
            {"pokemon_tcg_api", "pokewallet"},
        )
        assert validator.errors == []
    finally:
        validator.errors[:] = original_errors


def test_daily_budget_stop() -> None:
    now = datetime.now(timezone.utc)
    state = {
        "requestLedger": [
            {"timestampUtc": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"), "requests": 100},
            {"timestampUtc": (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z"), "requests": 860},
        ]
    }
    budget = {"hourlyTarget": 90, "dailyTarget": 950}
    should_stop, reason, _, daily_rem = updater.should_stop_for_budget(state, budget)
    assert should_stop is True
    assert reason == "daily_budget_exhausted"
    assert daily_rem == 0


def test_all_day_hourly_exhaustion_returns_sleep_window() -> None:
    now = datetime.now(timezone.utc)
    oldest = now - timedelta(minutes=59, seconds=40)
    state = {
        "requestLedger": [
            {"timestampUtc": oldest.isoformat().replace("+00:00", "Z"), "requests": 90},
        ]
    }
    budget = {"hourlyTarget": 90, "dailyTarget": 990}
    snapshot = updater.build_budget_snapshot(state, budget, now)
    sleep_seconds, reason = updater.next_safe_wake_seconds(snapshot)
    assert reason == "hourly_budget_exhausted"
    assert sleep_seconds > 0
    assert sleep_seconds <= 3600


def test_all_day_daily_exhaustion_stops_without_sleep() -> None:
    now = datetime.now(timezone.utc)
    state = {
        "requestLedger": [
            {"timestampUtc": (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z"), "requests": 990},
        ]
    }
    budget = {"hourlyTarget": 90, "dailyTarget": 990}
    snapshot = updater.build_budget_snapshot(state, budget, now)
    sleep_seconds, reason = updater.next_safe_wake_seconds(snapshot)
    assert reason == "daily_budget_exhausted"
    assert sleep_seconds > 0


def test_should_commit_changes_requires_validation_and_diffs() -> None:
    assert updater.should_commit_changes(True, ["public/v1/index.json"], True) is True
    assert updater.should_commit_changes(True, ["public/v1/index.json"], False) is False
    assert updater.should_commit_changes(True, [], True) is False
    assert updater.should_commit_changes(False, ["public/v1/index.json"], True) is False


def test_should_push_changes_requires_commit() -> None:
    assert updater.should_push_changes(True, True) is True
    assert updater.should_push_changes(True, False) is False
    assert updater.should_push_changes(False, True) is False


def test_pokewallet_bwp_maps_via_override_when_present() -> None:
    set_data = {
        "id": "bwp",
        "name": "BW Black Star Promos",
        "ptcgoCode": "PR-BLW",
        "printedTotal": 101,
        "releaseDate": "2011/03/01",
        "language": "en",
    }
    set_map = {
        "blackandwhitepromos": [
            {
                "providerSetCode": "PR",
                "providerSetId": "1407",
                "providerSetName": "Black and White Promos",
                "language": "en",
                "cardCount": 98,
                "releaseDate": "25th April, 2011",
                "lookupName": "blackandwhitepromos",
            }
        ]
    }
    match = builder.resolve_pokewallet_set_match(set_data, set_map)
    assert match["matchedCode"] == "PR"
    assert match["reason"] in {"override_name_match", "scored_match"}


def test_pokewallet_mapping_rejects_ambiguous_candidates() -> None:
    set_data = {
        "id": "bwp",
        "name": "BW Black Star Promos",
        "ptcgoCode": "PR-BLW",
        "printedTotal": 100,
        "releaseDate": "2011/03/01",
        "language": "en",
    }
    set_map = {
        "blackandwhitepromos": [
            {
                "providerSetCode": "PRA",
                "providerSetId": "1",
                "providerSetName": "Black and White Promos",
                "language": "en",
                "cardCount": 100,
                "releaseDate": "1st March, 2011",
                "lookupName": "blackandwhitepromos",
            },
            {
                "providerSetCode": "PRB",
                "providerSetId": "2",
                "providerSetName": "Black and White Promos",
                "language": "en",
                "cardCount": 100,
                "releaseDate": "1st March, 2011",
                "lookupName": "blackandwhitepromos",
            },
        ]
    }
    match = builder.resolve_pokewallet_set_match(set_data, set_map)
    assert match["matchedCode"] is None
    assert "ambiguous" in str(match["reason"])


def test_pokewallet_exact_code_match_wins() -> None:
    set_data = {
        "id": "base1",
        "name": "Base",
        "ptcgoCode": "BS",
        "printedTotal": 102,
        "releaseDate": "1999/01/09",
        "language": "en",
    }
    set_map = {
        "baseset": [
            {
                "providerSetCode": "BS",
                "providerSetId": "604",
                "providerSetName": "Base Set",
                "language": "en",
                "cardCount": 101,
                "releaseDate": "9th January, 1999",
                "lookupName": "baseset",
            },
            {
                "providerSetCode": "BSS",
                "providerSetId": "1663",
                "providerSetName": "Base Set (Shadowless)",
                "language": "en",
                "cardCount": 101,
                "releaseDate": "9th January, 1999",
                "lookupName": "basesetshadowless",
            },
        ]
    }
    match = builder.resolve_pokewallet_set_match(set_data, set_map)
    assert match["matchedCode"] == "BS"
    assert match["reason"] == "exact_code_match"


def test_pokewallet_normalized_name_match_works() -> None:
    set_data = {
        "id": "gymheroes_local",
        "name": "Gym Heroes",
        "ptcgoCode": "GHERO",
        "printedTotal": 132,
        "releaseDate": "2000/08/14",
        "language": "en",
    }
    set_map = {
        "gymheroes": [
            {
                "providerSetCode": "GYM1",
                "providerSetId": "777",
                "providerSetName": "Gym Heroes",
                "language": "en",
                "cardCount": 132,
                "releaseDate": "14th August, 2000",
                "lookupName": "gymheroes",
            }
        ]
    }
    match = builder.resolve_pokewallet_set_match(set_data, set_map)
    assert match["matchedCode"] == "GYM1"
    assert match["reason"] in {"normalized_name_match", "scored_match"}


def test_builder_uses_pokewallet_current_price_source_when_available() -> None:
    original_get_detailed = builder.pokewallet_get_detailed
    original_map_loader = builder.load_pokewallet_set_code_map
    original_cap = builder.CURRENT_PRICE_REQUEST_CAP
    original_tracker = dict(builder.REQUEST_TRACKER)
    original_use_flag = os.environ.get("CARDSCANR_USE_POKEWALLET_PRICES")
    original_api_key = os.environ.get("CARDSCANR_POKEWALLET_API_KEY")
    try:
        os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = "true"
        os.environ["CARDSCANR_POKEWALLET_API_KEY"] = "test-key"
        builder.reset_request_tracker()
        builder.CURRENT_PRICE_REQUEST_CAP = 10

        def fake_pokewallet_get_detailed(endpoint: str, api_key: str, params=None):
            assert api_key == "test-key"
            assert endpoint == "prices/BS"
            return ({
                "data": [
                    {
                        "card_number": "1",
                        "name": "Test Card",
                        "tcgplayer": {
                            "prices": {
                                "normal": {"market": 1.23, "low": 1.0, "high": 1.5}
                            }
                        },
                    }
                ]
            }, 200)

        builder.pokewallet_get_detailed = fake_pokewallet_get_detailed
        builder.load_pokewallet_set_code_map = lambda: {
            "baseset": [
                {
                    "providerSetCode": "BS",
                    "providerSetName": "Base Set",
                    "cardCount": 102,
                }
            ]
        }

        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            catalog_sets = {
                "catalogueStatus": "built",
                "sets": [
                    {"id": "base1", "name": "Base Set", "printedTotal": 102},
                ],
            }
            written_files, metrics, next_state = builder.build_english_current_prices_by_set(
                "2026-05-21T00:00:00Z",
                {
                    "buildCurrentPricesFromPokemonTcgApi": True,
                    "scheduledCurrentPriceBatchEnabled": False,
                    "continueOnSetError": True,
                    "usePokewalletPrices": True,
                },
                catalog_sets,
                output_dir,
                "current_prices",
                {"enCurrentPriceCursor": 0},
                fail_after_set_count=0,
            )

            assert [item[0] for item in written_files] == ["base1"]
            assert metrics["currentPriceEnSource"] == "pokewallet"
            assert metrics["currentPriceEnProviderUsed"] == ["pokewallet"]
            assert metrics["currentPriceEnFallbackReasons"] == []
            assert metrics["currentPriceEnSetsWritten"] == 1
            assert metrics["currentPriceEnPriceRecordsWritten"] == 1
            assert next_state["lastStopReason"] == "completed"
            assert (output_dir / "base1.json").exists()
            payload = builder.load_json(output_dir / "base1.json")
            assert payload["source"] == "pokewallet"
            assert payload["prices"][0]["source"] == "pokewallet"
    finally:
        builder.pokewallet_get_detailed = original_get_detailed
        builder.load_pokewallet_set_code_map = original_map_loader
        builder.CURRENT_PRICE_REQUEST_CAP = original_cap
        builder.REQUEST_TRACKER.update(original_tracker)
        if original_use_flag is None:
            os.environ.pop("CARDSCANR_USE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = original_use_flag
        if original_api_key is None:
            os.environ.pop("CARDSCANR_POKEWALLET_API_KEY", None)
        else:
            os.environ["CARDSCANR_POKEWALLET_API_KEY"] = original_api_key


def test_pokewallet_parser_extracts_records_from_flat_price_shape() -> None:
    record = {
        "name": "Zorua - BW12",
        "card_number": "12/99",
        "variant": "Holofoil",
        "tcgplayer": {
            "market_price": 2.34,
            "low_price": 1.9,
            "high_price": 3.1,
        },
        "cardmarket": {},
    }
    set_data = {"id": "bwp", "name": "BW Black Star Promos"}
    catalog_index = {
        "BW12": [
            {"collectorNumber": "BW12", "normalizedName": "zorua"}
        ]
    }
    records, reasons = builder.extract_pokewallet_current_price_records(
        record,
        set_data,
        "2026-05-21T00:00:00Z",
        catalog_index=catalog_index,
    )
    assert reasons == []
    assert len(records) == 1
    assert records[0]["source"] == "pokewallet"
    assert records[0]["collectorNumber"] == "BW12"


def test_pokewallet_parser_rejects_missing_tcgplayer_price() -> None:
    record = {
        "name": "Zorua - BW12",
        "card_number": "12/99",
        "variant": "Holofoil",
        "tcgplayer": {},
        "cardmarket": {"market_price": 1.5},
    }
    set_data = {"id": "bwp", "name": "BW Black Star Promos"}
    catalog_index = {
        "BW12": [
            {"collectorNumber": "BW12", "normalizedName": "zorua"}
        ]
    }
    records, reasons = builder.extract_pokewallet_current_price_records(
        record,
        set_data,
        "2026-05-21T00:00:00Z",
        catalog_index=catalog_index,
    )
    assert records == []
    assert "unsupported_currency" in reasons or "missing_tcgplayer_price" in reasons


def test_pokewallet_duplicate_canonicalids_are_deduped_deterministically() -> None:
    common = {
        "set_id": "bwp",
        "set_name": "BW Black Star Promos",
        "collector_number": "BW29",
        "normalized_name": "victory_cup",
        "variant": "holo",
        "ts": "2026-05-21T00:00:00Z",
        "source": "pokewallet",
        "currency": "USD",
        "market": "us",
        "country": "US",
        "diagnostics_notes": ["pokewallet_tcgplayer_usd"],
    }
    lower_quality = builder.build_current_price_record_from_fields(
        **common,
        pricing={"market": None, "low": 60.0, "high": 90.0},
        confidence="medium",
        provider_diagnostics={"providerId": "low"},
    )
    better_quality = builder.build_current_price_record_from_fields(
        **common,
        pricing={"market": 59.95, "low": 74.9, "high": 100.99},
        confidence="high",
        provider_diagnostics={"providerId": "high"},
    )
    assert lower_quality is not None
    assert better_quality is not None

    deduped, diagnostics = builder.dedupe_pokewallet_current_price_records([lower_quality, better_quality])
    assert len(deduped) == 1
    assert deduped[0]["marketPrice"] == 59.95
    assert diagnostics["dedupedRecords"] == 1
    assert diagnostics["duplicateCanonicalIdCounts"][better_quality["canonicalId"]] == 2


def test_pokewallet_bwp_mocked_duplicate_victory_cup_rows_produce_unique_records() -> None:
    original_get_detailed = builder.pokewallet_get_detailed
    original_map_loader = builder.load_pokewallet_set_code_map
    original_catalog_index_loader = builder.load_catalogue_card_index_for_set
    original_fetch = builder.fetch_pokemon_tcg_paginated
    original_load_existing = builder.load_existing_current_price_files
    original_use_flag = os.environ.get("CARDSCANR_USE_POKEWALLET_PRICES")
    original_api_key = os.environ.get("CARDSCANR_POKEWALLET_API_KEY")
    original_priority = os.environ.get("CARDSCANR_PRICE_PROVIDER_PRIORITY")
    original_require = os.environ.get("CARDSCANR_REQUIRE_POKEWALLET_PRICES")
    try:
        os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = "true"
        os.environ["CARDSCANR_POKEWALLET_API_KEY"] = "test-key"
        os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = "pokewallet,pokemon_tcg_api"
        os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = "true"
        builder.load_existing_current_price_files = lambda language="en": []
        builder.load_pokewallet_set_code_map = lambda: {
            "blackandwhitepromos": [
                {
                    "providerSetCode": "PR",
                    "providerSetId": "1407",
                    "providerSetName": "Black and White Promos",
                    "language": "en",
                    "cardCount": 98,
                    "releaseDate": "25th April, 2011",
                    "lookupName": "blackandwhitepromos",
                }
            ]
        }
        builder.load_catalogue_card_index_for_set = lambda set_id, language="en": {
            "BW29": [{"collectorNumber": "BW29", "normalizedName": "victory_cup"}],
            "BW30": [{"collectorNumber": "BW30", "normalizedName": "victory_cup"}],
            "BW95": [{"collectorNumber": "BW95", "normalizedName": "champions_festival"}],
        }
        builder.fetch_pokemon_tcg_paginated = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pokemon_tcg_api fallback should not run when PokeWallet succeeds")
        )

        builder.pokewallet_get_detailed = lambda endpoint, api_key, params=None: (
            {
                "data": [
                    {
                        "id": "pw-bw29-a",
                        "name": "Victory Cup - BW29",
                        "card_number": "BW29",
                        "variant": "Holofoil",
                        "tcgplayer": {"prices": {"holofoil": {"market": 49.08, "low": 65.0, "high": 660.0}}},
                    },
                    {
                        "id": "pw-bw29-b",
                        "name": "Victory Cup - BW29",
                        "card_number": "BW29",
                        "variant": "Holofoil",
                        "tcgplayer": {"prices": {"holofoil": {"market": 59.95, "low": 74.9, "high": 100.99}}},
                    },
                    {
                        "id": "pw-bw30-a",
                        "name": "Victory Cup - BW30",
                        "card_number": "BW30",
                        "variant": "Holofoil",
                        "tcgplayer": {"prices": {"holofoil": {"market": 70.0, "low": 70.0, "high": 79.94}}},
                    },
                    {
                        "id": "pw-bw30-b",
                        "name": "Victory Cup - BW30",
                        "card_number": "BW30",
                        "variant": "Holofoil",
                        "tcgplayer": {"prices": {"holofoil": {"market": 68.47, "low": 100.0, "high": 9999.0}}},
                    },
                    {
                        "id": "pw-bw95-a",
                        "name": "Champions Festival - BW95",
                        "card_number": "BW95",
                        "variant": "Normal",
                        "tcgplayer": {"prices": {"normal": {"market": 399.0, "low": 399.0, "high": 399.0}}},
                    },
                    {
                        "id": "pw-bw95-b",
                        "name": "Champions Festival - BW95",
                        "card_number": "BW95",
                        "variant": "Normal",
                        "tcgplayer": {"prices": {"normal": {"market": 184.5, "low": 174.99, "high": 300.0}}},
                    },
                ]
            },
            200,
        )

        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            written_files, metrics, _next_state = builder.build_english_current_prices_by_set(
                "2026-05-21T00:00:00Z",
                {
                    "buildCurrentPricesFromPokemonTcgApi": True,
                    "scheduledCurrentPriceBatchEnabled": False,
                    "continueOnSetError": True,
                    "usePokewalletPrices": True,
                },
                {
                    "catalogueStatus": "built",
                    "sets": [{"id": "bwp", "name": "BW Black Star Promos", "printedTotal": 101}],
                },
                output_dir,
                "current_prices",
                {"enCurrentPriceCursor": 0},
                fail_after_set_count=0,
            )

            assert [item[0] for item in written_files] == ["bwp"]
            assert metrics["currentPriceEnSource"] == "pokewallet"
            payload = builder.load_json(output_dir / "bwp.json")
            canonical_ids = [item["canonicalId"] for item in payload["prices"]]
            assert len(canonical_ids) == len(set(canonical_ids))
            assert payload["priceCount"] == 3
            diagnostics = payload["providerDiagnostics"]["pokewallet"]
            assert diagnostics["rawItems"] == 6
            assert diagnostics["usableRecordsBeforeDedupe"] == 6
            assert diagnostics["usableRecordsAfterDedupe"] == 3
            assert diagnostics["dedupedRecords"] == 3
            assert all(count == 2 for count in diagnostics["duplicateCanonicalIdCounts"].values())
    finally:
        builder.pokewallet_get_detailed = original_get_detailed
        builder.load_pokewallet_set_code_map = original_map_loader
        builder.load_catalogue_card_index_for_set = original_catalog_index_loader
        builder.fetch_pokemon_tcg_paginated = original_fetch
        builder.load_existing_current_price_files = original_load_existing
        if original_use_flag is None:
            os.environ.pop("CARDSCANR_USE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = original_use_flag
        if original_api_key is None:
            os.environ.pop("CARDSCANR_POKEWALLET_API_KEY", None)
        else:
            os.environ["CARDSCANR_POKEWALLET_API_KEY"] = original_api_key
        if original_priority is None:
            os.environ.pop("CARDSCANR_PRICE_PROVIDER_PRIORITY", None)
        else:
            os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = original_priority
        if original_require is None:
            os.environ.pop("CARDSCANR_REQUIRE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = original_require


def test_require_mode_fails_when_raw_items_have_no_usable_prices() -> None:
    original_get_detailed = builder.pokewallet_get_detailed
    original_map_loader = builder.load_pokewallet_set_code_map
    original_fetch = builder.fetch_pokemon_tcg_paginated
    original_catalog_index_loader = builder.load_catalogue_card_index_for_set
    original_use_flag = os.environ.get("CARDSCANR_USE_POKEWALLET_PRICES")
    original_api_key = os.environ.get("CARDSCANR_POKEWALLET_API_KEY")
    original_priority = os.environ.get("CARDSCANR_PRICE_PROVIDER_PRIORITY")
    original_require = os.environ.get("CARDSCANR_REQUIRE_POKEWALLET_PRICES")
    try:
        os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = "true"
        os.environ["CARDSCANR_POKEWALLET_API_KEY"] = "test-key"
        os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = "pokewallet,pokemon_tcg_api"
        os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = "true"

        builder.load_pokewallet_set_code_map = lambda: {
            "blackandwhitepromos": [
                {
                    "providerSetCode": "PR",
                    "providerSetId": "1407",
                    "providerSetName": "Black and White Promos",
                    "language": "en",
                    "cardCount": 98,
                    "releaseDate": "25th April, 2011",
                    "lookupName": "blackandwhitepromos",
                }
            ]
        }
        builder.load_catalogue_card_index_for_set = lambda set_id, language="en": {
            "BW12": [{"collectorNumber": "BW12", "normalizedName": "zorua"}]
        }
        builder.pokewallet_get_detailed = lambda endpoint, api_key, params=None: (
            {
                "data": [
                    {
                        "name": "Zorua - BW12",
                        "card_number": "12/99",
                        "tcgplayer": {},
                        "cardmarket": {"market_price": 1.5},
                    }
                ]
            },
            200,
        )
        builder.fetch_pokemon_tcg_paginated = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pokemon_tcg_api fallback should not run in require mode when unusable records")
        )

        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            try:
                builder.build_english_current_prices_by_set(
                    "2026-05-21T00:00:00Z",
                    {
                        "buildCurrentPricesFromPokemonTcgApi": True,
                        "scheduledCurrentPriceBatchEnabled": False,
                        "continueOnSetError": True,
                        "usePokewalletPrices": True,
                    },
                    {"catalogueStatus": "built", "sets": [{"id": "bwp", "name": "BW Black Star Promos", "printedTotal": 101}]},
                    output_dir,
                    "current_prices",
                    {"enCurrentPriceCursor": 0},
                    fail_after_set_count=0,
                )
                assert False, "expected RuntimeError for no usable pokewallet records in require mode"
            except RuntimeError as exc:
                assert "no usable price records" in str(exc)
    finally:
        builder.pokewallet_get_detailed = original_get_detailed
        builder.load_pokewallet_set_code_map = original_map_loader
        builder.fetch_pokemon_tcg_paginated = original_fetch
        builder.load_catalogue_card_index_for_set = original_catalog_index_loader
        if original_use_flag is None:
            os.environ.pop("CARDSCANR_USE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = original_use_flag
        if original_api_key is None:
            os.environ.pop("CARDSCANR_POKEWALLET_API_KEY", None)
        else:
            os.environ["CARDSCANR_POKEWALLET_API_KEY"] = original_api_key
        if original_priority is None:
            os.environ.pop("CARDSCANR_PRICE_PROVIDER_PRIORITY", None)
        else:
            os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = original_priority
        if original_require is None:
            os.environ.pop("CARDSCANR_REQUIRE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = original_require


def test_fallback_mode_falls_back_when_raw_items_unusable_and_reports_rejections() -> None:
    original_get_detailed = builder.pokewallet_get_detailed
    original_map_loader = builder.load_pokewallet_set_code_map
    original_fetch = builder.fetch_pokemon_tcg_paginated
    original_catalog_index_loader = builder.load_catalogue_card_index_for_set
    original_load_existing = builder.load_existing_current_price_files
    original_use_flag = os.environ.get("CARDSCANR_USE_POKEWALLET_PRICES")
    original_api_key = os.environ.get("CARDSCANR_POKEWALLET_API_KEY")
    original_priority = os.environ.get("CARDSCANR_PRICE_PROVIDER_PRIORITY")
    original_require = os.environ.get("CARDSCANR_REQUIRE_POKEWALLET_PRICES")
    try:
        os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = "true"
        os.environ["CARDSCANR_POKEWALLET_API_KEY"] = "test-key"
        os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = "pokewallet,pokemon_tcg_api"
        os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = "false"

        builder.load_pokewallet_set_code_map = lambda: {
            "blackandwhitepromos": [
                {
                    "providerSetCode": "PR",
                    "providerSetId": "1407",
                    "providerSetName": "Black and White Promos",
                    "language": "en",
                    "cardCount": 98,
                    "releaseDate": "25th April, 2011",
                    "lookupName": "blackandwhitepromos",
                }
            ]
        }
        builder.load_catalogue_card_index_for_set = lambda set_id, language="en": {
            "BW12": [{"collectorNumber": "BW12", "normalizedName": "zorua"}]
        }
        builder.load_existing_current_price_files = lambda language="en": []

        builder.pokewallet_get_detailed = lambda endpoint, api_key, params=None: (
            {
                "data": [
                    {
                        "name": "Zorua - BW12",
                        "card_number": "12/99",
                        "tcgplayer": {},
                        "cardmarket": {"market_price": 1.5},
                    }
                ]
            },
            200,
        )

        def fake_fetch(endpoint: str, *, base_params=None, page_size=250, max_pages=50, sleep_seconds=0.15):
            set_id = str((base_params or {}).get("q", "set.id:test")).split(":", 1)[1]
            card = {
                "id": f"{set_id}-1",
                "set": {"id": set_id},
                "number": "1",
                "name": "Fallback Card",
                "tcgplayer": {"prices": {"normal": {"market": 2.0, "low": 1.5, "high": 2.5}}},
            }
            return [card], 1, 1

        builder.fetch_pokemon_tcg_paginated = fake_fetch

        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            _written_files, metrics, _next_state = builder.build_english_current_prices_by_set(
                "2026-05-21T00:00:00Z",
                {
                    "buildCurrentPricesFromPokemonTcgApi": True,
                    "scheduledCurrentPriceBatchEnabled": False,
                    "continueOnSetError": True,
                    "usePokewalletPrices": True,
                },
                {"catalogueStatus": "built", "sets": [{"id": "bwp", "name": "BW Black Star Promos", "printedTotal": 101}]},
                output_dir,
                "current_prices",
                {"enCurrentPriceCursor": 0},
                fail_after_set_count=0,
            )

            assert metrics["pokemonTcgApiFallbackSets"] == 1
            fallback_entries = [
                item for item in metrics.get("providerFallbackReasons", [])
                if isinstance(item, dict) and item.get("reason") == "pokewallet_no_price_records"
            ]
            assert fallback_entries
            assert isinstance(fallback_entries[0].get("rejectionReasonCounts"), dict)
    finally:
        builder.pokewallet_get_detailed = original_get_detailed
        builder.load_pokewallet_set_code_map = original_map_loader
        builder.fetch_pokemon_tcg_paginated = original_fetch
        builder.load_catalogue_card_index_for_set = original_catalog_index_loader
        builder.load_existing_current_price_files = original_load_existing
        if original_use_flag is None:
            os.environ.pop("CARDSCANR_USE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = original_use_flag
        if original_api_key is None:
            os.environ.pop("CARDSCANR_POKEWALLET_API_KEY", None)
        else:
            os.environ["CARDSCANR_POKEWALLET_API_KEY"] = original_api_key
        if original_priority is None:
            os.environ.pop("CARDSCANR_PRICE_PROVIDER_PRIORITY", None)
        else:
            os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = original_priority
        if original_require is None:
            os.environ.pop("CARDSCANR_REQUIRE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = original_require


def test_pokewallet_logging_and_diagnostics_when_enabled() -> None:
    original_get_detailed = builder.pokewallet_get_detailed
    original_map_loader = builder.load_pokewallet_set_code_map
    original_load_existing = builder.load_existing_current_price_files
    original_use_flag = os.environ.get("CARDSCANR_USE_POKEWALLET_PRICES")
    original_api_key = os.environ.get("CARDSCANR_POKEWALLET_API_KEY")
    original_priority = os.environ.get("CARDSCANR_PRICE_PROVIDER_PRIORITY")
    original_require = os.environ.get("CARDSCANR_REQUIRE_POKEWALLET_PRICES")
    try:
        os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = "true"
        os.environ["CARDSCANR_POKEWALLET_API_KEY"] = "test-key"
        os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = "pokewallet,pokemon_tcg_api"
        os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = "false"

        def fake_pokewallet_get_detailed(endpoint: str, api_key: str, params=None):
            assert endpoint == "prices/BS"
            assert api_key == "test-key"
            return ({
                "data": [
                    {
                        "card_number": "1",
                        "name": "Test Card",
                        "tcgplayer": {
                            "prices": {
                                "normal": {"market": 1.23, "low": 1.0, "high": 1.5}
                            }
                        },
                    }
                ]
            }, 200)

        builder.pokewallet_get_detailed = fake_pokewallet_get_detailed
        builder.load_pokewallet_set_code_map = lambda: {
            "baseset": [{"providerSetCode": "BS", "providerSetName": "Base Set", "cardCount": 102}]
        }
        builder.load_existing_current_price_files = lambda language="en": []

        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            stdout_buffer = io.StringIO()
            with contextlib.redirect_stdout(stdout_buffer):
                _written_files, metrics, _next_state = builder.build_english_current_prices_by_set(
                    "2026-05-21T00:00:00Z",
                    {
                        "buildCurrentPricesFromPokemonTcgApi": True,
                        "scheduledCurrentPriceBatchEnabled": False,
                        "continueOnSetError": True,
                        "usePokewalletPrices": True,
                    },
                    {"catalogueStatus": "built", "sets": [{"id": "base1", "name": "Base Set", "printedTotal": 102}]},
                    output_dir,
                    "current_prices",
                    {"enCurrentPriceCursor": 0},
                    fail_after_set_count=0,
                )

            logs = stdout_buffer.getvalue()
            assert "usePokeWalletPrices=True" in logs
            assert "providerPriority=['pokewallet', 'pokemon_tcg_api']" in logs
            assert "pokewalletApiKeyPresent=True" in logs
            assert "Trying PokeWallet for set base1 using providerSetCode BS" in logs
            assert metrics["pokewalletEnabled"] is True
            assert metrics["pokewalletApiKeyPresent"] is True
            assert metrics["pokewalletSetsAttempted"] == 1
            assert metrics["pokewalletSetsSucceeded"] == 1
            assert metrics["providerSourceCounts"]["pokewallet"] == 1
    finally:
        builder.pokewallet_get_detailed = original_get_detailed
        builder.load_pokewallet_set_code_map = original_map_loader
        builder.load_existing_current_price_files = original_load_existing
        if original_use_flag is None:
            os.environ.pop("CARDSCANR_USE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = original_use_flag
        if original_api_key is None:
            os.environ.pop("CARDSCANR_POKEWALLET_API_KEY", None)
        else:
            os.environ["CARDSCANR_POKEWALLET_API_KEY"] = original_api_key
        if original_priority is None:
            os.environ.pop("CARDSCANR_PRICE_PROVIDER_PRIORITY", None)
        else:
            os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = original_priority
        if original_require is None:
            os.environ.pop("CARDSCANR_REQUIRE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = original_require


def test_require_mode_fails_when_pokewallet_set_code_missing() -> None:
    original_map_loader = builder.load_pokewallet_set_code_map
    original_fetch = builder.fetch_pokemon_tcg_paginated
    original_load_existing = builder.load_existing_current_price_files
    original_use_flag = os.environ.get("CARDSCANR_USE_POKEWALLET_PRICES")
    original_api_key = os.environ.get("CARDSCANR_POKEWALLET_API_KEY")
    original_priority = os.environ.get("CARDSCANR_PRICE_PROVIDER_PRIORITY")
    original_require = os.environ.get("CARDSCANR_REQUIRE_POKEWALLET_PRICES")
    try:
        os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = "true"
        os.environ["CARDSCANR_POKEWALLET_API_KEY"] = "test-key"
        os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = "pokewallet,pokemon_tcg_api"
        os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = "true"
        builder.load_pokewallet_set_code_map = lambda: {}
        builder.load_existing_current_price_files = lambda language="en": []

        fallback_called = {"value": False}

        def fail_if_called(*args, **kwargs):
            fallback_called["value"] = True
            raise AssertionError("pokemon_tcg_api fallback should not run in require mode")

        builder.fetch_pokemon_tcg_paginated = fail_if_called

        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            try:
                builder.build_english_current_prices_by_set(
                    "2026-05-21T00:00:00Z",
                    {
                        "buildCurrentPricesFromPokemonTcgApi": True,
                        "scheduledCurrentPriceBatchEnabled": False,
                        "continueOnSetError": True,
                        "usePokewalletPrices": True,
                    },
                    {"catalogueStatus": "built", "sets": [{"id": "base1", "name": "Base Set", "printedTotal": 102}]},
                    output_dir,
                    "current_prices",
                    {"enCurrentPriceCursor": 0},
                    fail_after_set_count=0,
                )
                assert False, "expected RuntimeError for missing PokeWallet set-code match"
            except RuntimeError as exc:
                assert "no provider set-code match" in str(exc)
        assert fallback_called["value"] is False
    finally:
        builder.load_pokewallet_set_code_map = original_map_loader
        builder.fetch_pokemon_tcg_paginated = original_fetch
        builder.load_existing_current_price_files = original_load_existing
        if original_use_flag is None:
            os.environ.pop("CARDSCANR_USE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = original_use_flag
        if original_api_key is None:
            os.environ.pop("CARDSCANR_POKEWALLET_API_KEY", None)
        else:
            os.environ["CARDSCANR_POKEWALLET_API_KEY"] = original_api_key
        if original_priority is None:
            os.environ.pop("CARDSCANR_PRICE_PROVIDER_PRIORITY", None)
        else:
            os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = original_priority
        if original_require is None:
            os.environ.pop("CARDSCANR_REQUIRE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = original_require


def test_require_mode_fails_when_pokewallet_request_fails() -> None:
    original_get_detailed = builder.pokewallet_get_detailed
    original_map_loader = builder.load_pokewallet_set_code_map
    original_fetch = builder.fetch_pokemon_tcg_paginated
    original_load_existing = builder.load_existing_current_price_files
    original_use_flag = os.environ.get("CARDSCANR_USE_POKEWALLET_PRICES")
    original_api_key = os.environ.get("CARDSCANR_POKEWALLET_API_KEY")
    original_priority = os.environ.get("CARDSCANR_PRICE_PROVIDER_PRIORITY")
    original_require = os.environ.get("CARDSCANR_REQUIRE_POKEWALLET_PRICES")
    try:
        os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = "true"
        os.environ["CARDSCANR_POKEWALLET_API_KEY"] = "test-key"
        os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = "pokewallet,pokemon_tcg_api"
        os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = "true"
        builder.load_pokewallet_set_code_map = lambda: {
            "baseset": [{"providerSetCode": "BS", "providerSetName": "Base Set", "cardCount": 102}]
        }
        builder.load_existing_current_price_files = lambda language="en": []
        builder.pokewallet_get_detailed = lambda endpoint, api_key, params=None: (_ for _ in ()).throw(
            builder.requests.RequestException("boom")
        )
        builder.fetch_pokemon_tcg_paginated = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pokemon_tcg_api fallback should not run in require mode")
        )

        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            try:
                builder.build_english_current_prices_by_set(
                    "2026-05-21T00:00:00Z",
                    {
                        "buildCurrentPricesFromPokemonTcgApi": True,
                        "scheduledCurrentPriceBatchEnabled": False,
                        "continueOnSetError": True,
                        "usePokewalletPrices": True,
                    },
                    {"catalogueStatus": "built", "sets": [{"id": "base1", "name": "Base Set", "printedTotal": 102}]},
                    output_dir,
                    "current_prices",
                    {"enCurrentPriceCursor": 0},
                    fail_after_set_count=0,
                )
                assert False, "expected RuntimeError for failed PokeWallet request"
            except RuntimeError as exc:
                assert "request failed" in str(exc)
    finally:
        builder.pokewallet_get_detailed = original_get_detailed
        builder.load_pokewallet_set_code_map = original_map_loader
        builder.fetch_pokemon_tcg_paginated = original_fetch
        builder.load_existing_current_price_files = original_load_existing
        if original_use_flag is None:
            os.environ.pop("CARDSCANR_USE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = original_use_flag
        if original_api_key is None:
            os.environ.pop("CARDSCANR_POKEWALLET_API_KEY", None)
        else:
            os.environ["CARDSCANR_POKEWALLET_API_KEY"] = original_api_key
        if original_priority is None:
            os.environ.pop("CARDSCANR_PRICE_PROVIDER_PRIORITY", None)
        else:
            os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = original_priority
        if original_require is None:
            os.environ.pop("CARDSCANR_REQUIRE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = original_require


def test_fallback_mode_uses_pokemon_tcg_api_when_pokewallet_unmatched() -> None:
    original_map_loader = builder.load_pokewallet_set_code_map
    original_fetch = builder.fetch_pokemon_tcg_paginated
    original_load_existing = builder.load_existing_current_price_files
    original_use_flag = os.environ.get("CARDSCANR_USE_POKEWALLET_PRICES")
    original_api_key = os.environ.get("CARDSCANR_POKEWALLET_API_KEY")
    original_priority = os.environ.get("CARDSCANR_PRICE_PROVIDER_PRIORITY")
    original_require = os.environ.get("CARDSCANR_REQUIRE_POKEWALLET_PRICES")
    try:
        os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = "true"
        os.environ["CARDSCANR_POKEWALLET_API_KEY"] = "test-key"
        os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = "pokewallet,pokemon_tcg_api"
        os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = "false"
        builder.load_pokewallet_set_code_map = lambda: {}
        builder.load_existing_current_price_files = lambda language="en": []

        def fake_fetch(endpoint: str, *, base_params=None, page_size=250, max_pages=50, sleep_seconds=0.15):
            set_id = str((base_params or {}).get("q", "set.id:test")).split(":", 1)[1]
            card = {
                "id": f"{set_id}-1",
                "set": {"id": set_id},
                "number": "1",
                "name": "Fallback Card",
                "tcgplayer": {"prices": {"normal": {"market": 2.0, "low": 1.5, "high": 2.5}}},
            }
            return [card], 1, 1

        builder.fetch_pokemon_tcg_paginated = fake_fetch

        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            written_files, metrics, _next_state = builder.build_english_current_prices_by_set(
                "2026-05-21T00:00:00Z",
                {
                    "buildCurrentPricesFromPokemonTcgApi": True,
                    "scheduledCurrentPriceBatchEnabled": False,
                    "continueOnSetError": True,
                    "usePokewalletPrices": True,
                },
                {"catalogueStatus": "built", "sets": [{"id": "setx", "name": "Set X"}]},
                output_dir,
                "current_prices",
                {"enCurrentPriceCursor": 0},
                fail_after_set_count=0,
            )

            assert [item[0] for item in written_files] == ["setx"]
            payload = builder.load_json(output_dir / "setx.json")
            assert payload["source"] == "pokemon_tcg_api"
            assert metrics["pokemonTcgApiFallbackSets"] == 1
            assert metrics["providerSourceCounts"]["pokemon_tcg_api"] == 1
    finally:
        builder.load_pokewallet_set_code_map = original_map_loader
        builder.fetch_pokemon_tcg_paginated = original_fetch
        builder.load_existing_current_price_files = original_load_existing
        if original_use_flag is None:
            os.environ.pop("CARDSCANR_USE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = original_use_flag
        if original_api_key is None:
            os.environ.pop("CARDSCANR_POKEWALLET_API_KEY", None)
        else:
            os.environ["CARDSCANR_POKEWALLET_API_KEY"] = original_api_key
        if original_priority is None:
            os.environ.pop("CARDSCANR_PRICE_PROVIDER_PRIORITY", None)
        else:
            os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = original_priority
        if original_require is None:
            os.environ.pop("CARDSCANR_REQUIRE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = original_require


def test_provider_source_counts_track_pokewallet_and_pokemon_fallback() -> None:
    original_get_detailed = builder.pokewallet_get_detailed
    original_map_loader = builder.load_pokewallet_set_code_map
    original_fetch = builder.fetch_pokemon_tcg_paginated
    original_load_existing = builder.load_existing_current_price_files
    original_use_flag = os.environ.get("CARDSCANR_USE_POKEWALLET_PRICES")
    original_api_key = os.environ.get("CARDSCANR_POKEWALLET_API_KEY")
    original_priority = os.environ.get("CARDSCANR_PRICE_PROVIDER_PRIORITY")
    original_require = os.environ.get("CARDSCANR_REQUIRE_POKEWALLET_PRICES")
    try:
        os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = "true"
        os.environ["CARDSCANR_POKEWALLET_API_KEY"] = "test-key"
        os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = "pokewallet,pokemon_tcg_api"
        os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = "false"
        builder.load_existing_current_price_files = lambda language="en": []
        builder.load_pokewallet_set_code_map = lambda: {
            "baseset": [{"providerSetCode": "BS", "providerSetName": "Base Set", "cardCount": 102}]
        }

        def fake_pokewallet_get_detailed(endpoint: str, api_key: str, params=None):
            if endpoint == "prices/BS":
                return ({
                    "data": [
                        {
                            "card_number": "1",
                            "name": "PW Card",
                            "tcgplayer": {"prices": {"normal": {"market": 1.0, "low": 0.8, "high": 1.2}}},
                        }
                    ]
                }, 200)
            return ({"data": []}, 200)

        def fake_fetch(endpoint: str, *, base_params=None, page_size=250, max_pages=50, sleep_seconds=0.15):
            set_id = str((base_params or {}).get("q", "set.id:test")).split(":", 1)[1]
            card = {
                "id": f"{set_id}-1",
                "set": {"id": set_id},
                "number": "1",
                "name": "Fallback Card",
                "tcgplayer": {"prices": {"normal": {"market": 2.0, "low": 1.6, "high": 2.4}}},
            }
            return [card], 1, 1

        builder.pokewallet_get_detailed = fake_pokewallet_get_detailed
        builder.fetch_pokemon_tcg_paginated = fake_fetch

        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            _written_files, metrics, _next_state = builder.build_english_current_prices_by_set(
                "2026-05-21T00:00:00Z",
                {
                    "buildCurrentPricesFromPokemonTcgApi": True,
                    "scheduledCurrentPriceBatchEnabled": False,
                    "continueOnSetError": True,
                    "usePokewalletPrices": True,
                },
                {
                    "catalogueStatus": "built",
                    "sets": [
                        {"id": "base1", "name": "Base Set", "printedTotal": 102},
                        {"id": "setx", "name": "Set X"},
                    ],
                },
                output_dir,
                "current_prices",
                {"enCurrentPriceCursor": 0},
                fail_after_set_count=0,
            )

            assert metrics["providerSourceCounts"]["pokewallet"] == 1
            assert metrics["providerSourceCounts"]["pokemon_tcg_api"] == 1
    finally:
        builder.pokewallet_get_detailed = original_get_detailed
        builder.load_pokewallet_set_code_map = original_map_loader
        builder.fetch_pokemon_tcg_paginated = original_fetch
        builder.load_existing_current_price_files = original_load_existing
        if original_use_flag is None:
            os.environ.pop("CARDSCANR_USE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_USE_POKEWALLET_PRICES"] = original_use_flag
        if original_api_key is None:
            os.environ.pop("CARDSCANR_POKEWALLET_API_KEY", None)
        else:
            os.environ["CARDSCANR_POKEWALLET_API_KEY"] = original_api_key
        if original_priority is None:
            os.environ.pop("CARDSCANR_PRICE_PROVIDER_PRIORITY", None)
        else:
            os.environ["CARDSCANR_PRICE_PROVIDER_PRIORITY"] = original_priority
        if original_require is None:
            os.environ.pop("CARDSCANR_REQUIRE_POKEWALLET_PRICES", None)
        else:
            os.environ["CARDSCANR_REQUIRE_POKEWALLET_PRICES"] = original_require


def test_detect_rate_limited() -> None:
    assert updater.detect_rate_limited({"currentPriceEnStatus": "rate_limited"}) is True
    assert updater.detect_rate_limited({"buildStatus": "rate_limited"}) is True
    assert updater.detect_rate_limited({}, "HTTP 429 Too Many Requests") is True
    assert updater.detect_rate_limited({}, "generic error") is False


def test_append_request_ledger_keeps_recent_entries() -> None:
    state = {"requestLedger": []}
    updated = updater.append_request_ledger(state, requests_used=12, status="built", now_iso=updater.utc_now_iso())
    assert isinstance(updated.get("requestLedger"), list)
    assert len(updated["requestLedger"]) == 1
    entry = updated["requestLedger"][0]
    assert int(entry["requests"]) == 12
    assert entry["status"] == "built"


def test_builder_request_cap_blocks_provider_request_before_exceeding() -> None:
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {"data": {}}

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, params=None, headers=None, timeout=None):
        calls.append(url)
        return FakeResponse()

    original_cap = builder.CURRENT_PRICE_REQUEST_CAP
    original_get = builder.requests.get
    try:
        builder.reset_request_tracker()
        builder.CURRENT_PRICE_REQUEST_CAP = 1
        builder.REQUEST_TRACKER["attempted"] = 1
        builder.requests.get = fake_get

        try:
            builder.pokemon_tcg_get("cards")
            assert False, "expected RequestCapReachedError"
        except builder.RequestCapReachedError:
            pass

        assert calls == []
    finally:
        builder.CURRENT_PRICE_REQUEST_CAP = original_cap
        builder.requests.get = original_get


def test_builder_stops_when_cap_is_reached_and_cursor_remains_valid() -> None:
    original_fetch = builder.fetch_pokemon_tcg_paginated
    original_load_existing = builder.load_existing_current_price_files
    original_cap = builder.CURRENT_PRICE_REQUEST_CAP
    original_tracker = dict(builder.REQUEST_TRACKER)

    def fake_fetch(endpoint: str, *, base_params=None, page_size=250, max_pages=50, sleep_seconds=0.15):
        if builder.REQUEST_TRACKER["attempted"] >= builder.CURRENT_PRICE_REQUEST_CAP:
            raise builder.RequestCapReachedError("current price request cap reached")
        builder.mark_request_attempt(success=True)
        set_id = str((base_params or {}).get("q", "set.id:test")).split(":", 1)[1]
        card = {
            "id": f"{set_id}-1",
            "set": {"id": set_id},
            "number": "1",
            "name": f"Test {set_id}",
            "tcgplayer": {
                "prices": {
                    "normal": {"market": 1.23, "low": 1.0, "high": 1.5},
                }
            },
        }
        return [card], 1, 1

    try:
        builder.reset_request_tracker()
        builder.CURRENT_PRICE_REQUEST_CAP = 1
        builder.fetch_pokemon_tcg_paginated = fake_fetch
        builder.load_existing_current_price_files = lambda language="en": []

        with TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            catalog_sets = {
                "catalogueStatus": "built",
                "sets": [
                    {"id": "set1", "name": "Set One"},
                    {"id": "set2", "name": "Set Two"},
                ],
            }
            written_files, metrics, next_state = builder.build_english_current_prices_by_set(
                "2026-05-21T00:00:00Z",
                {
                    "buildCurrentPricesFromPokemonTcgApi": True,
                    "scheduledCurrentPriceBatchEnabled": True,
                    "scheduledCurrentPriceRefreshStrategy": "rotating_set_batch",
                    "scheduledCurrentPriceBatchSize": 2,
                    "localUpdaterIntervalMinutes": 60,
                    "continueOnSetError": True,
                },
                catalog_sets,
                output_dir,
                "current_prices",
                {"enCurrentPriceCursor": 0},
                fail_after_set_count=0,
            )

            assert metrics["currentPriceEnRequestCap"] == 1
            assert metrics["currentPriceEnRequestsUsed"] == 1
            assert metrics["currentPriceEnStatus"] == "partial_built"
            assert metrics["currentPriceEnStopReason"] == "request_cap_reached"
            assert metrics["currentPriceEnRateLimited"] is False
            assert metrics["currentPriceEnStatus"] != "rate_limited"
            assert metrics["currentPriceEnSetsWritten"] == 1
            assert [item[0] for item in written_files] == ["set1"]
            assert next_state["lastStopReason"] == "request_cap_reached"
            assert next_state["lastProcessedSetIds"] == ["set1"]
            assert next_state["enCurrentPriceCursor"] == 1
            assert (output_dir / "set1.json").exists()
            assert not (output_dir / "set2.json").exists()
    finally:
        builder.fetch_pokemon_tcg_paginated = original_fetch
        builder.load_existing_current_price_files = original_load_existing
        builder.CURRENT_PRICE_REQUEST_CAP = original_cap
        builder.REQUEST_TRACKER.update(original_tracker)


def test_request_cap_stop_is_not_detected_as_provider_rate_limit() -> None:
    diagnostics = {
        "currentPriceEnStatus": "partial_built",
        "stopReason": "request_cap_reached",
        "rateLimitStatus": "not_limited",
    }
    assert updater.detect_rate_limited(diagnostics) is False


if __name__ == "__main__":
    test_budget_usage_and_stop_logic()
    test_cycle_request_cap_uses_safety_buffer()
    test_updater_does_not_start_cycle_when_cap_is_exhausted()
    test_updater_passes_request_cap_to_builder_env()
    test_pokewallet_api_key_resolution_prefers_cardscanr_alias()
    test_price_provider_priority_prefers_pokewallet_when_configured()
    test_validate_cache_accepts_pokewallet_for_en_current_prices()
    test_validate_cache_rejects_unknown_en_current_price_source()
    test_validate_cache_accepts_pokemon_tcg_api_fallback_source()
    test_daily_budget_stop()
    test_all_day_hourly_exhaustion_returns_sleep_window()
    test_all_day_daily_exhaustion_stops_without_sleep()
    test_should_commit_changes_requires_validation_and_diffs()
    test_should_push_changes_requires_commit()
    test_pokewallet_bwp_maps_via_override_when_present()
    test_pokewallet_mapping_rejects_ambiguous_candidates()
    test_pokewallet_exact_code_match_wins()
    test_pokewallet_normalized_name_match_works()
    test_builder_uses_pokewallet_current_price_source_when_available()
    test_pokewallet_parser_extracts_records_from_flat_price_shape()
    test_pokewallet_parser_rejects_missing_tcgplayer_price()
    test_pokewallet_duplicate_canonicalids_are_deduped_deterministically()
    test_pokewallet_bwp_mocked_duplicate_victory_cup_rows_produce_unique_records()
    test_require_mode_fails_when_raw_items_have_no_usable_prices()
    test_fallback_mode_falls_back_when_raw_items_unusable_and_reports_rejections()
    test_pokewallet_logging_and_diagnostics_when_enabled()
    test_require_mode_fails_when_pokewallet_set_code_missing()
    test_require_mode_fails_when_pokewallet_request_fails()
    test_fallback_mode_uses_pokemon_tcg_api_when_pokewallet_unmatched()
    test_provider_source_counts_track_pokewallet_and_pokemon_fallback()
    test_detect_rate_limited()
    test_append_request_ledger_keeps_recent_entries()
    test_builder_request_cap_blocks_provider_request_before_exceeding()
    test_builder_stops_when_cap_is_reached_and_cursor_remains_valid()
    test_request_cap_stop_is_not_detected_as_provider_rate_limit()
    print("Local updater budget tests passed.")
