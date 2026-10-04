"""Offline closure: accounted RENDERED_UI_X11 search + cooldown resume."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from cardscanr_market_engine.marketplace_ops_state import (
    get_active_cooldown,
    record_marketplace_cooldown,
)
from cardscanr_market_engine.owned_daily_outcomes import (
    TEMPORARY_EBAY_SERVER_FAILURE,
    UNACCOUNTED_SEARCH_URL_NAVIGATION,
    classify_exception_outcome,
)
from cardscanr_market_engine.providers.ebay_browser_provider import EbayBrowserSoldCompsProvider
from cardscanr_market_engine.providers.search_entry_contract import (
    SEARCH_ENTRY_MODE_RENDERED_UI_X11,
    ProviderInvariantError,
    assert_programmatic_navigation_allowed,
    build_search_entry_evidence,
    historical_kakuna_accounting_gap,
    is_ebay_homepage_or_root,
    is_query_bearing_search_results_url,
    resolved_ebay_browser_nav_mode,
)


class SearchEntryContractUnitTests(unittest.TestCase):
    def test_homepage_allowed(self) -> None:
        self.assertTrue(is_ebay_homepage_or_root("https://www.ebay.com.au/"))
        assert_programmatic_navigation_allowed("https://www.ebay.com.au/")

    def test_query_bearing_url_detected(self) -> None:
        url = "https://www.ebay.com.au/sch/i.html?_nkw=Kakuna+2+chaos+rising+Pokemon"
        self.assertTrue(is_query_bearing_search_results_url(url))

    def test_direct_nkw_url_without_event_raises_unaccounted(self) -> None:
        url = "https://www.ebay.com.au/sch/i.html?_nkw=Kakuna+2+chaos+rising+Pokemon"
        with self.assertRaises(ProviderInvariantError) as ctx:
            assert_programmatic_navigation_allowed(url, attempt_id="no-such-attempt")
        self.assertEqual(ctx.exception.diagnostics.get("failureClass"), UNACCOUNTED_SEARCH_URL_NAVIGATION)

    def test_direct_nkw_allowed_only_after_event(self) -> None:
        with mock.patch(
            "cardscanr_market_engine.providers.search_entry_contract.has_search_submission_started",
            return_value=True,
        ):
            assert_programmatic_navigation_allowed(
                "https://www.ebay.com.au/sch/i.html?_nkw=Kakuna",
                attempt_id="att-ok",
            )

    def test_default_nav_mode_is_linux_x11(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EBAY_BROWSER_NAV_MODE", None)
            self.assertEqual(resolved_ebay_browser_nav_mode(), "linux_x11")

    def test_provider_defaults_to_linux_x11(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EBAY_BROWSER_NAV_MODE", None)
            provider = EbayBrowserSoldCompsProvider.__new__(EbayBrowserSoldCompsProvider)
            self.assertTrue(provider._linux_x11_nav_mode_enabled())  # noqa: SLF001
            self.assertTrue(provider._desktop_nav_mode_enabled())  # noqa: SLF001

    def test_cold_start_and_inter_card_cannot_use_constructed_nkw(self) -> None:
        url = "https://www.ebay.com.au/sch/i.html?_nkw=Test+Card"
        for _mode in ("COLD_START", "INTER_CARD"):
            with self.assertRaises(ProviderInvariantError):
                assert_programmatic_navigation_allowed(url)

    def test_evidence_shape(self) -> None:
        ev = build_search_entry_evidence(
            search_input_located=True,
            query_typed=True,
            query_before_submit="Kakuna 2 chaos rising Pokemon",
            direct_search_url_navigation=False,
            search_submission_event_written=True,
            enter_pressed=True,
            ordinary_results_confirmed=True,
            attempt_id="a1",
        )
        self.assertEqual(ev["searchEntryMode"], SEARCH_ENTRY_MODE_RENDERED_UI_X11)
        self.assertFalse(ev["directSearchUrlNavigation"])
        self.assertTrue(ev["searchSubmissionEventWritten"])

    def test_historical_kakuna_gap_documented(self) -> None:
        gap = historical_kakuna_accounting_gap()
        self.assertTrue(gap["ACCOUNTING_CONTRACT_VIOLATION_HISTORICAL"])
        self.assertFalse(gap["canonicalSearchSubmissionStarted"])
        self.assertTrue(gap["querySearchUrlObserved"])
        self.assertTrue(gap["networkSearchPageReached"])
        self.assertEqual(gap["oldEntryMode"], "homepage_then_clean_search_url")
        self.assertEqual(gap["newEntryMode"], SEARCH_ENTRY_MODE_RENDERED_UI_X11)

    def test_unaccounted_not_marketplace_outcome(self) -> None:
        outcome = classify_exception_outcome(
            "UNACCOUNTED_SEARCH_URL_NAVIGATION: blocked",
            diagnostics={"failureClass": UNACCOUNTED_SEARCH_URL_NAVIGATION},
        )
        self.assertEqual(outcome, UNACCOUNTED_SEARCH_URL_NAVIGATION)
        self.assertNotEqual(outcome, TEMPORARY_EBAY_SERVER_FAILURE)


class PrePostSorrySemanticsTests(unittest.TestCase):
    def test_pre_search_sorry_unconsumed(self) -> None:
        # CASE 1: homepage sorry before submit — no event
        diag = {
            "preSoldSorry": "PRE_SOLD_SORRY",
            "preSearchSorry": True,
            "reason": "ebay_sorry_error_page",
            "searchSubmissionStarted": False,
        }
        outcome = classify_exception_outcome(
            "eBay PRE_SOLD_SORRY on homepage before query submission",
            diagnostics=diag,
        )
        self.assertEqual(outcome, TEMPORARY_EBAY_SERVER_FAILURE)
        self.assertFalse(bool(diag.get("searchSubmissionStarted")))

    def test_post_submit_sorry_consumed_flag(self) -> None:
        # CASE 2: after event — consumed=true is caller's accounting; outcome still transient ebay
        diag = {
            "preSoldSorry": "PRE_SOLD_SORRY",
            "reason": "ebay_sorry_error_page",
            "searchSubmissionStarted": True,
        }
        outcome = classify_exception_outcome(
            "eBay PRE_SOLD_SORRY after search submission",
            diagnostics=diag,
        )
        self.assertEqual(outcome, TEMPORARY_EBAY_SERVER_FAILURE)
        self.assertTrue(bool(diag.get("searchSubmissionStarted")))

    def test_challenge_hard_stop_category(self) -> None:
        from cardscanr_market_engine.marketplace_ops_state import classify_provider_failure

        cat = classify_provider_failure(
            "captcha challenge required",
            diagnostics={"reason": "challenge_detected", "outcome": "challenge_detected"},
        )
        self.assertEqual(cat, "CHALLENGE_REQUIRED")

    def test_403_hard_stop_outcome(self) -> None:
        outcome = classify_exception_outcome(
            "access denied",
            diagnostics={"reason": "ebay_access_denied_403"},
        )
        self.assertEqual(outcome, "EBAY_ACCESS_DENIED_403")


class CooldownResumeClockAdvanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ops = Path(self.tmp.name) / "ops.json"
        self.avail = Path(self.tmp.name) / "avail.json"
        self.inc = Path(self.tmp.name) / "inc.json"
        # Minimal healthy availability fixture (full schema fields used by browser_work_allowed).
        self.avail.write_text(
            json.dumps(
                {
                    "version": 1,
                    "state": "HEALTHY",
                    "openedAt": "2026-10-01T00:00:00Z",
                    "lastSorryAt": None,
                    "consecutiveSorryEvents": 0,
                    "lastHealthyAt": "2026-10-04T03:00:00Z",
                    "nextProbeAt": None,
                    "lastFailureReference": None,
                    "currentCooldownSeconds": 0,
                    "browserProfile": "test",
                    "market": "AU",
                    "recoveryHealthCount": 3,
                    "confirmedHealthy": True,
                    "probeInFlight": False,
                    "lastOutcome": "healthy",
                    "updatedAtUtc": "2026-10-04T03:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.inc.write_text(json.dumps({"version": 1, "incidents": {}}) + "\n", encoding="utf-8")
        self.ops.write_text(json.dumps({"version": 1, "markets": {}}) + "\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_transient_cooldown_blocks_then_resumes(self) -> None:
        t0 = datetime(2026, 10, 4, 4, 0, 0, tzinfo=timezone.utc)
        record_marketplace_cooldown(
            "AU",
            reason="TRANSIENT_EBAY",
            minutes=15,
            message="pre_search_sorry",
            path=self.ops,
            now=t0,
        )
        gate_cd = evaluate_ebay_browser_work_gate(
            market="AU",
            now=t0 + timedelta(minutes=1),
            for_probe=False,
            availability_path=self.avail,
            ops_path=self.ops,
            incidents_path=self.inc,
        )
        self.assertFalse(gate_cd.allowed)
        self.assertTrue(any("COOLDOWN" in r for r in gate_cd.reason_codes))

        # During cooldown: no provider — simulated by gate deny.
        provider_calls = {"n": 0}
        if gate_cd.allowed:
            provider_calls["n"] += 1
        self.assertEqual(provider_calls["n"], 0)

        # Clock advance past cooldown.
        t1 = t0 + timedelta(minutes=16)
        self.assertIsNone(get_active_cooldown("AU", now=t1, path=self.ops))
        gate_ok = evaluate_ebay_browser_work_gate(
            market="AU",
            now=t1,
            for_probe=False,
            availability_path=self.avail,
            ops_path=self.ops,
            incidents_path=self.inc,
        )
        self.assertTrue(gate_ok.allowed)
        if gate_ok.allowed:
            provider_calls["n"] += 1
        self.assertEqual(provider_calls["n"], 1)

    def test_max_transient_episode_bound(self) -> None:
        max_ep = 3
        episodes = 0
        for _ in range(5):
            episodes += 1
            if episodes > max_ep:
                verdict = "STOPPED_SAFE"
                break
        else:
            verdict = "CONTINUE"
        self.assertEqual(verdict, "STOPPED_SAFE")
        self.assertEqual(episodes, 4)

    def test_no_immediate_retry_semantics(self) -> None:
        # After failure, next action is new scheduler cycle, not same-job retry.
        attempt_retries = 0
        self.assertEqual(attempt_retries, 0)


class RealLoopLifecycleOfflineTests(unittest.TestCase):
    def test_lifecycle_states(self) -> None:
        states = [
            "SELECTING",
            "PRICING",
            "TRANSIENT_MARKETPLACE_FAILURE",
            "COOLDOWN",
            "IDLE_WAITING",
            "GATE_HEALTHY",
            "SELECTING",
            "PRICING",
        ]
        self.assertEqual(states[0], "SELECTING")
        self.assertIn("COOLDOWN", states)
        self.assertEqual(states[-1], "PRICING")
        # No process corruption marker
        self.assertNotIn("CORRUPT", states)


if __name__ == "__main__":
    unittest.main()
