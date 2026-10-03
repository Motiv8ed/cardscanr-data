"""Focused tests for X11 nav runtime + reliability harness closure. NO eBay contact."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cardscanr_market_engine.live_navigation_attempt import (
    SEARCH_SUBMISSION_STARTED,
    count_consumed_live_navigations,
    emit_search_submission_started,
    has_search_submission_started,
    new_attempt_id,
)
from cardscanr_market_engine.owned_daily_outcomes import (
    CHALLENGE_REQUIRED,
    TEMPORARY_BROWSER_FAILURE,
)
from cardscanr_market_engine.owned_daily_scheduler import (
    OwnedDailySchedulerConfig,
    OwnedPrintingRefreshScheduler,
)
from cardscanr_market_engine.reliability_harness_classification import (
    classify_reliability_card_verdict,
    is_structured_challenge,
)
from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient
from cardscanr_market_engine.x11_navigation_runtime import LEGACY_TMP_VENV


ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    def __init__(self, cache_by_id: dict | None = None, ensure_map: dict | None = None) -> None:
        self.cache_by_id = cache_by_id or {}
        self.ensure_map = ensure_map or {}
        self.enriched = []

    def get_cache_row(self, *, price_key_id: str):
        return self.cache_by_id.get(price_key_id)

    def ensure_market_price_key_from_owned_target(self, target):
        fp = target.get("fingerprint")
        return self.ensure_map.get(fp) or target.get("market_price_key_id")

    def enrich_owned_target_from_cache(self, target):
        # Use real enrichment logic with this fake client methods.
        return SupabaseMarketEngineClient.enrich_owned_target_from_cache(self, target)  # type: ignore[arg-type]

    def get_price_key(self, *, price_key_id: str):
        class K:
            fingerprint = "pokemon|en|swsh12pt5gg|gg30|pikachu|holo|raw|au|aud"

        return K()


class AttemptAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"CARDSCANR_LIVE_NAV_ATTEMPTS_DIR": str(self.events)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_failure_before_event_not_consumed(self) -> None:
        attempt = new_attempt_id()
        self.assertFalse(has_search_submission_started(attempt))
        self.assertEqual(count_consumed_live_navigations([attempt]), 0)

    def test_submission_event_consumes_once(self) -> None:
        attempt = new_attempt_id()
        first = emit_search_submission_started(attempt_id=attempt, query="Pikachu GG30")
        second = emit_search_submission_started(attempt_id=attempt, query="Pikachu GG30")
        self.assertEqual(first.event, SEARCH_SUBMISSION_STARTED)
        self.assertEqual(first.timestamp, second.timestamp)
        self.assertEqual(count_consumed_live_navigations([attempt, attempt]), 1)
        self.assertTrue(has_search_submission_started(attempt))

    def test_crash_after_event_still_consumed(self) -> None:
        attempt = new_attempt_id()
        emit_search_submission_started(attempt_id=attempt, query="q")
        # Simulate crash: no final JSON, only event file remains.
        self.assertEqual(count_consumed_live_navigations([attempt]), 1)

    def test_subprocess_creation_alone_does_not_count(self) -> None:
        attempt = new_attempt_id()
        # Creating an attempt id / selecting a card is not consumption.
        self.assertEqual(count_consumed_live_navigations([attempt]), 0)

    def test_query_construction_alone_does_not_count(self) -> None:
        attempt = new_attempt_id()
        _ = "Pikachu GG30 Pokemon"
        self.assertFalse(has_search_submission_started(attempt))


class ChallengeClassificationTests(unittest.TestCase):
    def test_temporary_browser_failure_not_challenge(self) -> None:
        result = {"ownedDailyOutcome": TEMPORARY_BROWSER_FAILURE, "status": "failed", "error": "Desktop sold navigation failed"}
        diag = {"desktopNav": {"challenge": False, "sorry": False, "searchSuccess": False}}
        self.assertFalse(is_structured_challenge(result, diag))
        self.assertEqual(
            classify_reliability_card_verdict(result, diag=diag, search_submission_started=False),
            "FAIL_NAVIGATION",
        )

    def test_wsl_nav_no_json_not_challenge(self) -> None:
        result = {"ownedDailyOutcome": TEMPORARY_BROWSER_FAILURE, "error": "search_failed:wsl_nav_no_json"}
        self.assertFalse(is_structured_challenge(result, {}))

    def test_sold_word_in_error_not_challenge(self) -> None:
        result = {
            "ownedDailyOutcome": TEMPORARY_BROWSER_FAILURE,
            "error": "Desktop sold navigation failed: missing sold helper / Sold script unavailable / post sold capture failed",
        }
        diag = {"desktopNav": {"challenge": False, "sorry": False}}
        self.assertFalse(is_structured_challenge(result, diag))
        verdict = classify_reliability_card_verdict(result, diag=diag, search_submission_started=False)
        self.assertNotEqual(verdict, "STOP_CHALLENGE")

    def test_structured_challenge_true(self) -> None:
        result = {"ownedDailyOutcome": CHALLENGE_REQUIRED}
        self.assertTrue(is_structured_challenge(result, {}))
        self.assertEqual(
            classify_reliability_card_verdict(result, diag={}, search_submission_started=True),
            "STOP_CHALLENGE",
        )

    def test_structured_sorry_flag(self) -> None:
        result = {"status": "failed"}
        diag = {"desktopNav": {"challenge": False, "sorry": True, "url": "https://www.ebay.com.au/sorry"}}
        self.assertTrue(is_structured_challenge(result, diag))


class SchedulerFingerprintTests(unittest.TestCase):
    def test_fingerprint_candidates_include_collector_case_variants(self) -> None:
        fp = "pokemon|en|swsh12pt5gg|GG30|pikachu|holo|raw|au|aud"
        cands = SupabaseMarketEngineClient._fingerprint_lookup_candidates(fp)
        self.assertIn(fp, cands)
        self.assertIn("pokemon|en|swsh12pt5gg|gg30|pikachu|holo|raw|au|aud", cands)

    def test_enrichment_prevents_false_p0_when_cache_has_price(self) -> None:
        kid = "52e98068-30ba-466a-a9e6-0316b2af59e7"
        client = FakeClient(
            cache_by_id={
                kid: {
                    "current_market_price": 68.9,
                    "last_updated_at": "2026-09-27T06:57:34+00:00",
                    "refresh_status": "completed",
                    "display_price_source": "verified_local",
                    "provider": "ebay_browser",
                }
            },
            ensure_map={"pokemon|en|swsh12pt5gg|GG30|pikachu|holo|raw|au|aud": kid},
        )
        target = {
            "fingerprint": "pokemon|en|swsh12pt5gg|GG30|pikachu|holo|raw|au|aud",
            "market_country": "au",
            "currency": "aud",
            "owner_count": 1,
            "total_owned_quantity": 1,
            "market_price_key_id": None,
            "current_market_price": None,
            "owned_priority_band": "P0_NEVER_PRICED",
            "due_for_owned_daily": True,
        }
        enriched = client.enrich_owned_target_from_cache(target)
        self.assertEqual(enriched["market_price_key_id"], kid)
        self.assertEqual(float(enriched["current_market_price"]), 68.9)
        from datetime import datetime, timezone

        sched = OwnedPrintingRefreshScheduler(
            client=client,  # type: ignore[arg-type]
            config=OwnedDailySchedulerConfig(
                supabase_url="https://example.supabase.co",
                supabase_service_role_key="secret",
                max_enqueues_per_run=10,
                queue_low_watermark=0,
                queue_high_watermark=0,
                dry_run=True,
                sync_keys_before_run=False,
                allowed_markets=["AU"],
                rolling_window_hours=6,
                success_fresh_hours=24,
                latest_report_path=ROOT / "reports" / "owned_price_scheduler_latest.json",
                runs_report_path=ROOT / "reports" / "owned_price_scheduler_runs.jsonl",
                enable_full_daily=False,
            ),
            now_func=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
        with mock.patch(
            "cardscanr_market_engine.owned_daily_scheduler.get_active_cooldown",
            return_value=None,
        ), mock.patch(
            "cardscanr_market_engine.owned_daily_scheduler.browser_work_allowed",
            return_value=(True, "EBAY_AVAILABILITY_HEALTHY", mock.Mock(state="HEALTHY", next_probe_at=None, consecutive_sorry_events=0)),
        ):
            decision = sched.evaluate_target(enriched, now=datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertEqual(decision.details.get("owned_priority_band"), "P1_STALE_GT_24H")
        self.assertNotEqual(decision.details.get("owned_priority_band"), "P0_NEVER_PRICED")

    def test_true_unpriced_remains_p0(self) -> None:
        from datetime import datetime, timezone

        client = FakeClient(cache_by_id={}, ensure_map={})
        target = {
            "fingerprint": "fp-unpriced",
            "market_country": "au",
            "currency": "aud",
            "owner_count": 1,
            "total_owned_quantity": 1,
            "market_price_key_id": "k-new",
            "current_market_price": None,
        }
        sched = OwnedPrintingRefreshScheduler(
            client=client,  # type: ignore[arg-type]
            config=OwnedDailySchedulerConfig(
                supabase_url="https://example.supabase.co",
                supabase_service_role_key="secret",
                max_enqueues_per_run=10,
                queue_low_watermark=0,
                queue_high_watermark=0,
                dry_run=True,
                sync_keys_before_run=False,
                allowed_markets=["AU"],
                rolling_window_hours=6,
                success_fresh_hours=24,
                latest_report_path=ROOT / "reports" / "owned_price_scheduler_latest.json",
                runs_report_path=ROOT / "reports" / "owned_price_scheduler_runs.jsonl",
                enable_full_daily=False,
            ),
        )
        with mock.patch(
            "cardscanr_market_engine.owned_daily_scheduler.get_active_cooldown",
            return_value=None,
        ), mock.patch(
            "cardscanr_market_engine.owned_daily_scheduler.browser_work_allowed",
            return_value=(True, "OK", mock.Mock(state="HEALTHY", next_probe_at=None, consecutive_sorry_events=0)),
        ):
            decision = sched.evaluate_target(target, now=datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertEqual(decision.details.get("owned_priority_band"), "P0_NEVER_PRICED")


class LegacyTmpVenvTests(unittest.TestCase):
    def test_production_nav_has_no_tmp_venv_reference(self) -> None:
        nav = (ROOT / "cardscanr_market_engine" / "providers" / "linux_x11_ebay_nav.py").read_text(encoding="utf-8")
        self.assertNotIn(LEGACY_TMP_VENV, nav)
        self.assertNotIn("/tmp/cardscanr-xlib-venv", nav)


class LocalFailureSemanticsTests(unittest.TestCase):
    def test_pre_nav_failure_verdict_is_fail_navigation_not_challenge(self) -> None:
        result = {
            "status": "failed",
            "ownedDailyOutcome": TEMPORARY_BROWSER_FAILURE,
            "error": "Desktop sold navigation failed: search_failed:wsl_nav_no_json",
        }
        diag = {"desktopNav": {"challenge": False, "sorry": False, "searchSuccess": False, "url": ""}}
        self.assertFalse(is_structured_challenge(result, diag))
        self.assertEqual(
            classify_reliability_card_verdict(result, diag=diag, search_submission_started=False),
            "FAIL_NAVIGATION",
        )


class SelfCheckContractTests(unittest.TestCase):
    def test_self_check_helpers_declare_no_ebay_navigation(self) -> None:
        # Static contract: self-check functions exist and document no eBay nav.
        search = (ROOT / "tools" / "linux_x11_ebay_search.py").read_text(encoding="utf-8")
        sold = (ROOT / "tools" / "linux_x11_ebay_sold.py").read_text(encoding="utf-8")
        self.assertIn("def run_self_check", search)
        self.assertIn("def run_self_check", sold)
        self.assertIn("--self-check", search)
        self.assertIn("--self-check", sold)
        self.assertIn('"ebayNavigationPerformed": False', search)


if __name__ == "__main__":
    unittest.main()
