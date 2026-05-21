#!/usr/bin/env python3
"""Lightweight tests for local updater budget/rate-limit helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_local_price_update as updater  # noqa: E402
import build_price_cache as builder  # noqa: E402


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


def test_builder_uses_pokewallet_current_price_source_when_available() -> None:
    original_get = builder.pokewallet_get
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

        def fake_pokewallet_get(endpoint: str, api_key: str, params=None):
            assert api_key == "test-key"
            assert endpoint == "prices/BS"
            return {
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
            }

        builder.pokewallet_get = fake_pokewallet_get
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
        builder.pokewallet_get = original_get
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
    test_daily_budget_stop()
    test_builder_uses_pokewallet_current_price_source_when_available()
    test_detect_rate_limited()
    test_append_request_ledger_keeps_recent_entries()
    test_builder_request_cap_blocks_provider_request_before_exceeding()
    test_builder_stops_when_cap_is_reached_and_cursor_remains_valid()
    test_request_cap_stop_is_not_detected_as_provider_rate_limit()
    print("Local updater budget tests passed.")
