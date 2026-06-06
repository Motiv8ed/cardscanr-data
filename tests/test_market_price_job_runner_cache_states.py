from __future__ import annotations

from datetime import datetime, timezone
import unittest

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.filters import filter_comps
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.models import MarketPriceKey, MarketPriceRefreshJob, ProviderRequest, ProviderResult, SoldComp
from cardscanr_market_engine.providers.errors import ProviderTemporaryError, ProviderUnsupportedMarketError


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


class MarketPriceJobRunnerCacheStateTests(unittest.TestCase):
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

    def test_no_evidence_found_writes_null_price_cache(self) -> None:
        client = _FakeClient(_riolu_key())
        runner = MarketPriceJobRunner(
            client=client,
            provider=_StaticProvider([]),
            config=_config(),
            now_func=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
            logger=lambda _message: None,
        )

        result = runner.run_job(_job())

        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["recommendedPrice"])
        self.assertEqual(result["includedCount"], 0)
        self.assertEqual(len(client.cache_payloads), 1)
        cache = client.cache_payloads[0]
        self.assertIsNone(cache["current_market_price"])
        self.assertIsNone(cache["recommended_price"])
        self.assertEqual(cache["sample_size"], 0)
        self.assertEqual(cache["refresh_status"], "completed")
        self.assertEqual(client.snapshots[0]["diagnostics_json"]["no_reliable_price_reason"], "no_comps_parsed")

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
        self.assertEqual(len(client.failed_jobs), 1)
        self.assertIn("provider timeout", client.failed_jobs[0]["error_message"])
        self.assertEqual(client.cache_payloads, [])

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
            _sold_comp(title="Pikachu 050/131 Prismatic Evolutions Reverse Holo Pokemon", listing_id="wrong-card"),
            _sold_comp(title="Riolu 051/131 Prismatic Evolutions Reverse Holo Pokemon", listing_id="wrong-number"),
            _sold_comp(title="Riolu 050/131 SV9 Reverse Holo Pokemon", listing_id="wrong-set"),
            _sold_comp(title="Riolu 050/131 English Prismatic Evolutions Reverse Holo Pokemon", listing_id="wrong-language"),
            _sold_comp(title="Riolu 050/131 Prismatic Evolutions Reverse Holo booster pack sealed", listing_id="sealed"),
            _sold_comp(title="Riolu 050/131 Prismatic Evolutions Reverse Holo lot of 10 cards", listing_id="lot"),
            _sold_comp(title="Riolu 050/131 Prismatic Evolutions Reverse Holo PSA 10 graded", listing_id="graded"),
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


if __name__ == "__main__":
    unittest.main()
