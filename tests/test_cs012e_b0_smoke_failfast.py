"""CS-012e-B0 unit tests: smoke skip + empty-DOM fail-fast gates."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.bulk.set_id_aliases import is_smoke_pricing_key
from cardscanr_market_engine.providers.ebay_browser_provider import (
    should_fail_fast_empty_result_dom,
    smoke_failfast_settle_ms,
)
from cardscanr_market_engine.scheduler import MarketPriceRefreshScheduler, MarketSchedulerConfig


def _fixed_config() -> MarketSchedulerConfig:
    return MarketSchedulerConfig(
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="secret",
        max_keys_per_run=50,
        max_enqueues_per_run=50,
        queue_low_watermark=0,
        queue_high_watermark=0,
        include_missing_cache=True,
        include_stale_cache=True,
        min_popularity_score=0,
        min_inventory_count=0,
        dry_run=True,
        poll_seconds=300,
        allowed_markets=["AU", "US", "CA", "GB"],
        latest_report_path=ROOT / "reports" / "market_price_scheduler_latest.json",
        runs_report_path=ROOT / "reports" / "market_price_scheduler_runs.jsonl",
    )


class FakeClient:
    pass


class SmokePricingKeyTests(unittest.TestCase):
    def test_smoke_fingerprint_and_charizard(self) -> None:
        self.assertTrue(
            is_smoke_pricing_key(
                fingerprint="smoke-test",
                set_code="smoke-test",
                set_name="Smoke Test Set",
                card_name="Smoke Test Charizard ex",
                collector_number="001/999",
            )
        )

    def test_real_card_not_smoke(self) -> None:
        self.assertFalse(
            is_smoke_pricing_key(
                fingerprint="sv3-223-en-nm",
                set_code="sv3",
                set_name="Obsidian Flames",
                card_name="Charizard ex",
                collector_number="223/197",
            )
        )

    def test_collector_alone_not_smoke(self) -> None:
        self.assertFalse(is_smoke_pricing_key(collector_number="001/999"))


class FailFastGateTests(unittest.TestCase):
    def test_smoke_zero_selectors_unknown_failfast(self) -> None:
        self.assertTrue(
            should_fail_fast_empty_result_dom(
                is_smoke=True,
                selector_counts={"li.s-item": 0, ".srp-results": 0},
                page_state={"outcome": "parsing_failure", "reason": "unknown_page_state"},
            )
        )

    def test_non_smoke_zero_selectors_no_failfast(self) -> None:
        self.assertFalse(
            should_fail_fast_empty_result_dom(
                is_smoke=False,
                selector_counts={"li.s-item": 0},
                page_state={"outcome": "parsing_failure", "reason": "unknown_page_state"},
            )
        )

    def test_smoke_with_selectors_no_failfast(self) -> None:
        self.assertFalse(
            should_fail_fast_empty_result_dom(
                is_smoke=True,
                selector_counts={"li.s-item": 3},
                page_state={"outcome": "parsing_failure", "reason": "unknown_page_state"},
            )
        )

    def test_settle_never_exceeds_timeout(self) -> None:
        self.assertEqual(smoke_failfast_settle_ms(timeout_ms=2000), 2000)
        self.assertLessEqual(smoke_failfast_settle_ms(timeout_ms=45000), 45000)


class SchedulerSmokeSkipTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cooldown_patch = patch(
            "cardscanr_market_engine.scheduler.get_active_cooldown",
            return_value=None,
        )
        self._cooldown_patch.start()
        self.addCleanup(self._cooldown_patch.stop)
        self.scheduler = MarketPriceRefreshScheduler(client=FakeClient(), config=_fixed_config())
        self.now = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)

    def test_smoke_fingerprint_skipped(self) -> None:
        decision = self.scheduler.evaluate_candidate(
            {
                "id": "pk-smoke",
                "fingerprint": "smoke-test",
                "market_country": "US",
                "has_cache": True,
                "current_market_price": None,
                "popularity_score": 0,
                "inventory_count": 0,
            },
            now=self.now,
        )
        self.assertFalse(decision.should_enqueue)
        self.assertEqual(decision.reason, "skipped_synthetic_smoke")

    def test_real_missing_price_still_enqueues(self) -> None:
        decision = self.scheduler.evaluate_candidate(
            {
                "id": "pk-real",
                "fingerprint": "sv3-223-en",
                "market_country": "AU",
                "has_cache": True,
                "current_market_price": None,
                "popularity_score": 20,
                "inventory_count": 2,
                "last_seen_at": "2026-09-01T00:00:00+00:00",
            },
            now=self.now,
        )
        self.assertTrue(decision.should_enqueue)
        self.assertIn(decision.reason, {"missing_price", "missing_price_recent"})


if __name__ == "__main__":
    unittest.main()
