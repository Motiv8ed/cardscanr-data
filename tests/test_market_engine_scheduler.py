from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.scheduler import (
    MarketPriceRefreshScheduler,
    MarketSchedulerConfig,
    sanitize_scheduler_report,
)


class FakeSchedulerClient:
    def __init__(
        self,
        *,
        missing_rows: list[dict] | None = None,
        stale_rows: list[dict] | None = None,
        active_jobs: dict[str, dict] | None = None,
    ) -> None:
        self.missing_rows = missing_rows or []
        self.stale_rows = stale_rows or []
        self.active_jobs = active_jobs or {}
        self.enqueued: list[dict] = []

    def list_missing_cache_keys(self, **_kwargs) -> list[dict]:
        return list(self.missing_rows)

    def list_stale_cache_keys(self, **_kwargs) -> list[dict]:
        return list(self.stale_rows)

    def get_active_jobs_for_keys(self, *, price_key_ids: list[str]) -> dict[str, dict]:
        return {key: value for key, value in self.active_jobs.items() if key in set(price_key_ids)}

    def enqueue_refresh_job(self, *, price_key_id: str, reason: str, priority: int, dedupe_key: str | None) -> dict:
        payload = {
            "id": f"job-{len(self.enqueued) + 1}",
            "price_key_id": price_key_id,
            "reason": reason,
            "priority": priority,
            "dedupe_key": dedupe_key,
            "status": "queued",
        }
        self.enqueued.append(payload)
        return payload


def fixed_config(*, dry_run: bool = False, max_enqueues: int = 50) -> MarketSchedulerConfig:
    return MarketSchedulerConfig(
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="secret",
        max_keys_per_run=100,
        max_enqueues_per_run=max_enqueues,
        include_missing_cache=True,
        include_stale_cache=True,
        min_popularity_score=0,
        min_inventory_count=0,
        dry_run=dry_run,
        poll_seconds=300,
        latest_report_path=ROOT / "reports" / "market_price_scheduler_latest.json",
        runs_report_path=ROOT / "reports" / "market_price_scheduler_runs.jsonl",
    )


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class MarketEngineSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 5, 25, tzinfo=timezone.utc)

    def scheduler_for(self, client: FakeSchedulerClient, *, dry_run: bool = False, max_enqueues: int = 50) -> MarketPriceRefreshScheduler:
        return MarketPriceRefreshScheduler(
            client=client,
            config=fixed_config(dry_run=dry_run, max_enqueues=max_enqueues),
            now_func=lambda: self.now,
        )

    def test_missing_cache_candidate_gets_priority_50(self) -> None:
        scheduler = self.scheduler_for(FakeSchedulerClient())
        decision = scheduler.evaluate_candidate(
            {
                "id": "k1",
                "has_cache": False,
                "popularity_score": 2,
                "inventory_count": 1,
                "last_seen_at": iso(self.now),
            },
            now=self.now,
        )
        self.assertTrue(decision.should_enqueue)
        self.assertEqual(decision.priority, 50)

    def test_stale_high_value_cache_gets_higher_priority_than_normal_stale(self) -> None:
        scheduler = self.scheduler_for(FakeSchedulerClient())
        high = scheduler.evaluate_candidate(
            {
                "id": "k1",
                "has_cache": True,
                "stale_after": iso(self.now - timedelta(hours=1)),
                "current_market_price": 150,
                "recommended_price": 140,
                "popularity_score": 1,
                "inventory_count": 0,
            },
            now=self.now,
        )
        normal = scheduler.evaluate_candidate(
            {
                "id": "k2",
                "has_cache": True,
                "stale_after": iso(self.now - timedelta(hours=1)),
                "current_market_price": 20,
                "recommended_price": 18,
                "popularity_score": 1,
                "inventory_count": 0,
            },
            now=self.now,
        )
        self.assertEqual(high.priority, 80)
        self.assertEqual(normal.priority, 100)
        self.assertLess(high.priority or 999, normal.priority or 999)

    def test_popular_stale_cache_gets_priority_90(self) -> None:
        scheduler = self.scheduler_for(FakeSchedulerClient())
        decision = scheduler.evaluate_candidate(
            {
                "id": "k1",
                "has_cache": True,
                "stale_after": iso(self.now - timedelta(hours=1)),
                "current_market_price": 25,
                "recommended_price": 24,
                "popularity_score": 16,
                "inventory_count": 0,
            },
            now=self.now,
        )
        self.assertEqual(decision.priority, 90)

    def test_fresh_cache_is_skipped(self) -> None:
        scheduler = self.scheduler_for(FakeSchedulerClient())
        decision = scheduler.evaluate_candidate(
            {
                "id": "k1",
                "has_cache": True,
                "stale_after": iso(self.now + timedelta(hours=8)),
                "current_market_price": 25,
                "recommended_price": 24,
                "popularity_score": 10,
                "inventory_count": 1,
            },
            now=self.now,
        )
        self.assertFalse(decision.should_enqueue)
        self.assertEqual(decision.reason, "fresh_cache")

    def test_active_job_is_skipped_and_reported(self) -> None:
        key_id = "k-active"
        client = FakeSchedulerClient(
            stale_rows=[
                {
                    "id": key_id,
                    "fingerprint": "f-active",
                    "market_country": "us",
                    "currency": "usd",
                    "marketplace": "EBAY_US",
                    "popularity_score": 20,
                    "inventory_count": 2,
                    "last_seen_at": iso(self.now),
                    "stale_after": iso(self.now - timedelta(hours=1)),
                    "current_market_price": 40,
                    "recommended_price": 38,
                }
            ],
            active_jobs={key_id: {"id": "job-existing", "status": "queued"}},
        )
        report = self.scheduler_for(client).run_once()
        self.assertEqual(report["summary"]["jobsSkippedAlreadyActive"], 1)
        self.assertEqual(report["summary"]["jobsEnqueued"], 0)
        self.assertEqual(report["candidateDecisions"][0]["market_country"], "us")
        self.assertEqual(report["candidateDecisions"][0]["currency"], "usd")
        self.assertEqual(report["candidateDecisions"][0]["marketplace"], "EBAY_US")

    def test_dry_run_does_not_enqueue(self) -> None:
        client = FakeSchedulerClient(
            stale_rows=[
                {
                    "id": "k1",
                    "fingerprint": "f1",
                    "market_country": "us",
                    "currency": "usd",
                    "marketplace": "EBAY_US",
                    "popularity_score": 10,
                    "inventory_count": 1,
                    "last_seen_at": iso(self.now),
                    "stale_after": iso(self.now - timedelta(hours=1)),
                    "current_market_price": 30,
                    "recommended_price": 28,
                }
            ]
        )
        report = self.scheduler_for(client, dry_run=True).run_once()
        self.assertEqual(report["summary"]["jobsEnqueued"], 0)
        self.assertEqual(report["summary"]["jobsDryRunOnly"], 1)
        self.assertEqual(client.enqueued, [])

    def test_max_enqueue_limit_is_respected(self) -> None:
        stale_rows = []
        for idx in range(3):
            stale_rows.append(
                {
                    "id": f"k{idx}",
                    "fingerprint": f"f{idx}",
                    "market_country": "us",
                    "currency": "usd",
                    "marketplace": "EBAY_US",
                    "popularity_score": 0,
                    "inventory_count": 0,
                    "last_seen_at": iso(self.now),
                    "stale_after": iso(self.now - timedelta(hours=1)),
                    "current_market_price": 25 + idx,
                    "recommended_price": 20 + idx,
                }
            )
        client = FakeSchedulerClient(stale_rows=stale_rows)
        report = self.scheduler_for(client, max_enqueues=1).run_once()
        self.assertEqual(report["summary"]["jobsEnqueued"], 1)
        self.assertEqual(report["summary"]["jobsSkippedByLimit"], 2)
        self.assertEqual(len(client.enqueued), 1)

    def test_scheduler_enqueues_same_card_in_different_markets(self) -> None:
        shared_fingerprint = "pokemon|en|base1|4|charizard|raw|near_mint"
        client = FakeSchedulerClient(
            stale_rows=[
                {
                    "id": "key-au",
                    "fingerprint": f"{shared_fingerprint}|au|aud",
                    "market_country": "au",
                    "currency": "aud",
                    "marketplace": "EBAY_AU",
                    "popularity_score": 20,
                    "inventory_count": 2,
                    "last_seen_at": iso(self.now),
                    "stale_after": iso(self.now - timedelta(hours=1)),
                    "current_market_price": 40,
                    "recommended_price": 38,
                },
                {
                    "id": "key-us",
                    "fingerprint": f"{shared_fingerprint}|us|usd",
                    "market_country": "us",
                    "currency": "usd",
                    "marketplace": "EBAY_US",
                    "popularity_score": 20,
                    "inventory_count": 2,
                    "last_seen_at": iso(self.now),
                    "stale_after": iso(self.now - timedelta(hours=1)),
                    "current_market_price": 40,
                    "recommended_price": 38,
                },
            ]
        )
        report = self.scheduler_for(client, max_enqueues=2).run_once()
        self.assertEqual(report["summary"]["jobsEnqueued"], 2)
        self.assertEqual({row["price_key_id"] for row in report["enqueuedJobs"]}, {"key-au", "key-us"})

    def test_report_redacts_secrets(self) -> None:
        clean = sanitize_scheduler_report(
            {
                "status": "success",
                "supabase_service_role_key": "secret-value",
                "nested": {"apiKey": "abc", "ok": "value"},
            }
        )
        self.assertEqual(clean["supabase_service_role_key"], "***REDACTED***")
        self.assertEqual(clean["nested"]["apiKey"], "***REDACTED***")
        self.assertEqual(clean["nested"]["ok"], "value")


if __name__ == "__main__":
    unittest.main()
