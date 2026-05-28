from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.refresh_policy import (  # noqa: E402
    RefreshCooldownConfig,
    calculate_refresh_policy,
)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class RefreshPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 5, 25, 12, tzinfo=timezone.utc)
        self.config = RefreshCooldownConfig(
            default_cooldown_hours=6,
            high_value_cooldown_hours=4,
            popular_cooldown_hours=4,
            hot_card_cooldown_hours=2,
            low_value_cooldown_hours=12,
        )

    def decide(
        self,
        *,
        cache: dict | None,
        key: dict | None = None,
        active_job: dict | None = None,
        force: bool = False,
    ):
        return calculate_refresh_policy(
            cache_row=cache,
            price_key_row=key or {"popularity_score": 0, "inventory_count": 0},
            active_job=active_job,
            now=self.now,
            request_reason="user_refresh",
            force=force,
            config=self.config,
        )

    def test_no_cache_allows_refresh(self) -> None:
        decision = self.decide(cache=None)
        self.assertTrue(decision.can_refresh)
        self.assertFalse(decision.cache_is_fresh)
        self.assertEqual(decision.reason, "no_cache")

    def test_default_cooldown_blocks_recent_cache(self) -> None:
        decision = self.decide(cache={"last_updated_at": iso(self.now - timedelta(hours=2))})
        self.assertFalse(decision.can_refresh)
        self.assertTrue(decision.is_in_cooldown)
        self.assertEqual(decision.cooldown_hours, 6)
        self.assertEqual(decision.reason, "default")

    def test_default_cooldown_allows_old_cache(self) -> None:
        decision = self.decide(cache={"last_updated_at": iso(self.now - timedelta(hours=8))})
        self.assertTrue(decision.can_refresh)
        self.assertFalse(decision.is_in_cooldown)
        self.assertEqual(decision.reason, "default_expired")

    def test_high_value_card_uses_four_hour_cooldown(self) -> None:
        decision = self.decide(
            cache={"last_updated_at": iso(self.now - timedelta(hours=3)), "current_market_price": 125}
        )
        self.assertFalse(decision.can_refresh)
        self.assertEqual(decision.cooldown_hours, 4)
        self.assertEqual(decision.reason, "high_value")

    def test_popular_card_uses_four_hour_cooldown(self) -> None:
        decision = self.decide(
            cache={"last_updated_at": iso(self.now - timedelta(hours=3)), "recommended_price": 25},
            key={"popularity_score": 10, "inventory_count": 0},
        )
        self.assertFalse(decision.can_refresh)
        self.assertEqual(decision.cooldown_hours, 4)
        self.assertEqual(decision.reason, "popular")

    def test_high_value_and_popular_card_uses_two_hour_cooldown(self) -> None:
        decision = self.decide(
            cache={"last_updated_at": iso(self.now - timedelta(hours=3)), "recommended_price": 125},
            key={"popularity_score": 10, "inventory_count": 0},
        )
        self.assertTrue(decision.can_refresh)
        self.assertEqual(decision.cooldown_hours, 2)
        self.assertEqual(decision.reason, "hot_card_expired")

    def test_low_value_common_card_uses_twelve_hour_cooldown(self) -> None:
        decision = self.decide(
            cache={
                "last_updated_at": iso(self.now - timedelta(hours=8)),
                "current_market_price": 5,
                "recommended_price": 6,
            },
            key={"popularity_score": 2, "inventory_count": 2},
        )
        self.assertFalse(decision.can_refresh)
        self.assertEqual(decision.cooldown_hours, 12)
        self.assertEqual(decision.reason, "low_value_common")

    def test_active_job_blocks_duplicate_enqueue(self) -> None:
        active_job = {"id": "job-1", "status": "running"}
        decision = self.decide(cache=None, active_job=active_job)
        self.assertFalse(decision.can_refresh)
        self.assertEqual(decision.reason, "active_job_exists")
        self.assertEqual(decision.active_refresh_job, active_job)

    def test_different_market_fingerprints_are_independent_inputs(self) -> None:
        au = self.decide(cache={"last_updated_at": iso(self.now - timedelta(hours=2))})
        us = self.decide(cache={"last_updated_at": iso(self.now - timedelta(hours=8))})
        self.assertFalse(au.can_refresh)
        self.assertTrue(us.can_refresh)


if __name__ == "__main__":
    unittest.main()
