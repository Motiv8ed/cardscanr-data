"""
test_market_pricing_worker_provider_integration.py

Integration tests for the market pricing worker using the provider adapter layer.

Tests cover:
- Worker resolves providers via MarketPriceProviderRegistry
- Mock provider end-to-end builds market price aggregates
- Disabled eBay provider fails before any network call
- Dry-run mode does not write public price files
- Write mode writes valid market price JSON files
- Rejected evidence reasons appear in the report
- Provider summary appears in the report
- Local engine runner still passes (smoke check)
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(ROOT))

import importlib.util

# Load market_pricing_worker module
_WORKER_SPEC = importlib.util.spec_from_file_location(
    "market_pricing_worker",
    TOOLS_DIR / "market_pricing_worker.py",
)
if _WORKER_SPEC is None or _WORKER_SPEC.loader is None:
    raise RuntimeError("Unable to load market_pricing_worker.py")
worker_mod = importlib.util.module_from_spec(_WORKER_SPEC)
_WORKER_SPEC.loader.exec_module(worker_mod)

from market_pricing_provider_contracts import MarketPriceSearchRequest
from market_price_providers.provider_registry import (
    MarketPriceProviderRegistry,
    ProviderNotAllowedError,
)
from market_price_providers.mock_provider import MockMarketPriceProvider
from market_price_providers.disabled_ebay_provider import (
    DisabledEbayMarketPriceProvider,
    DisabledProviderError,
)
from market_price_evidence_normalizer import filter_evidence_listings
from market_pricing_job_queue import aggregate_evidence_listings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _worker_args(
    *,
    dry_run: bool = False,
    write: bool = False,
    provider: str = "mock",
    set_id: str = "base1",
    max_jobs: int = 5,
    query_only: bool = False,
    manual_source_path: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        market="AU",
        language="en",
        game="pokemon",
        max_jobs=max_jobs,
        dry_run=dry_run,
        provider=provider,
        write=write,
        commit_safe_report=True,
        card_id=None,
        set_id=set_id,
        query_only=query_only,
        manual_source_path=manual_source_path or str(ROOT / "data" / "manual_market_prices" / "sample_market_sold_listings.json"),
        condition="near_mint",
        variant="raw",
        graded=False,
    )


def _seed_minimal_catalog(root: Path) -> None:
    cards_path = root / "public" / "v1" / "catalog" / "pokemon" / "en" / "cards" / "base1.json"
    cards_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cardCount": 1,
        "cards": [
            {
                "canonicalBaseId": "pokemon|en|base1|4|charizard",
                "setId": "base1",
                "setName": "Base Set",
                "collectorNumber": "4",
                "name": "Charizard",
            }
        ],
    }
    cards_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _seed_market_status(root: Path) -> None:
    status_path = root / "public" / "v1" / "markets" / "market-price-status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps({"schemaVersion": "1.0.0", "status": "enabled_foundation"}, indent=2) + "\n",
        encoding="utf-8",
    )


def _sample_request(market: str = "AU", currency: str = "AUD") -> MarketPriceSearchRequest:
    return MarketPriceSearchRequest(
        market=market,
        currency=currency,
        marketplace=f"EBAY_{market}",
        game="pokemon",
        language="en",
        canonical_id="pokemon|en|base1|4|charizard",
        card_name="Charizard",
        set_name="Base Set",
        set_id="base1",
        collector_number="4",
        variant="raw",
        condition="near_mint",
    )


# ---------------------------------------------------------------------------
# 1. Worker uses provider registry
# ---------------------------------------------------------------------------


class TestWorkerUsesProviderRegistry(unittest.TestCase):
    """Worker must resolve provider via MarketPriceProviderRegistry, not old classes."""

    def test_worker_mock_provider_resolves_via_registry(self) -> None:
        registry = MarketPriceProviderRegistry()
        provider = registry.get("mock")
        self.assertIsInstance(provider, MockMarketPriceProvider)

    def test_worker_report_includes_provider_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)
            _seed_market_status(root)

            args = _worker_args(dry_run=True)
            report = worker_mod.run_worker(args, root=root)

            self.assertIn("providerResolved", report)
            self.assertEqual(report["providerResolved"], "mock")

    def test_worker_report_includes_provider_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True, provider="mock")
            report = worker_mod.run_worker(args, root=root)

            self.assertEqual(report["providerRequested"], "mock")

    def test_worker_report_includes_provider_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True)
            report = worker_mod.run_worker(args, root=root)

            self.assertTrue(report["providerEnabled"])


# ---------------------------------------------------------------------------
# 2. Mock provider end-to-end builds market price aggregates
# ---------------------------------------------------------------------------


class TestMockProviderEndToEnd(unittest.TestCase):
    """Full mock provider flow produces valid aggregates."""

    def test_mock_provider_produces_priced_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True)
            report = worker_mod.run_worker(args, root=root)

            records = report.get("records", [])
            self.assertGreaterEqual(len(records), 1)
            priced = [r for r in records if r.get("status") == "priced"]
            self.assertGreaterEqual(len(priced), 1)

    def test_mock_provider_record_has_aggregate_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True)
            report = worker_mod.run_worker(args, root=root)

            record = report["records"][0]
            self.assertIsNotNone(record.get("medianPrice"))
            self.assertIsNotNone(record.get("averagePrice"))
            self.assertIsNotNone(record.get("lowPrice"))
            self.assertIsNotNone(record.get("highPrice"))
            self.assertIsInstance(record.get("sampleCount"), int)
            self.assertGreater(record["sampleCount"], 0)
            self.assertIsNotNone(record.get("confidenceScore"))
            self.assertIsNotNone(record.get("confidenceLabel"))

    def test_mock_provider_source_is_mock_market_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True)
            report = worker_mod.run_worker(args, root=root)

            record = report["records"][0]
            self.assertEqual(record.get("source"), "mock_market_provider")

    def test_mock_provider_evidence_accepted_in_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True)
            report = worker_mod.run_worker(args, root=root)

            summary = report.get("summary", {})
            self.assertGreater(summary.get("evidenceAccepted", 0), 0)

    def test_mock_provider_live_ebay_is_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True)
            report = worker_mod.run_worker(args, root=root)

            self.assertFalse(report.get("liveEbayEnabled"))


# ---------------------------------------------------------------------------
# 3. Disabled eBay provider fails before any network call
# ---------------------------------------------------------------------------


class TestDisabledEbayProviderBlockedBeforeNetwork(unittest.TestCase):
    """eBay/live/apify/browser providers must be blocked before any network activity."""

    def test_disabled_ebay_provider_fetch_raises_immediately(self) -> None:
        provider = DisabledEbayMarketPriceProvider()
        req = _sample_request()
        with self.assertRaises(DisabledProviderError) as ctx:
            provider.fetch(req)
        # Must not have attempted live network
        self.assertFalse(ctx.exception.provider_error.live_network_attempted)

    def test_registry_blocks_ebay_disabled_before_fetch(self) -> None:
        registry = MarketPriceProviderRegistry()
        with self.assertRaises(ProviderNotAllowedError):
            registry.get("ebay_disabled")

    def test_worker_is_live_provider_helper_blocks_ebay(self) -> None:
        self.assertTrue(worker_mod._is_live_provider("ebay"))
        self.assertTrue(worker_mod._is_live_provider("ebay_live"))
        self.assertTrue(worker_mod._is_live_provider("ebay_sold_listings_apify_planned"))
        self.assertTrue(worker_mod._is_live_provider("apify"))
        self.assertTrue(worker_mod._is_live_provider("browser"))

    def test_worker_is_live_provider_allows_mock_and_manual(self) -> None:
        self.assertFalse(worker_mod._is_live_provider("mock"))
        self.assertFalse(worker_mod._is_live_provider("manual"))

    def test_worker_with_blocked_provider_name_adds_error_to_report(self) -> None:
        """If somehow a blocked provider name reaches _is_live_provider, report it safely."""
        # We can simulate this via a custom args namespace with a bad provider name
        # that bypasses argparse choices validation
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True)
            # Manually override to a live-sounding name
            args.provider = "ebay_live_test"
            report = worker_mod.run_worker(args, root=root)

            self.assertFalse(report.get("providerEnabled"))
            self.assertEqual(report.get("status"), "partial_error")
            errors = report.get("summary", {}).get("errors", [])
            self.assertTrue(any("provider_blocked" in e for e in errors))


# ---------------------------------------------------------------------------
# 4. Dry-run does not write public price files
# ---------------------------------------------------------------------------


class TestDryRunDoesNotWritePublicFiles(unittest.TestCase):
    def test_dry_run_with_write_flag_does_not_write_price_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True, write=True)
            report = worker_mod.run_worker(args, root=root)

            self.assertTrue(report["input"]["dryRun"])
            prices_dir = root / "public" / "v1" / "markets" / "prices"
            self.assertFalse(prices_dir.exists(), "dry-run must not write public price files")

    def test_dry_run_still_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)
            paths = worker_mod.output_paths_for_root(root)

            args = _worker_args(dry_run=True)
            worker_mod.run_worker(args, root=root, output_paths=paths)

            self.assertTrue(paths["worker_json"].exists(), "worker report JSON must be written in dry-run")
            self.assertTrue(paths["worker_md"].exists(), "worker report MD must be written in dry-run")


# ---------------------------------------------------------------------------
# 5. Write mode writes valid market price files
# ---------------------------------------------------------------------------


class TestWriteModeWritesValidFiles(unittest.TestCase):
    def test_write_mode_creates_price_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)
            _seed_market_status(root)

            args = _worker_args(dry_run=False, write=True)
            report = worker_mod.run_worker(args, root=root)

            self.assertEqual(report["status"], "ok")
            out_file = root / "public" / "v1" / "markets" / "prices" / "au" / "pokemon" / "en" / "base1.json"
            self.assertTrue(out_file.exists(), f"Expected price file at {out_file}")

    def test_write_mode_price_file_has_valid_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)
            _seed_market_status(root)

            args = _worker_args(dry_run=False, write=True)
            worker_mod.run_worker(args, root=root)

            out_file = root / "public" / "v1" / "markets" / "prices" / "au" / "pokemon" / "en" / "base1.json"
            payload = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("market"), "AU")
            self.assertEqual(payload.get("game"), "pokemon")
            self.assertEqual(payload.get("language"), "en")
            self.assertIsInstance(payload.get("prices"), list)
            self.assertGreaterEqual(payload.get("recordCount", 0), 1)

    def test_write_mode_price_file_source_is_mock_market_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)
            _seed_market_status(root)

            args = _worker_args(dry_run=False, write=True)
            worker_mod.run_worker(args, root=root)

            out_file = root / "public" / "v1" / "markets" / "prices" / "au" / "pokemon" / "en" / "base1.json"
            payload = json.loads(out_file.read_text(encoding="utf-8"))
            prices = payload.get("prices", [])
            self.assertGreater(len(prices), 0)
            self.assertEqual(prices[0].get("source"), "mock_market_provider")


# ---------------------------------------------------------------------------
# 6. Rejected evidence reasons appear in report
# ---------------------------------------------------------------------------


class TestRejectedEvidenceReasonsInReport(unittest.TestCase):
    def test_rejection_reason_keys_are_present_in_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True)
            report = worker_mod.run_worker(args, root=root)

            summary = report.get("summary", {})
            self.assertIn("rejectionReasons", summary)
            self.assertIsInstance(summary["rejectionReasons"], dict)

    def test_provider_summary_contains_rejection_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True)
            report = worker_mod.run_worker(args, root=root)

            prov_summary = report.get("providerSummary", {})
            self.assertIn("rejectionReasons", prov_summary)

    def test_filter_evidence_listings_tracks_rejected_titles(self) -> None:
        """filter_evidence_listings correctly rejects listings with exclusion terms."""
        from market_pricing_provider_contracts import MarketPriceEvidenceListing

        clean = MarketPriceEvidenceListing(
            title="Charizard Base Set near mint raw",
            sold_price=80.0, shipping_price=0.0, total_price=80.0,
            currency="AUD", sold_date="2024-03-15T00:00:00Z",
            listing_url="https://www.ebay.com.au/itm/1",
            marketplace="EBAY_AU",
        )
        dirty = MarketPriceEvidenceListing(
            title="Charizard fake proxy lot",
            sold_price=5.0, shipping_price=0.0, total_price=5.0,
            currency="AUD", sold_date="2024-03-15T00:00:00Z",
            listing_url="https://www.ebay.com.au/itm/2",
            marketplace="EBAY_AU",
        )

        accepted, rejected = filter_evidence_listings([clean, dirty])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        self.assertIn("rejectReason", rejected[0])
        self.assertTrue(rejected[0]["rejectReason"].startswith("excluded:"))

    def test_mock_provider_evidence_has_no_rejections_by_default(self) -> None:
        """Mock provider generates clean titles — no rejections expected."""
        provider = MockMarketPriceProvider()
        result = provider.fetch(_sample_request())
        accepted, rejected = filter_evidence_listings(result.listings)
        self.assertEqual(len(rejected), 0, "Mock provider should not generate listings with exclusion terms")
        self.assertEqual(len(accepted), len(result.listings))


# ---------------------------------------------------------------------------
# 7. Provider summary appears in report
# ---------------------------------------------------------------------------


class TestProviderSummaryInReport(unittest.TestCase):
    def test_report_has_provider_summary_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True)
            report = worker_mod.run_worker(args, root=root)

            self.assertIn("providerSummary", report)
            prov = report["providerSummary"]
            self.assertIsInstance(prov, dict)

    def test_provider_summary_has_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True)
            report = worker_mod.run_worker(args, root=root)

            prov = report["providerSummary"]
            self.assertIn("providerRequested", prov)
            self.assertIn("providerResolved", prov)
            self.assertIn("providerEnabled", prov)
            self.assertIn("liveEbayDisabled", prov)
            self.assertIn("evidenceAccepted", prov)
            self.assertIn("evidenceRejected", prov)
            self.assertIn("aggregatesBuilt", prov)

    def test_provider_summary_live_ebay_disabled_is_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True)
            report = worker_mod.run_worker(args, root=root)

            self.assertTrue(report["providerSummary"]["liveEbayDisabled"])

    def test_report_has_live_ebay_disabled_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_minimal_catalog(root)

            args = _worker_args(dry_run=True)
            report = worker_mod.run_worker(args, root=root)

            self.assertIn("liveEbayDisabledWarning", report)
            self.assertIsInstance(report["liveEbayDisabledWarning"], str)
            self.assertGreater(len(report["liveEbayDisabledWarning"]), 0)


# ---------------------------------------------------------------------------
# 8. Aggregate evidence listings unit tests
# ---------------------------------------------------------------------------


class TestAggregateEvidenceListings(unittest.TestCase):
    def test_aggregate_empty_returns_no_results(self) -> None:
        agg = aggregate_evidence_listings([])
        self.assertEqual(agg.status, "no_results")
        self.assertEqual(agg.sample_count, 0)
        self.assertIsNone(agg.median_price)

    def test_aggregate_with_listings_returns_priced(self) -> None:
        provider = MockMarketPriceProvider()
        result = provider.fetch(_sample_request())
        agg = aggregate_evidence_listings(result.listings)
        self.assertEqual(agg.status, "priced")
        self.assertIsNotNone(agg.median_price)
        self.assertIsNotNone(agg.average_price)
        self.assertGreaterEqual(agg.sample_count, 3)

    def test_aggregate_confidence_label_is_string(self) -> None:
        provider = MockMarketPriceProvider()
        result = provider.fetch(_sample_request())
        agg = aggregate_evidence_listings(result.listings)
        self.assertIn(agg.confidence_label, ("low", "medium", "high"))

    def test_aggregate_evidence_links_are_urls(self) -> None:
        provider = MockMarketPriceProvider()
        result = provider.fetch(_sample_request())
        agg = aggregate_evidence_listings(result.listings)
        self.assertGreater(len(agg.evidence_links), 0)
        for link in agg.evidence_links:
            self.assertTrue(link.startswith("https://"))


# ---------------------------------------------------------------------------
# 9. Local engine runner smoke (mock safety still works)
# ---------------------------------------------------------------------------


class TestLocalEngineRunnerSmoke(unittest.TestCase):
    """Smoke test: local engine runner mock safety still works after our changes."""

    def test_local_engine_runner_mock_safe_guard(self) -> None:
        from unittest.mock import patch
        from workers.market_price_engine_local import _assert_mock_safe

        with patch.dict(__import__("os").environ, {"MARKET_LOOKUP_PROVIDER": "mock"}):
            _assert_mock_safe()  # should not raise

    def test_local_engine_runner_blocks_live_provider(self) -> None:
        from unittest.mock import patch
        from workers.market_price_engine_local import _assert_mock_safe

        with patch.dict(__import__("os").environ, {"MARKET_LOOKUP_PROVIDER": "ebay_live"}):
            with self.assertRaises(ValueError):
                _assert_mock_safe()

    def test_local_engine_runner_report_has_market_pricing_worker_summary(self) -> None:
        from unittest.mock import patch
        import tempfile
        from workers.market_price_engine_local import run_local_engine
        from tests.test_market_engine_local_runner import (
            FakeScheduler, FakeWorkerRunner,
            _make_scheduler_factory, _make_worker_factory,
        )

        with tempfile.TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            reports_dir.mkdir()

            report = run_local_engine(
                cycles=1,
                dry_run=True,
                reports_dir=reports_dir,
                scheduler_factory=_make_scheduler_factory(FakeScheduler()),
                worker_factory=_make_worker_factory(FakeWorkerRunner()),
            )

            # market_pricing_worker_summary key should always be present
            self.assertIn("market_pricing_worker_summary", report)
            mpws = report["market_pricing_worker_summary"]
            self.assertIsInstance(mpws, dict)


if __name__ == "__main__":
    unittest.main()
