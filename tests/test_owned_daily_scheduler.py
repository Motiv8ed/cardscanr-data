from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.owned_daily_scheduler import (
    OwnedDailySchedulerConfig,
    OwnedPrintingRefreshScheduler,
)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeOwnedClient:
    def __init__(self, targets: list[dict]) -> None:
        self.targets = targets
        self.enqueued: list[dict] = []
        self.synced = False
        self.active_jobs: dict[str, dict] = {}

    def list_owned_market_pricing_targets(self, **_kwargs) -> dict:
        return {"targetCount": len(self.targets), "targets": list(self.targets)}

    def sync_owned_market_price_keys(self) -> dict:
        self.synced = True
        return {"ensuredKeys": len(self.targets), "createdKeys": 0}

    def owned_price_health_report(self) -> dict:
        return {"usersWithOwnedCards": 2, "marketSpecificKeys": len(self.targets)}

    def get_active_jobs_for_keys(self, *, price_key_ids: list[str]) -> dict:
        return {k: v for k, v in self.active_jobs.items() if k in set(price_key_ids)}

    def count_refresh_queue_depth(self) -> int:
        return 0

    def enqueue_refresh_job(self, *, price_key_id: str, reason: str, priority: int, dedupe_key: str | None) -> dict:
        row = {
            "id": f"job-{len(self.enqueued) + 1}",
            "price_key_id": price_key_id,
            "reason": reason,
            "priority": priority,
            "dedupe_key": dedupe_key,
            "status": "queued",
        }
        self.enqueued.append(row)
        return row

    def ensure_market_price_key_from_owned_target(self, target: dict) -> str:
        return str(target.get("market_price_key_id") or f"created-{target.get('fingerprint')}")


def config(*, max_enqueues: int = 50, dry_run: bool = False) -> OwnedDailySchedulerConfig:
    return OwnedDailySchedulerConfig(
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="secret",
        max_enqueues_per_run=max_enqueues,
        queue_low_watermark=0,
        queue_high_watermark=0,
        dry_run=dry_run,
        sync_keys_before_run=True,
        allowed_markets=["AU", "US", "GB", "CA"],
        rolling_window_hours=24,
        success_fresh_hours=24,
        latest_report_path=ROOT / "reports" / "owned_price_scheduler_latest.json",
        runs_report_path=ROOT / "reports" / "owned_price_scheduler_runs.jsonl",
        enable_full_daily=False,
    )


class OwnedDailySchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
        self._cooldown_patch = patch(
            "cardscanr_market_engine.owned_daily_scheduler.get_active_cooldown",
            return_value=None,
        )
        self._cooldown_patch.start()
        self.addCleanup(self._cooldown_patch.stop)
        self._avail_patch = patch(
            "cardscanr_market_engine.owned_daily_scheduler.browser_work_allowed",
            return_value=(True, "EBAY_AVAILABILITY_HEALTHY", None),
        )
        self._avail_patch.start()
        self.addCleanup(self._avail_patch.stop)

    def test_multi_user_same_printing_one_job(self) -> None:
        targets = [
            {
                "fingerprint": "fp-au",
                "market_country": "au",
                "currency": "aud",
                "owner_count": 3,
                "total_owned_quantity": 12,
                "market_price_key_id": "k1",
                "current_market_price": None,
                "owned_priority_band": "P0_NEVER_PRICED",
                "due_for_owned_daily": True,
            }
        ]
        client = FakeOwnedClient(targets)
        scheduler = OwnedPrintingRefreshScheduler(
            client=client, config=config(), now_func=lambda: self.now
        )
        report = scheduler.run_once()
        self.assertEqual(report["summary"]["jobsEnqueued"], 1)
        self.assertEqual(client.enqueued[0]["price_key_id"], "k1")

    def test_au_and_us_owners_create_two_market_keys(self) -> None:
        targets = [
            {
                "fingerprint": "fp-au",
                "market_country": "au",
                "currency": "aud",
                "owner_count": 2,
                "total_owned_quantity": 2,
                "market_price_key_id": "k-au",
                "current_market_price": None,
                "due_for_owned_daily": True,
                "owned_priority_band": "P0_NEVER_PRICED",
            },
            {
                "fingerprint": "fp-us",
                "market_country": "us",
                "currency": "usd",
                "owner_count": 1,
                "total_owned_quantity": 1,
                "market_price_key_id": "k-us",
                "current_market_price": None,
                "due_for_owned_daily": True,
                "owned_priority_band": "P0_NEVER_PRICED",
            },
        ]
        client = FakeOwnedClient(targets)
        scheduler = OwnedPrintingRefreshScheduler(
            client=client, config=config(), now_func=lambda: self.now
        )
        report = scheduler.run_once()
        self.assertEqual(report["summary"]["jobsEnqueued"], 2)
        keys = {row["price_key_id"] for row in client.enqueued}
        self.assertEqual(keys, {"k-au", "k-us"})

    def test_fresh_under_24h_skipped(self) -> None:
        targets = [
            {
                "fingerprint": "fp-fresh",
                "market_country": "au",
                "currency": "aud",
                "owner_count": 1,
                "total_owned_quantity": 1,
                "market_price_key_id": "k-fresh",
                "current_market_price": 12.5,
                "last_updated_at": iso(self.now - timedelta(hours=6)),
                "refresh_status": "completed",
                "due_for_owned_daily": False,
                "owned_priority_band": "FRESH_SKIP",
            }
        ]
        client = FakeOwnedClient(targets)
        scheduler = OwnedPrintingRefreshScheduler(
            client=client, config=config(), now_func=lambda: self.now
        )
        report = scheduler.run_once()
        self.assertEqual(report["summary"]["jobsEnqueued"], 0)
        self.assertEqual(report["metrics"]["OWNED_PRICE_SKIPPED_FRESH"], 1)

    def test_stale_gt_24h_enqueued(self) -> None:
        targets = [
            {
                "fingerprint": "fp-stale",
                "market_country": "au",
                "currency": "aud",
                "owner_count": 1,
                "total_owned_quantity": 1,
                "market_price_key_id": "k-stale",
                "current_market_price": 9.0,
                "last_updated_at": iso(self.now - timedelta(hours=25)),
                "refresh_status": "completed",
                "due_for_owned_daily": True,
                "owned_priority_band": "P1_STALE_GT_24H",
            }
        ]
        client = FakeOwnedClient(targets)
        scheduler = OwnedPrintingRefreshScheduler(
            client=client, config=config(), now_func=lambda: self.now
        )
        report = scheduler.run_once()
        self.assertEqual(report["summary"]["jobsEnqueued"], 1)
        self.assertIn("P1_STALE_GT_24H", client.enqueued[0]["reason"])

    def test_failed_does_not_count_as_fresh(self) -> None:
        targets = [
            {
                "fingerprint": "fp-fail",
                "market_country": "au",
                "currency": "aud",
                "owner_count": 1,
                "total_owned_quantity": 1,
                "market_price_key_id": "k-fail",
                "current_market_price": 8.0,
                "last_updated_at": iso(self.now - timedelta(hours=2)),
                "refresh_status": "failed",
                "next_refresh_due_at": iso(self.now - timedelta(minutes=1)),
                "due_for_owned_daily": True,
                "owned_priority_band": "P2_FAILED_RETRY",
            }
        ]
        client = FakeOwnedClient(targets)
        scheduler = OwnedPrintingRefreshScheduler(
            client=client, config=config(), now_func=lambda: self.now
        )
        decision = scheduler.evaluate_target(targets[0], now=self.now)
        self.assertTrue(decision.should_enqueue)
        self.assertEqual(decision.details["owned_priority_band"], "P2_FAILED_RETRY")

    def test_capacity_gap_fair_rolling_reports_deferred(self) -> None:
        targets = []
        for i in range(5):
            targets.append(
                {
                    "fingerprint": f"fp-{i}",
                    "market_country": "au",
                    "currency": "aud",
                    "owner_count": 1,
                    "total_owned_quantity": 1,
                    "market_price_key_id": f"k-{i}",
                    "current_market_price": None,
                    "due_for_owned_daily": True,
                    "owned_priority_band": "P0_NEVER_PRICED",
                }
            )
        client = FakeOwnedClient(targets)
        scheduler = OwnedPrintingRefreshScheduler(
            client=client, config=config(max_enqueues=2), now_func=lambda: self.now
        )
        report = scheduler.run_once()
        self.assertEqual(report["summary"]["jobsEnqueued"], 2)
        self.assertEqual(report["summary"]["capacityGap"], 3)
        self.assertTrue(report["summary"]["fairRollingApplied"])
        self.assertEqual(len(report["deferredDueToCapacity"]), 3)

    def test_zero_owners_excluded(self) -> None:
        targets = [
            {
                "fingerprint": "fp-zero",
                "market_country": "au",
                "currency": "aud",
                "owner_count": 0,
                "total_owned_quantity": 0,
                "market_price_key_id": "k-zero",
                "current_market_price": None,
                "due_for_owned_daily": True,
                "owned_priority_band": "P0_NEVER_PRICED",
            }
        ]
        client = FakeOwnedClient(targets)
        scheduler = OwnedPrintingRefreshScheduler(
            client=client, config=config(), now_func=lambda: self.now
        )
        report = scheduler.run_once()
        self.assertEqual(report["summary"]["jobsEnqueued"], 0)

    def test_metrics_have_no_user_ids(self) -> None:
        targets = [
            {
                "fingerprint": "fp-a",
                "market_country": "au",
                "currency": "aud",
                "owner_count": 2,
                "total_owned_quantity": 2,
                "market_price_key_id": "k-a",
                "current_market_price": None,
                "due_for_owned_daily": True,
                "owned_priority_band": "P0_NEVER_PRICED",
                "user_id": "should-not-appear",
            }
        ]
        client = FakeOwnedClient(targets)
        scheduler = OwnedPrintingRefreshScheduler(
            client=client, config=config(), now_func=lambda: self.now
        )
        report = scheduler.run_once()
        blob = str(report)
        self.assertNotIn("should-not-appear", blob)
        self.assertIn("OWNED_PRICE_ENQUEUED", report["metrics"])


if __name__ == "__main__":
    unittest.main()
