"""Tests for bulk coverage mappings and TCGdex API repair."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cardscanr_market_engine.bulk.set_id_aliases import (
    is_synthetic_set_code,
    resolve_static_set_id,
    resolve_tcgdex_set_id,
)
from cardscanr_market_engine.bulk.static_price_index import clear_static_index_cache, lookup_static_reference
from cardscanr_market_engine.bulk.sync_lock import acquire_bulk_sync_lock, release_bulk_sync_lock
from cardscanr_market_engine.bulk.tcgdex_client import TcgdexRunCache, lookup_tcgdex_reference
from cardscanr_market_engine.currency_conversion import resolve_currency_conversion
from datetime import datetime, timezone


class SetMappingTests(unittest.TestCase):
    def test_me4_maps_to_tcgdex_me04(self) -> None:
        self.assertEqual(resolve_tcgdex_set_id("me4"), "me04")

    def test_obf_maps_to_sv03_static_and_tcgdex(self) -> None:
        self.assertEqual(resolve_static_set_id("obf"), "sv3")
        self.assertEqual(resolve_tcgdex_set_id("obf"), "sv03")

    def test_jp_sv09_maps_to_24173_static(self) -> None:
        self.assertEqual(resolve_static_set_id("sv09", language="jp"), "24173")

    def test_asia_official_set_alias(self) -> None:
        self.assertEqual(
            resolve_static_set_id("pokemon-asia-my-official:set:sv3.5"),
            "sv3pt5",
        )

    def test_smoke_test_excluded(self) -> None:
        self.assertTrue(is_synthetic_set_code("smoke-test", "Smoke Test Set"))


class TcgdexClientTests(unittest.TestCase):
    def test_preload_set_uses_set_detail_endpoint(self) -> None:
        cache = TcgdexRunCache()
        payload = {
            "id": "me04",
            "cards": [{"id": "me04-050", "localId": "050", "name": "Golbat"}],
        }

        def _fake_get(url: str, *, timeout: int = 15):
            self.assertTrue(url.endswith("/sets/me04"))
            return payload

        with patch("cardscanr_market_engine.bulk.tcgdex_client._http_get_json", side_effect=_fake_get):
            cards = cache.preload_set(language="en", set_id="me04")
        self.assertEqual(len(cards), 1)

    def test_lookup_me4_golbat(self) -> None:
        cache = TcgdexRunCache()
        set_payload = {
            "id": "me04",
            "cards": [{"id": "me04-050", "localId": "050", "name": "Golbat"}],
        }
        detail_payload = {
            "id": "me04-050",
            "pricing": {
                "tcgplayer": {
                    "holofoil": {"marketPrice": 0.06},
                }
            },
        }

        def _fake_get(url: str, *, timeout: int = 15):
            if url.endswith("/sets/me04"):
                return set_payload
            if url.endswith("/cards/me04-050"):
                return detail_payload
            raise AssertionError(url)

        with patch("cardscanr_market_engine.bulk.tcgdex_client._http_get_json", side_effect=_fake_get):
            obs = lookup_tcgdex_reference(
                language="en",
                set_code="me4",
                collector_number="050/086",
                card_name="Golbat",
                normalized_card_name="golbat",
                variant="non_holo",
                cache=cache,
            )
        self.assertIsNotNone(obs)
        assert obs is not None
        self.assertEqual(obs.provider, "tcgdex_tcgplayer")
        self.assertGreater(obs.market_price, 0)


class CurrencyTriangulationTests(unittest.TestCase):
    def test_usd_to_gbp_via_aud(self) -> None:
        conversion = resolve_currency_conversion(
            source_currency="USD",
            target_currency="GBP",
            rates={"USD:AUD": 1.55, "GBP:AUD": 1.98},
            rate_source="test",
            now=datetime.now(timezone.utc),
        )
        self.assertAlmostEqual(conversion.rate, 1.55 / 1.98, places=4)


class StaticDuplicateSourceTests(unittest.TestCase):
    def test_duplicate_provider_rows_pick_single_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            price_dir = root / "public" / "v1" / "prices" / "current" / "pokemon" / "jp"
            price_dir.mkdir(parents=True)
            payload = {
                "prices": [
                    {
                        "collectorNumber": "044/100",
                        "variant": "normal",
                        "normalizedName": "swinub",
                        "marketPrice": 0.5,
                        "sourceCurrency": "USD",
                        "currency": "USD",
                        "source": "tcgplayer",
                    },
                    {
                        "collectorNumber": "044/100",
                        "variant": "normal",
                        "normalizedName": "swinub",
                        "marketPrice": 0.4,
                        "sourceCurrency": "EUR",
                        "currency": "EUR",
                        "source": "cardmarket",
                    },
                ]
            }
            (price_dir / "24173.json").write_text(json.dumps(payload), encoding="utf-8")
            with patch("cardscanr_market_engine.bulk.static_price_index.CURRENT_PRICE_ROOT", root / "public" / "v1" / "prices" / "current"):
                clear_static_index_cache()
                obs = lookup_static_reference(
                    game="pokemon",
                    language="jp",
                    set_code="24173",
                    collector_number="044/100",
                    card_name="Swinub",
                    normalized_card_name="swinub",
                    variant="raw",
                )
        self.assertIsNotNone(obs)
        assert obs is not None
        self.assertNotEqual(obs.mapping_status, "ambiguous")
        self.assertGreater(obs.market_price, 0)


class SyncLockTests(unittest.TestCase):
    def test_lock_prevents_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_file = Path(tmp) / "bulk.lock.json"
            with patch("cardscanr_market_engine.bulk.sync_lock.LOCK_PATH", lock_file):
                first = acquire_bulk_sync_lock()
                second = acquire_bulk_sync_lock()
                self.assertTrue(first.acquired)
                self.assertFalse(second.acquired)
                release_bulk_sync_lock()
                third = acquire_bulk_sync_lock()
                self.assertTrue(third.acquired)
                release_bulk_sync_lock()
