from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig
from cardscanr_market_engine.job_runner import MarketPriceJobRunner
from cardscanr_market_engine.models import MarketPriceKey, MarketPriceRefreshJob, ProviderResult, SoldComp


def fixed_config() -> MarketEngineConfig:
    return MarketEngineConfig.from_env(require_supabase=False)


def sample_key() -> MarketPriceKey:
    return MarketPriceKey(
        id="key-1",
        game="pokemon",
        card_name="Charizard",
        normalized_card_name="charizard",
        set_name="Base Set",
        set_code="base1",
        collector_number="4",
        language="en",
        variant="raw",
        condition="near_mint",
        market_country="us",
        currency="usd",
        fingerprint="pokemon|en|base1|4|charizard|raw|near_mint|us|usd",
    )


class FakeProvider:
    def __init__(self) -> None:
        self.price_key: MarketPriceKey | None = None

    def fetch_comps(self, price_key: MarketPriceKey) -> ProviderResult:
        self.price_key = price_key
        return ProviderResult(
            provider_name="mock",
            marketplace="mock_ebay_sold",
            provider_fingerprint="mock:123",
            query_used="charizard base set 4",
            comps=[
                SoldComp(
                    source_listing_id="included-1",
                    title="Charizard Base Set 4 raw",
                    sold_price=19.0,
                    shipping_price=1.0,
                    total_price=20.0,
                    currency="USD",
                    sold_date=datetime(2026, 5, 20, tzinfo=timezone.utc),
                    listing_url="https://example.test/included-1",
                    condition_text="Raw",
                ),
                SoldComp(
                    source_listing_id="included-2",
                    title="Charizard Base Set 4 raw",
                    sold_price=21.0,
                    shipping_price=1.0,
                    total_price=22.0,
                    currency="USD",
                    sold_date=datetime(2026, 5, 19, tzinfo=timezone.utc),
                    listing_url="https://example.test/included-2",
                    condition_text="Raw",
                ),
                SoldComp(
                    source_listing_id="graded-1",
                    title="Charizard Base Set 4 PSA 10 graded",
                    sold_price=100.0,
                    shipping_price=0.0,
                    total_price=100.0,
                    currency="USD",
                    sold_date=datetime(2026, 5, 18, tzinfo=timezone.utc),
                    listing_url="https://example.test/graded-1",
                    condition_text="PSA 10",
                ),
            ],
        )


class FakeClient:
    def __init__(self) -> None:
        self.snapshot_payload: dict | None = None
        self.evidence_rows: list[dict] | None = None
        self.cache_payload: dict | None = None
        self.completed: dict | None = None
        self.failed: dict | None = None

    def claim_jobs(self, *, worker_id: str, max_jobs: int) -> list[MarketPriceRefreshJob]:
        return []

    def get_price_key(self, price_key_id: str) -> MarketPriceKey:
        return sample_key()

    def insert_snapshot(self, payload: dict) -> dict:
        self.snapshot_payload = payload
        return {"id": "snapshot-1"}

    def insert_evidence(self, rows: list[dict]) -> list[dict]:
        self.evidence_rows = rows
        return rows

    def upsert_cache(self, payload: dict) -> dict:
        self.cache_payload = payload
        return payload

    def complete_job(self, **kwargs) -> dict:
        self.completed = kwargs
        return kwargs

    def fail_job(self, **kwargs) -> dict:
        self.failed = kwargs
        return kwargs


class JobRunnerTests(unittest.TestCase):
    def test_job_runner_prepares_snapshot_cache_and_evidence_payloads(self) -> None:
        provider = FakeProvider()
        client = FakeClient()
        runner = MarketPriceJobRunner(
            client=client,
            provider=provider,
            config=fixed_config(),
            now_func=lambda: datetime(2026, 5, 25, tzinfo=timezone.utc),
            logger=lambda *_args, **_kwargs: None,
        )
        result = runner.run_job(
            MarketPriceRefreshJob(
                id="job-1",
                price_key_id="key-1",
                reason="user_refresh",
                priority=10,
                status="running",
                attempt_count=1,
            )
        )

        self.assertEqual(provider.price_key, sample_key())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(client.snapshot_payload["diagnostics_json"]["providerFingerprint"], "mock:123")
        self.assertEqual(client.cache_payload["latest_snapshot_id"], "snapshot-1")
        self.assertEqual(client.cache_payload["current_market_price"], 21.0)
        self.assertEqual(len(client.evidence_rows or []), 3)
        self.assertEqual(client.evidence_rows[2]["rejection_reason"], "graded_for_raw_request")
        self.assertIsNotNone(client.completed)
        self.assertIsNone(client.failed)

    def test_job_runner_calls_fail_rpc_on_provider_error(self) -> None:
        class BrokenProvider:
            def fetch_comps(self, price_key: MarketPriceKey) -> ProviderResult:
                raise RuntimeError("boom")

        client = FakeClient()
        runner = MarketPriceJobRunner(
            client=client,
            provider=BrokenProvider(),
            config=fixed_config(),
            now_func=lambda: datetime(2026, 5, 25, tzinfo=timezone.utc),
            logger=lambda *_args, **_kwargs: None,
        )
        result = runner.run_job(
            MarketPriceRefreshJob(
                id="job-2",
                price_key_id="key-1",
                reason="user_refresh",
                priority=10,
                status="running",
                attempt_count=1,
            )
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(client.failed, {"job_id": "job-2", "error_message": "boom"})


if __name__ == "__main__":
    unittest.main()
