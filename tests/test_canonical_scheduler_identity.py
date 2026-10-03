"""Canonical scheduler identity / source-aware policy tests. NO eBay."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

from cardscanr_market_engine.owned_daily_scheduler import (
    OwnedDailySchedulerConfig,
    OwnedPrintingRefreshScheduler,
)
from cardscanr_market_engine.owned_daily_source_policy import (
    classify_owned_daily_band,
    classify_owned_price_source,
)
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient


NOW = datetime(2026, 10, 2, 5, 55, tzinfo=timezone.utc)


def _config() -> OwnedDailySchedulerConfig:
    return OwnedDailySchedulerConfig(
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="secret",
        max_enqueues_per_run=50,
        queue_low_watermark=0,
        queue_high_watermark=0,
        dry_run=True,
        sync_keys_before_run=False,
        allowed_markets=["AU"],
        rolling_window_hours=24,
        success_fresh_hours=24,
        latest_report_path=ROOT / "reports" / "owned_price_scheduler_latest.json",
        runs_report_path=ROOT / "reports" / "owned_price_scheduler_runs.jsonl",
        enable_full_daily=False,
    )


class SourcePolicyTests(unittest.TestCase):
    def test_gg30_case_candidates_join(self) -> None:
        fp = "pokemon|en|swsh12pt5gg|GG30|pikachu|holo|raw|au|aud"
        cands = SupabaseMarketEngineClient._fingerprint_lookup_candidates(fp)
        self.assertIn("pokemon|en|swsh12pt5gg|gg30|pikachu|holo|raw|au|aud", cands)
        self.assertIn(fp, cands)

    def test_case_insensitive_not_collector_insensitive(self) -> None:
        gg30 = "pokemon|en|swsh12pt5gg|GG30|pikachu|holo|raw|au|aud"
        gg25 = "pokemon|en|swsh12pt5gg|GG25|bibarel|raw|raw|au|aud"
        c30 = set(SupabaseMarketEngineClient._fingerprint_lookup_candidates(gg30))
        c25 = set(SupabaseMarketEngineClient._fingerprint_lookup_candidates(gg25))
        self.assertTrue(c30.isdisjoint(c25))

    def test_reference_only_needs_verified_not_fresh_skip(self) -> None:
        target = {
            "current_market_price": 16.78,
            "display_price_source": "reference",
            "provider": "tcgdex_cardmarket",
            "last_updated_at": (NOW - timedelta(hours=12)).isoformat(),
            "stale_after": (NOW - timedelta(hours=6)).isoformat(),
            "refresh_status": "completed",
        }
        band, due, reason, view, _ = classify_owned_daily_band(target, now=NOW)
        self.assertEqual(band, "P0_NEEDS_VERIFIED_LOCAL")
        self.assertTrue(due)
        self.assertEqual(reason, "p0_needs_verified_local")
        self.assertTrue(view.has_reference_only_price)
        self.assertFalse(view.has_verified_local_price)
        self.assertIsNone(view.ebay_success_freshness_at)

    def test_verified_local_fresh_skips_despite_past_stale_after(self) -> None:
        target = {
            "current_market_price": 10.0,
            "display_price_source": "verified_local",
            "provider": "ebay_browser",
            "last_updated_at": (NOW - timedelta(hours=6)).isoformat(),
            "stale_after": (NOW - timedelta(hours=1)).isoformat(),
            "refresh_status": "completed",
        }
        band, due, reason, view, extras = classify_owned_daily_band(target, now=NOW)
        self.assertEqual(band, "FRESH_SKIP")
        self.assertFalse(due)
        self.assertTrue(extras.get("stale_after_ignored_for_band"))
        self.assertEqual(view.authoritative_timestamp_field, "last_updated_at")

    def test_verified_local_stale(self) -> None:
        target = {
            "current_market_price": 10.0,
            "display_price_source": "verified_local",
            "provider": "ebay_browser",
            "last_updated_at": (NOW - timedelta(hours=30)).isoformat(),
            "refresh_status": "completed",
        }
        band, due, *_ = classify_owned_daily_band(target, now=NOW)
        self.assertEqual(band, "P1_STALE_GT_24H")
        self.assertTrue(due)

    def test_truly_unpriced_p0(self) -> None:
        band, due, reason, view, _ = classify_owned_daily_band(
            {"current_market_price": None}, now=NOW
        )
        self.assertEqual(band, "P0_NEVER_PRICED")
        self.assertTrue(due)
        self.assertEqual(view.source_class, "none")

    def test_scheduler_final_agrees_with_policy(self) -> None:
        client = object()
        sched = OwnedPrintingRefreshScheduler(client=client, config=_config(), now_func=lambda: NOW)
        target = {
            "fingerprint": "fp-giratina",
            "market_country": "au",
            "currency": "aud",
            "owner_count": 1,
            "total_owned_quantity": 1,
            "market_price_key_id": "83382041-41a4-4fda-a359-8d0db5265934",
            "current_market_price": 16.78,
            "display_price_source": "reference",
            "provider": "tcgdex_cardmarket",
            "last_updated_at": (NOW - timedelta(hours=12)).isoformat(),
            "stale_after": (NOW - timedelta(hours=6)).isoformat(),
            "refresh_status": "completed",
            "owned_priority_band": "P1_STALE_GT_24H",
            "due_for_owned_daily": False,
        }
        with patch(
            "cardscanr_market_engine.owned_daily_scheduler.get_active_cooldown",
            return_value=None,
        ), patch(
            "cardscanr_market_engine.owned_daily_scheduler.browser_work_allowed",
            return_value=(True, "OK", type("S", (), {"state": "HEALTHY", "next_probe_at": None, "consecutive_sorry_events": 0})()),
        ):
            decision = sched.evaluate_target(target, now=NOW)
        self.assertTrue(decision.should_enqueue)
        self.assertEqual(decision.details.get("owned_priority_band"), "P0_NEEDS_VERIFIED_LOCAL")
        self.assertIn("needs_verified_local", decision.reason)

    def test_source_classifier_distinctions(self) -> None:
        self.assertEqual(
            classify_owned_price_source(
                current_market_price=1, display_price_source="verified_local", provider="ebay_browser"
            ),
            "verified_local",
        )
        self.assertEqual(
            classify_owned_price_source(
                current_market_price=1, display_price_source="reference", provider="tcgdex_cardmarket"
            ),
            "reference_only",
        )


class MigrationContractTests(unittest.TestCase):
    def test_source_aware_migration_present(self) -> None:
        path = ROOT / "supabase" / "migrations" / "20261002140000_owned_daily_source_aware_scheduler_align.sql"
        text = path.read_text(encoding="utf-8")
        self.assertIn("P0_NEEDS_VERIFIED_LOCAL", text)
        self.assertIn("lower(k.fingerprint) = lower(a.fingerprint)", text)
        # Must not use stale_after alone for P1 band.
        self.assertIn("Authoritative eBay-success clock: last_updated_at ONLY", text)

    def test_case_join_migration_present(self) -> None:
        path = ROOT / "supabase" / "migrations" / "20261002120000_owned_fingerprint_case_join_fix.sql"
        self.assertTrue(path.is_file())
        self.assertIn("lower(k.fingerprint) = lower(a.fingerprint)", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
