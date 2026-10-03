from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import tempfile
import unittest

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.ebay_availability import EbayAvailabilitySnapshot, save_availability
from cardscanr_market_engine.filters import filter_comps
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.models import MarketPriceKey, MarketPriceRefreshJob, ProviderRequest, ProviderResult, SoldComp
from cardscanr_market_engine.providers.errors import ProviderBlockedError, ProviderTemporaryError, ProviderUnsupportedMarketError


def _config() -> MarketEngineConfig:
    return MarketEngineConfig.from_env(require_supabase=False)


def _riolu_key(*, market_country: str = "au", currency: str = "aud") -> MarketPriceKey:
    return MarketPriceKey(
        id="key-riolu-au",
        game="pokemon",
        card_name="Riolu",
        normalized_card_name="riolu",
        set_name="Prismatic Evolutions",
        set_code="sv8pt5",
        collector_number="050/131",
        language="en",
        variant="reverse_holo",
        condition="raw",
        market_country=market_country,
        currency=currency,
        fingerprint=f"pokemon|en|sv8pt5|050/131|riolu|reverse_holo|raw|{market_country}|{currency}",
    )


def _job() -> MarketPriceRefreshJob:
    return MarketPriceRefreshJob(
        id="job-1",
        price_key_id="key-riolu-au",
        reason="unit_test",
        priority=10,
        status="running",
        attempt_count=1,
    )


def _sold_comp(
    *,
    title: str = "Riolu 050/131 Prismatic Evolutions Reverse Holo Pokemon",
    currency: str = "AUD",
    listing_id: str = "1",
) -> SoldComp:
    return SoldComp(
        source_listing_id=listing_id,
        title=title,
        sold_price=4.25,
        shipping_price=1.50,
        total_price=5.75,
        currency=currency,
        sold_date=datetime(2026, 5, 20, tzinfo=timezone.utc),
        listing_url=f"https://www.ebay.com.au/itm/{listing_id}",
        condition_text="Raw",
        raw_metadata={"url_quality": "direct_item"},
    )


class _FakeClient:
    def __init__(self, price_key: MarketPriceKey) -> None:
        self.price_key = price_key
        self.snapshots: list[dict] = []
        self.evidence_rows: list[dict] = []
        self.cache_payloads: list[dict] = []
        self.completed_jobs: list[dict] = []
        self.failed_jobs: list[dict] = []
        self.cancelled_jobs: list[dict] = []

    def get_price_key(self, price_key_id: str) -> MarketPriceKey:
        self.requested_price_key_id = price_key_id
        return self.price_key

    def insert_snapshot(self, payload: dict) -> dict:
        self.snapshots.append(payload)
        return {"id": "snapshot-1", **payload}

    def insert_evidence(self, rows: list[dict]) -> list[dict]:
        self.evidence_rows.extend(rows)
        return rows

    def upsert_cache(self, payload: dict) -> dict:
        row = {"id": "cache-1", **payload}
        self.cache_payloads.append(row)
        return row

    def complete_job(self, **kwargs: object) -> dict:
        self.completed_jobs.append(dict(kwargs))
        return {"status": "completed", **kwargs}

    def fail_job(self, **kwargs: object) -> dict:
        self.failed_jobs.append(dict(kwargs))
        return {"status": "failed", **kwargs}

    def cancel_job(self, **kwargs: object) -> dict:
        self.cancelled_jobs.append(dict(kwargs))
        return {"status": "cancelled", **kwargs}

    def mark_cache_failure(self, **kwargs: object) -> dict:
        self.cache_payloads.append({"refresh_status": "failed", **kwargs})
        return dict(kwargs)

    def mark_cache_checked_no_new_evidence(self, **kwargs: object) -> dict:
        self.cache_payloads.append({"refresh_status": "completed", **kwargs})
        return dict(kwargs)

    def count_recent_same_failures(self, *, price_key_id: str, error_message: str) -> int:
        return 0

    def get_cache_row(self, *, price_key_id: str):
        return getattr(self, "_cache_row", None)


class _StaticProvider:
    provider_name = "unit_provider"
    marketplace_name = "ebay"

    def __init__(self, comps: list[SoldComp]) -> None:
        self.comps = comps

    def fetch_comps(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(
            provider_name=self.provider_name,
            marketplace=request.provider_marketplace_id,
            provider_fingerprint="unit:fingerprint",
            query_used="Riolu 050/131 Pokemon",
            comps=self.comps,
            raw_metadata={
                "marketCountry": request.market_country,
                "currency": request.currency,
                "providerMarketplaceId": request.provider_marketplace_id,
                "providerDomain": request.provider_domain,
            },
        )


class _FailingProvider:
    provider_name = "unit_provider"
    marketplace_name = "ebay"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def fetch_comps(self, request: ProviderRequest) -> ProviderResult:
        raise self.exc


class _CountingFailingProvider(_FailingProvider):
    marketplace_name = "ebay"

    def __init__(self, exc: Exception) -> None:
        super().__init__(exc)
        self.calls: list[str] = []

    def fetch_comps(self, request: ProviderRequest) -> ProviderResult:
        self.calls.append(request.provider_marketplace_id)
        raise self.exc


class MarketPriceJobRunnerCacheStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        avail = root / "ebay_availability_state.json"
        ops = root / "marketplace_ops_state.json"
        incidents = root / "control_plane_incidents.json"
        save_availability(
            EbayAvailabilitySnapshot(
                state="HEALTHY",
                market="AU",
                confirmed_healthy=True,
                updated_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
            ),
            path=avail, force=True)
        ops.write_text('{"version":1,"markets":{}}\n', encoding="utf-8")
        incidents.write_text('{"version":1,"incidents":{}}\n', encoding="utf-8")
        self._prev_env = {
            "EBAY_AVAILABILITY_STATE_PATH": os.environ.get("EBAY_AVAILABILITY_STATE_PATH"),
            "MARKET_OPS_STATE_PATH": os.environ.get("MARKET_OPS_STATE_PATH"),
            "CONTROL_PLANE_INCIDENTS_PATH": os.environ.get("CONTROL_PLANE_INCIDENTS_PATH"),
            "MARKET_WORKER_ALLOWED_MARKETS": os.environ.get("MARKET_WORKER_ALLOWED_MARKETS"),
            "MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS": os.environ.get("MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS"),
        }
        os.environ["EBAY_AVAILABILITY_STATE_PATH"] = str(avail)
        os.environ["MARKET_OPS_STATE_PATH"] = str(ops)
        os.environ["CONTROL_PLANE_INCIDENTS_PATH"] = str(incidents)
        os.environ["MARKET_WORKER_ALLOWED_MARKETS"] = "AU,US,GB,CA,NZ"
        os.environ["MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS"] = "NONE"

    def tearDown(self) -> None:
        for key, value in self._prev_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmpdir.cleanup()
    def test_supported_au_aud_job_creates_cache_snapshot_and_evidence(self) -> None:
        client = _FakeClient(_riolu_key())
        runner = MarketPriceJobRunner(
            client=client,
            provider=_StaticProvider([_sold_comp()]),
            config=_config(),
            now_func=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
            logger=lambda _message: None,
        )

        result = runner.run_job(_job())

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["marketCountry"], "AU")
        self.assertEqual(result["currency"], "AUD")
        self.assertEqual(result["cacheRowId"], "cache-1")
        self.assertEqual(result["snapshotId"], "snapshot-1")
        self.assertEqual(len(client.snapshots), 1)
        self.assertEqual(len(client.evidence_rows), 1)
        self.assertEqual(len(client.cache_payloads), 1)
        cache = client.cache_payloads[0]
        self.assertEqual(cache["market_country"], "AU")
        self.assertEqual(cache["currency"], "AUD")
        self.assertEqual(cache["sample_size"], 1)
        self.assertEqual(cache["current_market_price"], 4.25)

    def test_no_evidence_never_priced_is_no_price_ever_found(self) -> None:
        client = _FakeClient(_riolu_key())
        runner = MarketPriceJobRunner(
            client=client,
            provider=_StaticProvider([]),
            config=_config(),
            now_func=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
            logger=lambda _message: None,
        )

        result = runner.run_job(_job())

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["ownedDailyOutcome"], "NO_PRICE_EVER_FOUND")
        self.assertIn("no_reliable_price", result["error"])
        self.assertEqual(len(client.failed_jobs), 1)

    def test_sparse_market_with_prior_good_is_checked_no_new_exact_evidence(self) -> None:
        client = _FakeClient(_riolu_key())
        client._cache_row = {
            "current_market_price": 4.25,
            "provider": "ebay_browser",
            "last_updated_at": "2026-05-20T00:00:00+00:00",
            "latest_snapshot_id": "snap-prior",
            "sample_size": 3,
            "refresh_status": "completed",
            "next_refresh_due_at": "2026-05-21T00:00:00+00:00",
        }
        job = _job()
        job = MarketPriceRefreshJob(
            id=job.id,
            price_key_id=job.price_key_id,
            reason="owned_daily:force_sparse_proof",
            priority=job.priority,
            status=job.status,
            attempt_count=job.attempt_count,
        )
        runner = MarketPriceJobRunner(
            client=client,
            provider=_StaticProvider([]),
            config=_config(),
            now_func=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
            logger=lambda _message: None,
        )

        result = runner.run_job(job)

        self.assertEqual(result["status"], "checked_no_new_exact_evidence")
        self.assertEqual(result["ownedDailyOutcome"], "CHECKED_NO_NEW_EXACT_EVIDENCE")
        self.assertEqual(result["retainedPrice"], 4.25)
        self.assertTrue(result["lastGoodRetained"])
        self.assertEqual(result["lastUpdatedAt"], "2026-05-20T00:00:00+00:00")
        self.assertEqual(len(client.cancelled_jobs), 1)
        self.assertEqual(client.cancelled_jobs[0]["reason"], "CHECKED_NO_NEW_EXACT_EVIDENCE")
        self.assertEqual(len(client.failed_jobs), 0)
        self.assertEqual(len(client.snapshots), 0)
        checked = client.cache_payloads[-1]
        self.assertEqual(checked["refresh_status"], "completed")
        self.assertNotIn("current_market_price", checked)
        self.assertNotIn("last_updated_at", checked)

    def test_provider_failure_marks_job_failed_clearly(self) -> None:
        client = _FakeClient(_riolu_key())
        runner = MarketPriceJobRunner(
            client=client,
            provider=_FailingProvider(ProviderTemporaryError("provider timeout")),
            config=_config(),
            now_func=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
            logger=lambda _message: None,
        )

        result = runner.run_job(_job())

        self.assertEqual(result["status"], "failed")
        self.assertIn("provider timeout", result["error"])
        self.assertEqual(result["providerDiagnostics"]["providerErrorCode"], "provider_temporary")
        self.assertEqual(result["ownedDailyOutcome"], "TEMPORARY_BROWSER_FAILURE")
        self.assertEqual(len(client.failed_jobs), 1)
        self.assertIn("provider timeout", client.failed_jobs[0]["error_message"])
        self.assertEqual(len(client.cache_payloads), 1)
        self.assertEqual(client.cache_payloads[0]["refresh_status"], "failed")

    def test_provider_block_stops_marketplace_fallback(self) -> None:
        client = _FakeClient(_riolu_key())
        provider = _CountingFailingProvider(
            ProviderBlockedError(
                "challenge detected",
                diagnostics={"providerOutcome": "challenge_detected"},
            )
        )
        runner = MarketPriceJobRunner(
            client=client,
            provider=provider,
            config=_config(),
            now_func=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
            logger=lambda _message: None,
        )

        result = runner.run_job(_job())

        self.assertEqual(provider.calls, ["EBAY_AU"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["providerDiagnostics"]["providerErrorCode"], "provider_blocked")
        self.assertEqual(result["providerDiagnostics"]["diagnostics"]["providerOutcome"], "challenge_detected")

    def test_unsupported_market_provider_failure_is_reported(self) -> None:
        client = _FakeClient(_riolu_key(market_country="de", currency="eur"))
        runner = MarketPriceJobRunner(
            client=client,
            provider=_FailingProvider(ProviderUnsupportedMarketError("unsupported market")),
            config=_config(),
            now_func=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
            logger=lambda _message: None,
        )

        result = runner.run_job(_job())

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["providerDiagnostics"]["providerErrorCode"], "provider_unsupported_market")
        self.assertEqual(len(client.failed_jobs), 1)

    def test_evidence_normalization_rejects_bad_listings(self) -> None:
        key = _riolu_key()
        comps = [
            _sold_comp(title="Pikachu 050/131 Prismatic Evolutions Japanese Reverse Holo Pokemon", listing_id="wrong-card"),
            _sold_comp(title="Riolu 051/131 Prismatic Evolutions Japanese Reverse Holo Pokemon", listing_id="wrong-number"),
            _sold_comp(title="Riolu 050/131 SV9 Japanese Reverse Holo Pokemon", listing_id="wrong-set"),
            _sold_comp(title="Riolu 050/131 English Prismatic Evolutions Reverse Holo Pokemon", listing_id="wrong-language"),
            _sold_comp(title="Riolu 050/131 Prismatic Evolutions Japanese Reverse Holo booster pack sealed", listing_id="sealed"),
            _sold_comp(title="Riolu 050/131 Prismatic Evolutions Japanese Reverse Holo lot of 10 cards", listing_id="lot"),
            _sold_comp(title="Riolu 050/131 Prismatic Evolutions Japanese Reverse Holo PSA 10 graded", listing_id="graded"),
        ]
        jp_key = MarketPriceKey(**{**key.__dict__, "language": "jp"})

        reasons = {item.comp.source_listing_id: item.rejection_reason for item in filter_comps(jp_key, comps)}

        self.assertEqual(reasons["wrong-card"], "wrong_card_name")
        self.assertEqual(reasons["wrong-number"], "wrong_collector_number")
        self.assertEqual(reasons["wrong-set"], "wrong_set")
        self.assertEqual(reasons["wrong-language"], "wrong_language")
        self.assertEqual(reasons["sealed"], "sealed_product_for_single_card_request")
        self.assertEqual(reasons["lot"], "likely_bundle_lot")
        self.assertEqual(reasons["graded"], "graded_for_raw_request")


    def test_skipped_already_fresh_cancels_without_failing_cache(self) -> None:
        client = _FakeClient(_riolu_key())
        client._cache_row = {
            "current_market_price": 4.25,
            "next_refresh_due_at": "2099-01-01T00:00:00+00:00",
        }
        runner = MarketPriceJobRunner(
            client=client,
            provider=_StaticProvider([_sold_comp()]),
            config=_config(),
            now_func=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
            logger=lambda _message: None,
        )
        result = runner.run_job(_job())
        self.assertEqual(result["status"], "skipped_already_fresh")
        self.assertEqual(result.get("outcomeClass"), "already_fresh_noop")
        self.assertEqual(len(client.cancelled_jobs), 1)
        self.assertEqual(client.cancelled_jobs[0]["reason"], "skipped_already_fresh")
        self.assertEqual(len(client.failed_jobs), 0)
        self.assertEqual(len(client.cache_payloads), 0)


if __name__ == "__main__":
    unittest.main()
