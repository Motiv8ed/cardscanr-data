"""Tests for hybrid bulk/reference pricing."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cardscanr_market_engine.bulk.display_price_policy import decide_display_price
from cardscanr_market_engine.bulk.price_semantics import ReferencePriceObservation
from cardscanr_market_engine.bulk.reference_refresh import BulkReferenceRefreshRunner, BulkRefreshConfig
from cardscanr_market_engine.bulk.static_price_index import clear_static_index_cache, lookup_static_reference
from cardscanr_market_engine.bulk.verification_router import route_verification
from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.models import MarketPriceKey
from cardscanr_market_engine.price_movement_guard import evaluate_price_movement


NOW = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)


def _sample_key(**overrides) -> MarketPriceKey:
    base = {
        "id": "k1",
        "game": "pokemon",
        "card_name": "Bulbasaur",
        "normalized_card_name": "bulbasaur",
        "set_name": "151",
        "set_code": "sv3pt5",
        "collector_number": "1",
        "language": "en",
        "variant": "raw",
        "condition": "raw",
        "market_country": "au",
        "currency": "aud",
        "fingerprint": "pokemon|en|sv3pt5|1|bulbasaur|raw|raw|au|aud",
    }
    base.update(overrides)
    return MarketPriceKey.from_row(base)


class StaticPriceIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_static_index_cache()

    def test_lookup_static_reference_from_repo_file(self) -> None:
        obs = lookup_static_reference(
            game="pokemon",
            language="en",
            set_code="sv3pt5",
            collector_number="1",
            card_name="Bulbasaur",
            normalized_card_name="bulbasaur",
            variant="raw",
        )
        self.assertIsNotNone(obs)
        assert obs is not None
        self.assertEqual(obs.provider, "static_reference")
        self.assertGreater(obs.market_price, 0)
        self.assertEqual(obs.mapping_status, "exact")

    def test_ambiguous_mapping_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            price_dir = root / "public" / "v1" / "prices" / "current" / "pokemon" / "en"
            price_dir.mkdir(parents=True)
            payload = {
                "prices": [
                    {
                        "collectorNumber": "10",
                        "variant": "normal",
                        "normalizedName": "pikachu",
                        "marketPrice": 1.0,
                        "sourceCurrency": "USD",
                        "currency": "USD",
                    },
                    {
                        "collectorNumber": "10",
                        "variant": "normal",
                        "normalizedName": "pikachu",
                        "marketPrice": 2.0,
                        "sourceCurrency": "USD",
                        "currency": "USD",
                    },
                ]
            }
            (price_dir / "demo.json").write_text(json.dumps(payload), encoding="utf-8")
            with patch("cardscanr_market_engine.bulk.static_price_index.CURRENT_PRICE_ROOT", root / "public" / "v1" / "prices" / "current"):
                clear_static_index_cache()
                obs = lookup_static_reference(
                    game="pokemon",
                    language="en",
                    set_code="demo",
                    collector_number="10",
                    card_name="Pikachu",
                    normalized_card_name="pikachu",
                    variant="raw",
                )
        self.assertIsNotNone(obs)
        assert obs is not None
        self.assertEqual(obs.mapping_status, "ambiguous")


class DisplayPolicyTests(unittest.TestCase):
    def test_verified_au_preserved_over_reference(self) -> None:
        obs = ReferencePriceObservation(
            provider="static_reference",
            source_market="us",
            source_currency="USD",
            market_price=5.0,
            low_price=None,
            high_price=None,
            confidence="medium",
            mapping_status="exact",
        )
        decision = decide_display_price(
            prior_cache={
                "current_market_price": 65.14,
                "provider": "ebay_browser",
                "display_price_source": "verified_au",
                "confidence": "medium",
            },
            observation=obs,
            converted_price=5.84,
            target_currency="AUD",
            now=NOW,
        )
        self.assertEqual(decision.action, "pending_verification")
        self.assertEqual(decision.display_price, 65.14)
        self.assertTrue(decision.verification_required)

    def test_pikachu_extreme_movement_guard(self) -> None:
        movement = evaluate_price_movement(old_price=65.14, new_price=5.84, included_count=13, confidence="medium")
        self.assertEqual(movement.action, "pending_verification")

    def test_reference_applied_when_no_prior(self) -> None:
        obs = ReferencePriceObservation(
            provider="static_reference",
            source_market="us",
            source_currency="USD",
            market_price=0.2,
            low_price=None,
            high_price=None,
            confidence="low",
            mapping_status="exact",
        )
        decision = decide_display_price(
            prior_cache=None,
            observation=obs,
            converted_price=0.31,
            target_currency="AUD",
            now=NOW,
        )
        self.assertEqual(decision.action, "apply_reference")
        self.assertEqual(decision.display_source, "reference")


class VerificationRouterTests(unittest.TestCase):
    def test_stable_low_value_skips_ebay(self) -> None:
        obs = ReferencePriceObservation(
            provider="static_reference",
            source_market="us",
            source_currency="USD",
            market_price=1.5,
            low_price=None,
            high_price=None,
            confidence="medium",
            mapping_status="exact",
        )
        display = decide_display_price(
            prior_cache=None,
            observation=obs,
            converted_price=2.3,
            target_currency="AUD",
            now=NOW,
        )
        route = route_verification(
            prior_cache=None,
            observation=obs,
            display=display,
            value_signal=2.3,
            high_value_threshold=50.0,
        )
        self.assertFalse(route.should_verify)
        self.assertEqual(route.reason, "stable_low_value_reference")


class FakeBulkClient:
    def __init__(self) -> None:
        self.snapshots: list[dict] = []
        self.cache: dict[str, dict] = {}
        self.jobs: list[dict] = []

    def _table_get(self, table: str, params: dict | None = None):
        if table == "market_price_keys":
            return [k.raw for k in getattr(self, "keys", [])]
        if table == "market_price_cache":
            ids = []
            filt = (params or {}).get("price_key_id", "")
            if filt.startswith("in."):
                ids = filt[4:-1].split(",")
            return [self.cache[i] for i in ids if i in self.cache]
        return []

    def insert_snapshot(self, payload: dict):
        self.snapshots.append(payload)
        return payload

    def upsert_cache(self, payload: dict):
        self.cache[payload["price_key_id"]] = payload
        return payload

    def enqueue_refresh_job(self, **kwargs):
        self.jobs.append(kwargs)
        return {"id": "job1", "status": "queued"}

    def record_provider_sync_run(self, **kwargs):
        return kwargs


class BulkRunnerSimulationTests(unittest.TestCase):
    def _engine_config(self) -> MarketEngineConfig:
        return MarketEngineConfig(
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="secret",
            provider_name="mock",
            worker_concurrency=1,
            poll_seconds=5,
            max_jobs_per_run=5,
            high_confidence_hours=24,
            medium_confidence_hours=12,
            low_confidence_hours=6,
            no_comps_hours=3,
            refresh_default_cooldown_hours=6,
            refresh_high_value_cooldown_hours=4,
            refresh_popular_cooldown_hours=4,
            refresh_hot_card_cooldown_hours=2,
            refresh_low_value_cooldown_hours=12,
            ebay_browser_headless=True,
            ebay_browser_engine="chrome",
            ebay_browser_channel="chrome",
            ebay_browser_profile_name="cardscanr",
            ebay_browser_max_results=30,
            ebay_browser_timeout_seconds=45,
            ebay_browser_cooldown_seconds=20,
            ebay_browser_min_seconds_between_requests=20,
            ebay_browser_user_data_dir=None,
            provider_max_requests_per_minute=20,
            provider_max_requests_per_day=100,
            ebay_fallback_marketplaces=(),
            currency_rates={"USD:AUD": 1.55, "EUR:AUD": 1.72},
            currency_rate_source="configured_static_rates",
            enable_live_ebay_scheduler=False,
            confirm_live_ebay_scheduler=False,
            live_ebay_scheduler_markets="AU",
            live_ebay_scheduler_max_enqueues_per_run=10,
            live_ebay_scheduler_max_keys_scanned_per_run=50,
            live_ebay_scheduler_min_cooldown_hours=1,
            live_ebay_scheduler_allow_force_refresh=False,
            live_ebay_scheduler_dry_run=False,
            live_ebay_scheduler_daily_enqueue_cap=100,
            reports_dir=Path("reports"),
            latest_report_path=Path("reports/latest.json"),
            runs_report_path=Path("reports/runs.jsonl"),
            worker_id="test",
        )

    def test_dry_run_processes_shared_keys(self) -> None:
        client = FakeBulkClient()
        key = _sample_key(id="abc")
        client.keys = [key]
        runner = BulkReferenceRefreshRunner(
            client=client,  # type: ignore[arg-type]
            engine_config=self._engine_config(),
            refresh_config=BulkRefreshConfig(
                dry_run=True,
                max_keys=10,
                enable_live_tcgdex=False,
                verification_budget_per_run=5,
                high_value_threshold=50.0,
                reference_fresh_hours=24,
            ),
            now_func=lambda: NOW,
        )
        report = runner.run()
        self.assertGreaterEqual(report["keysMatched"], 1)
        self.assertEqual(len(client.snapshots), 0)

    def test_scale_simulation_50k(self) -> None:
        keys = [
            _sample_key(
                id=f"k{i}",
                collector_number=str((i % 200) + 1),
                set_code="sv3pt5",
                normalized_card_name=f"card_{i % 50}",
                card_name=f"Card {i % 50}",
            )
            for i in range(50_000)
        ]
        client = FakeBulkClient()
        client.keys = keys
        runner = BulkReferenceRefreshRunner(
            client=client,  # type: ignore[arg-type]
            engine_config=self._engine_config(),
            refresh_config=BulkRefreshConfig(
                dry_run=True,
                max_keys=50_000,
                enable_live_tcgdex=False,
                verification_budget_per_run=0,
                high_value_threshold=50.0,
                reference_fresh_hours=24,
            ),
            now_func=lambda: NOW,
        )
        report = runner.run()
        self.assertEqual(report["keysScanned"], 50_000)
        self.assertGreater(report["bulkKeysPerHour"], 2000)


if __name__ == "__main__":
    unittest.main()
