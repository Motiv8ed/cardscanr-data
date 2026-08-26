"""Tests for pricing throughput scale-up helpers."""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from cardscanr_market_engine.price_movement_guard import evaluate_price_movement
from cardscanr_market_engine.queue_capacity import (
    QueueWatermarks,
    enqueue_budget,
    hours_for_keys,
    projected_cards_per_hour,
)
from cardscanr_market_engine.scheduler import MarketPriceRefreshScheduler
from tests.test_market_engine_scheduler import FakeSchedulerClient, fixed_config, iso


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class QueueWatermarkTests(unittest.TestCase):
    def test_disabled_watermarks_use_max_enqueues(self) -> None:
        budget = enqueue_budget(
            queue_depth=100,
            watermarks=QueueWatermarks(0, 0),
            max_enqueues_per_run=10,
        )
        self.assertEqual(budget, 10)

    def test_high_watermark_blocks_enqueue(self) -> None:
        budget = enqueue_budget(
            queue_depth=12,
            watermarks=QueueWatermarks(4, 12),
            max_enqueues_per_run=10,
        )
        self.assertEqual(budget, 0)

    def test_low_watermark_fills_toward_high(self) -> None:
        budget = enqueue_budget(
            queue_depth=1,
            watermarks=QueueWatermarks(4, 12),
            max_enqueues_per_run=10,
        )
        self.assertEqual(budget, 10)  # room=11, capped by max 10

    def test_between_watermarks_gentle_topup(self) -> None:
        budget = enqueue_budget(
            queue_depth=6,
            watermarks=QueueWatermarks(4, 12),
            max_enqueues_per_run=10,
        )
        self.assertEqual(budget, 2)  # min(10, 12-6, low//2=2)

    def test_projection_helpers(self) -> None:
        cph = projected_cards_per_hour(median_job_seconds=60, workers=1)
        self.assertEqual(cph, 60.0)
        self.assertAlmostEqual(hours_for_keys(50_000, 2500) or 0, 20.0)


class PriceMovementGuardTests(unittest.TestCase):
    def test_tiny_dollar_high_percent_accepted(self) -> None:
        d = evaluate_price_movement(old_price=0.2, new_price=0.4, included_count=1, confidence="low")
        self.assertEqual(d.action, "accept")

    def test_normal_move_accepted(self) -> None:
        d = evaluate_price_movement(old_price=10.0, new_price=11.0, included_count=2, confidence="medium")
        self.assertEqual(d.action, "accept")

    def test_large_move_weak_evidence_pending(self) -> None:
        d = evaluate_price_movement(old_price=65.14, new_price=5.84, included_count=1, confidence="low")
        self.assertEqual(d.action, "pending_verification")

    def test_extreme_move_medium_evidence_still_pending(self) -> None:
        # Production Pikachu ex style: 13 medium comps still quarantine extreme moves.
        d = evaluate_price_movement(old_price=65.14, new_price=5.84, included_count=13, confidence="medium")
        self.assertEqual(d.action, "pending_verification")
        self.assertIn("extreme", d.reason)

    def test_extreme_move_high_confidence_can_commit(self) -> None:
        d = evaluate_price_movement(old_price=65.14, new_price=5.84, included_count=13, confidence="high")
        self.assertEqual(d.action, "accept")
        self.assertIn("verified", d.reason)

    def test_moderate_large_move_strong_evidence_accepted(self) -> None:
        d = evaluate_price_movement(old_price=40.0, new_price=18.0, included_count=5, confidence="medium")
        self.assertEqual(d.action, "accept")

    def test_large_move_zero_included_rejected(self) -> None:
        d = evaluate_price_movement(old_price=65.14, new_price=5.84, included_count=0, confidence="low")
        self.assertEqual(d.action, "reject_weak")


class SchedulerWatermarkIntegrationTests(unittest.TestCase):
    def test_scheduler_respects_high_watermark(self) -> None:
        client = FakeSchedulerClient(
            stale_rows=[
                {
                    "id": f"k{i}",
                    "fingerprint": f"fp{i}",
                    "market_country": "AU",
                    "currency": "AUD",
                    "current_market_price": 1.0,
                    "next_refresh_due_at": iso(NOW - timedelta(days=1)),
                    "stale_after": iso(NOW - timedelta(days=1)),
                    "refresh_status": "completed",
                    "last_updated_at": iso(NOW - timedelta(days=2)),
                    "popularity_score": 1,
                    "inventory_count": 1,
                    "last_seen_at": iso(NOW - timedelta(days=1)),
                }
                for i in range(20)
            ]
        )
        # Pretend queue already at high watermark.
        client.active_jobs = {f"busy{i}": {"id": f"j{i}", "status": "queued"} for i in range(12)}
        config = replace(
            fixed_config(max_enqueues=10),
            queue_low_watermark=4,
            queue_high_watermark=12,
        )
        scheduler = MarketPriceRefreshScheduler(client=client, config=config, now_func=lambda: NOW)
        report = scheduler.run_once()
        self.assertEqual(report["summary"]["jobsEnqueued"], 0)
        self.assertEqual(report["limits"]["effectiveEnqueueBudget"], 0)


class ClaimLockSafetyTests(unittest.TestCase):
    def test_claim_rpc_uses_skip_locked(self) -> None:
        from pathlib import Path

        sql = (
            Path(__file__).resolve().parents[1]
            / "supabase"
            / "migrations"
            / "20260606000000_market_price_refresh_state_cache_rows.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("for update skip locked", sql.lower())
        self.assertIn("claim_market_price_refresh_jobs", sql)


class ScaleSchedulingSimulationTests(unittest.TestCase):
    def test_batch_schedule_simulation_scales(self) -> None:
        for n in (1_000, 10_000, 50_000):
            # Simulate eligibility sort + watermark fill without network.
            depth = 0
            enqueued = 0
            low, high, max_batch = 4, 12, 10
            # Process until all keys considered once in batches of max_keys=100.
            remaining = n
            passes = 0
            while remaining > 0 and passes < (n // 5 + 10):
                passes += 1
                scanned = min(100, remaining)
                remaining -= scanned
                budget = enqueue_budget(
                    queue_depth=depth,
                    watermarks=QueueWatermarks(low, high),
                    max_enqueues_per_run=max_batch,
                )
                take = min(budget, scanned)
                enqueued += take
                depth += take
                # Simulate worker draining half the queue each pass.
                depth = max(0, depth - max(1, depth // 2))
            self.assertGreater(enqueued, 0)
            self.assertLessEqual(depth, high)
            # Simulation should complete without pathological pass counts.
            self.assertLess(passes, n)


if __name__ == "__main__":
    unittest.main()
