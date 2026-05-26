from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS_DIR))

from market_pricing_job_queue import (  # noqa: E402
    MockMarketListingsProvider,
    MarketPriceJob,
    build_market_query,
    market_config,
    normalize_market,
)


WORKER_SPEC = importlib.util.spec_from_file_location(
    "market_pricing_worker",
    TOOLS_DIR / "market_pricing_worker.py",
)
if WORKER_SPEC is None or WORKER_SPEC.loader is None:
    raise RuntimeError("Unable to load market_pricing_worker.py")
worker = importlib.util.module_from_spec(WORKER_SPEC)
WORKER_SPEC.loader.exec_module(worker)


class MarketPricingFoundationTests(unittest.TestCase):
    def test_query_generation_excludes_banned_terms(self) -> None:
        job = MarketPriceJob(
            game="pokemon",
            language="en",
            market="AU",
            currency="AUD",
            canonical_card_id="pokemon|en|base1|4|charizard",
            set_id="base1",
            set_name="Base",
            collector_number="4",
            card_name="Charizard",
            variant="raw",
            condition="near_mint",
            graded_state="ungraded",
        )
        query = build_market_query(job)
        self.assertIn("-proxy", query)
        self.assertIn("-custom", query)
        self.assertIn("-fake", query)
        self.assertIn("-digital", query)
        self.assertIn("-lot", query)
        self.assertIn("-bundle", query)
        self.assertIn("-damaged", query)

    def test_market_currency_mapping_works(self) -> None:
        self.assertEqual(normalize_market("AU"), "au")
        self.assertEqual(normalize_market("eu_global"), "eu")
        self.assertEqual(market_config("gb")["currency"], "GBP")
        self.assertEqual(market_config("ca")["market"], "CA")

    def test_mock_provider_evidence_shape(self) -> None:
        provider = MockMarketListingsProvider()
        job = MarketPriceJob(
            game="pokemon",
            language="en",
            market="US",
            currency="USD",
            canonical_card_id="pokemon|en|base1|4|charizard",
            set_id="base1",
            set_name="Base",
            collector_number="4",
            card_name="Charizard",
            variant="raw",
            condition="near_mint",
            graded_state="ungraded",
        )
        result = provider.fetch(job, "charizard base set")
        self.assertEqual(result.provider_name, "mock")
        self.assertGreaterEqual(len(result.listings), 3)
        listing = result.listings[0]
        self.assertTrue(listing.listing_url.startswith("https://"))
        self.assertEqual(listing.currency, "USD")
        self.assertIsInstance(listing.total_price, float)

    def test_worker_dry_run_does_not_modify_public_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._seed_minimal_catalog(root)

            args = self._worker_args(dry_run=True, write=True)
            report = worker.run_worker(args, root=root)

            self.assertEqual(report["input"]["dryRun"], True)
            self.assertFalse((root / "public" / "v1" / "markets" / "prices").exists())

    def test_worker_write_mode_writes_market_price_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._seed_minimal_catalog(root)
            self._seed_market_status(root)

            args = self._worker_args(dry_run=False, write=True)
            report = worker.run_worker(args, root=root)

            self.assertEqual(report["status"], "ok")
            out_file = root / "public" / "v1" / "markets" / "prices" / "au" / "pokemon" / "en" / "base1.json"
            self.assertTrue(out_file.exists())
            payload = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("market"), "AU")
            self.assertEqual(payload.get("game"), "pokemon")
            self.assertEqual(payload.get("language"), "en")
            self.assertIsInstance(payload.get("prices"), list)
            self.assertGreaterEqual(payload.get("recordCount", 0), 1)

    def _worker_args(self, *, dry_run: bool, write: bool) -> argparse.Namespace:
        return argparse.Namespace(
            market="AU",
            language="en",
            game="pokemon",
            max_jobs=5,
            dry_run=dry_run,
            provider="mock",
            write=write,
            commit_safe_report=True,
            card_id=None,
            set_id="base1",
            query_only=False,
            manual_source_path="data/manual_market_prices/sample_market_sold_listings.json",
            condition="near_mint",
            variant="raw",
            graded=False,
        )

    def _seed_minimal_catalog(self, root: Path) -> None:
        cards_path = root / "public" / "v1" / "catalog" / "pokemon" / "en" / "cards" / "base1.json"
        cards_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cardCount": 1,
            "cards": [
                {
                    "canonicalBaseId": "pokemon|en|base1|4|charizard",
                    "setId": "base1",
                    "setName": "Base",
                    "collectorNumber": "4",
                    "name": "Charizard",
                }
            ],
        }
        cards_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _seed_market_status(self, root: Path) -> None:
        status_path = root / "public" / "v1" / "markets" / "market-price-status.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "1.0.0",
                    "generatedAtUtc": "2026-05-26T00:00:00Z",
                    "status": "enabled_foundation",
                    "sourceStatus": {
                        "liveEbayWorker": "disabled",
                        "mockProvider": "enabled",
                        "manualProvider": "enabled",
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
