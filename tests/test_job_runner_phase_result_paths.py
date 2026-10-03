#!/usr/bin/env python3
"""Direct MarketPriceJobRunner return-path phase reporting regressions (no eBay)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.ebay_availability import EbayAvailabilitySnapshot, save_availability
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.models import (
    MarketPriceKey,
    MarketPriceRefreshJob,
    ProviderRequest,
    ProviderResult,
    SoldComp,
)
from cardscanr_market_engine.owned_daily_outcomes import (
    CHECKED_NO_NEW_EXACT_EVIDENCE,
    UPDATED_FROM_EBAY,
)
from cardscanr_market_engine.providers.errors import ProviderTemporaryError
from cardscanr_market_engine.providers.post_sold_capture import (
    POST_SOLD_CAPTURE_READY,
)


PHASE_META = {
    "x11SoldStateVerified": True,
    "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
    "parsePhase": "PARSE_COMPLETE",
    "stageTimings": {
        "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
        "parsePhase": "PARSE_COMPLETE",
    },
}


def _key() -> MarketPriceKey:
    return MarketPriceKey(
        id="key-phase-1",
        game="pokemon",
        card_name="Pikachu",
        normalized_card_name="pikachu",
        set_name="Ascended Heroes",
        set_code="me2pt5",
        collector_number="55",
        language="en",
        variant="raw",
        condition="raw",
        market_country="au",
        currency="aud",
        fingerprint="pokemon|en|me2pt5|55|pikachu|raw|raw|au|aud",
    )


def _job() -> MarketPriceRefreshJob:
    return MarketPriceRefreshJob(
        id="job-phase-1",
        price_key_id="key-phase-1",
        reason="unit_test_phase",
        priority=10,
        status="running",
        attempt_count=1,
    )


def _comp() -> SoldComp:
    return SoldComp(
        source_listing_id="itm-1",
        title="Pikachu 55 Ascended Heroes Pokemon",
        sold_price=3.5,
        shipping_price=0.26,
        total_price=3.76,
        currency="AUD",
        sold_date=datetime(2026, 9, 30, tzinfo=timezone.utc),
        listing_url="https://www.ebay.com.au/itm/1",
        condition_text="Raw",
        raw_metadata={"url_quality": "direct_item"},
    )


class _Client:
    def __init__(self, *, prior_price: float | None = 3.13) -> None:
        self.prior_price = prior_price
        self.failed = None
        self.cancelled = None
        self.completed = None

    def get_price_key(self, price_key_id: str) -> MarketPriceKey:
        return _key()

    def get_cache_row(self, price_key_id: str):
        if self.prior_price is None:
            return None
        return {
            "id": "cache-1",
            "current_market_price": self.prior_price,
            "last_updated_at": "2026-09-01T00:00:00Z",
            "next_refresh_due_at": "2026-09-02T00:00:00Z",
        }

    def insert_snapshot(self, payload: dict) -> dict:
        return {"id": "snap-1", **payload}

    def insert_evidence(self, rows: list[dict]) -> list[dict]:
        return rows

    def upsert_cache(self, payload: dict) -> dict:
        return {"id": "cache-1", **payload}

    def complete_job(self, **kwargs):
        self.completed = kwargs
        return kwargs

    def fail_job(self, **kwargs):
        self.failed = kwargs
        return kwargs

    def cancel_job(self, **kwargs):
        self.cancelled = kwargs
        return kwargs

    def mark_cache_checked_no_new_evidence(self, **kwargs):
        return kwargs

    def mark_cache_failure(self, **kwargs):
        return kwargs

    def count_recent_same_failures(self, **kwargs) -> int:
        return 0


class _ProviderOk:
    marketplace_name = "ebay"

    def fetch_comps(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(
            provider_name="ebay_browser",
            marketplace=request.provider_marketplace_id,
            provider_fingerprint="ebay:test",
            query_used="Pikachu 55 ascended heroes Pokemon",
            comps=[_comp()],
            raw_metadata={
                **PHASE_META,
                "marketCountry": request.market_country,
                "currency": request.currency,
                "providerMarketplaceId": request.provider_marketplace_id,
                "providerDomain": request.provider_domain,
                "qualitySummary": {
                    "direct_item_url_count": 1,
                    "generic_url_count": 0,
                    "missing_url_count": 0,
                },
            },
        )


class _ProviderSparse:
    marketplace_name = "ebay"

    def fetch_comps(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(
            provider_name="ebay_browser",
            marketplace=request.provider_marketplace_id,
            provider_fingerprint="ebay:test",
            query_used="Pikachu 55 ascended heroes Pokemon",
            comps=[],
            raw_metadata={
                **PHASE_META,
                "marketCountry": request.market_country,
                "currency": request.currency,
                "providerMarketplaceId": request.provider_marketplace_id,
                "providerDomain": request.provider_domain,
                "qualitySummary": {
                    "direct_item_url_count": 0,
                    "generic_url_count": 0,
                    "missing_url_count": 0,
                },
                "noReliablePriceReason": "no_clean_exact_comps",
            },
        )


class _ProviderParseFail:
    marketplace_name = "ebay"

    def fetch_comps(self, request: ProviderRequest) -> ProviderResult:
        raise ProviderTemporaryError(
            "PARSE_FAILED after capture",
            diagnostics={
                "ownedDailyOutcome": "PARSE_FAILED",
                "x11SoldStateVerified": True,
                "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
                "parsePhase": "PARSE_FAILED",
                "stageTimings": {
                    "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
                    "parsePhase": "PARSE_FAILED",
                },
            },
        )


class _ProviderWriteFail(_ProviderOk):
    pass


class JobRunnerPhasePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.avail = root / "avail.json"
        self.ops = root / "ops.json"
        os.environ["EBAY_AVAILABILITY_STATE_PATH"] = str(self.avail)
        os.environ["MARKET_OPS_STATE_PATH"] = str(self.ops)
        os.environ["CONTROL_PLANE_INCIDENTS_PATH"] = str(root / "incidents.json")
        os.environ["MARKET_WORKER_ALLOWED_MARKETS"] = "AU,US,GB,CA"
        os.environ["MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS"] = ""
        save_availability(
            EbayAvailabilitySnapshot(state="HEALTHY", market="AU"),
            path=self.avail,
            force=True,
        )
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("EBAY_AVAILABILITY_STATE_PATH", None))
        self.addCleanup(lambda: os.environ.pop("MARKET_OPS_STATE_PATH", None))
        self.addCleanup(lambda: os.environ.pop("CONTROL_PLANE_INCIDENTS_PATH", None))

    def _runner(self, provider, client=None):
        return MarketPriceJobRunner(
            client=client or _Client(),
            provider=provider,
            config=MarketEngineConfig.from_env(require_supabase=False),
            now_func=lambda: datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
            logger=lambda *_a, **_k: None,
        )

    def _assert_phases(self, result: dict) -> None:
        self.assertIn("providerDiagnostics", result)
        self.assertEqual(result.get("postSoldCapturePhase"), POST_SOLD_CAPTURE_READY)
        self.assertIsNotNone(result.get("parsePhase"))
        self.assertTrue(result.get("x11SoldStateVerified"))

    def test_01_success_write_path(self) -> None:
        result = self._runner(_ProviderOk()).run_job(_job())
        self.assertEqual(result.get("status"), "completed")
        self.assertIn(result.get("ownedDailyOutcome"), {UPDATED_FROM_EBAY, "UPDATED_FROM_EBAY", "UNCHANGED_FROM_EBAY"})
        self._assert_phases(result)

    def test_02_sparse_checked_no_new_exact_evidence(self) -> None:
        result = self._runner(_ProviderSparse(), client=_Client(prior_price=3.13)).run_job(_job())
        self.assertEqual(result.get("ownedDailyOutcome"), CHECKED_NO_NEW_EXACT_EVIDENCE)
        self._assert_phases(result)

    def test_03_parse_failure_keeps_capture_ack(self) -> None:
        result = self._runner(_ProviderParseFail(), client=_Client(prior_price=3.13)).run_job(_job())
        self.assertEqual(result.get("status"), "failed")
        self.assertEqual(result.get("postSoldCapturePhase"), POST_SOLD_CAPTURE_READY)
        self.assertEqual(result.get("parsePhase"), "PARSE_FAILED")
        self.assertIn("providerDiagnostics", result)

    def test_04_production_write_failure(self) -> None:
        class BoomClient(_Client):
            def insert_snapshot(self, payload: dict) -> dict:
                raise RuntimeError("snapshot write failed")

        result = self._runner(_ProviderOk(), client=BoomClient()).run_job(_job())
        self.assertEqual(result.get("status"), "failed")
        self._assert_phases(result)

    def test_05_exception_converted_to_checked_no_new(self) -> None:
        class SparseMsgProvider:
            marketplace_name = "ebay"

            def fetch_comps(self, request: ProviderRequest) -> ProviderResult:
                raise ProviderTemporaryError(
                    "no_reliable_price:no_clean_exact_comps",
                    diagnostics={
                        "x11SoldStateVerified": True,
                        "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
                        "parsePhase": "PARSE_COMPLETE",
                    },
                )

        result = self._runner(SparseMsgProvider(), client=_Client(prior_price=3.13)).run_job(_job())
        self.assertEqual(result.get("ownedDailyOutcome"), CHECKED_NO_NEW_EXACT_EVIDENCE)
        self._assert_phases(result)

    def test_06_failure_path_with_provider_diagnostics(self) -> None:
        class Boom:
            marketplace_name = "ebay"

            def fetch_comps(self, request: ProviderRequest) -> ProviderResult:
                raise ProviderTemporaryError(
                    "POST_SOLD_CAPTURE_FAILURE: CDP_TIMEOUT",
                    diagnostics={
                        "ownedDailyOutcome": "POST_SOLD_CAPTURE_FAILURE",
                        "x11SoldStateVerified": True,
                        "postSoldCapturePhase": "CDP_TIMEOUT",
                        "parsePhase": "PARSE_FAILED",
                    },
                )

        result = self._runner(Boom(), client=_Client(prior_price=None)).run_job(_job())
        self.assertEqual(result.get("status"), "failed")
        self.assertIn("providerDiagnostics", result)
        self.assertEqual(result.get("postSoldCapturePhase"), "CDP_TIMEOUT")
        self.assertEqual(result.get("parsePhase"), "PARSE_FAILED")

    def test_07_missing_capture_ack_not_inferred_from_price(self) -> None:
        class NoPhaseProvider(_ProviderOk):
            def fetch_comps(self, request: ProviderRequest) -> ProviderResult:
                base = super().fetch_comps(request)
                meta = dict(base.raw_metadata)
                meta.pop("postSoldCapturePhase", None)
                st = dict(meta.get("stageTimings") or {})
                st.pop("postSoldCapturePhase", None)
                meta["stageTimings"] = st
                meta["x11SoldStateVerified"] = True
                return ProviderResult(
                    provider_name=base.provider_name,
                    marketplace=base.marketplace,
                    provider_fingerprint=base.provider_fingerprint,
                    query_used=base.query_used,
                    comps=base.comps,
                    raw_metadata=meta,
                )

        result = self._runner(NoPhaseProvider()).run_job(_job())
        # Write may succeed, but capture phase must remain absent (not invented).
        self.assertIsNone(result.get("postSoldCapturePhase"))
        self.assertIn("providerDiagnostics", result)

    def test_08_challenge_short_circuit_blocks_without_inventing_ready(self) -> None:
        save_availability(
            EbayAvailabilitySnapshot(state="CHALLENGE_REQUIRED", market="AU"),
            path=self.avail, force=True)
        result = self._runner(_ProviderOk()).run_job(_job())
        self.assertEqual(result.get("status"), "failed")
        # No successful capture phase invented from a blocked gate.
        self.assertNotEqual(result.get("postSoldCapturePhase"), POST_SOLD_CAPTURE_READY)


if __name__ == "__main__":
    unittest.main()
