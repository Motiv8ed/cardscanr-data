"""Focused tests for canary cooldown recovery and operation modes."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cardscanr_market_engine import canary_control_plane as ccp
from cardscanr_market_engine.canary_control_plane import (
    MAX_PRESUBMIT_TRANSIENT_EPISODES_24H,
    canary_may_continue,
    classify_canary_failure,
    operation_mode_allows_marketplace_persistence,
    parse_operation_mode,
    pre_submit_transient_count,
    record_canary_episode,
    set_operation_mode,
)
from cardscanr_market_engine.ebay_availability import (
    peek_availability,
    record_sorry,
)
from cardscanr_market_engine.market_dispatcher import global_concurrency, pick_fair_market
from cardscanr_market_engine.providers.linux_x11_gui_fsm import search_page_ready
from cardscanr_market_engine.region_pricing_registry import is_region_dispatchable
from cardscanr_market_engine.browser_lifecycle_policy import (
    RUNTIME_COLD_START,
    RUNTIME_INTER_CARD,
    PriorCardContext,
    required_runtime_mode,
)


NOW = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)


class CanaryControlPlaneTests(unittest.TestCase):
    def test_operation_modes(self) -> None:
        self.assertEqual(parse_operation_mode("canary"), "CANARY")
        self.assertEqual(parse_operation_mode("PROBE"), "PROBE")
        self.assertTrue(operation_mode_allows_marketplace_persistence("CANARY"))
        self.assertTrue(operation_mode_allows_marketplace_persistence("PROBE"))
        self.assertEqual(set_operation_mode("CANARY"), "CANARY")

    def test_error_page_title_fails_search_ready_as_marketplace(self) -> None:
        ready, reason = search_page_ready(
            url="https://www.ebay.com/",
            title="Error Page | eBay",
            search_button_found=False,
        )
        self.assertFalse(ready)
        self.assertEqual(reason, "ebay_error_page")

    def test_presubmit_transient_persists_while_enabled_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            record_canary_episode(
                "US",
                kind="PRE_SUBMIT_TRANSIENT",
                outcome="TEMPORARY_EBAY_SERVER_FAILURE",
                now=NOW,
                path=path,
            )
            self.assertEqual(pre_submit_transient_count("US", now=NOW, path=path), 1)
            self.assertEqual(pre_submit_transient_count("AU", now=NOW, path=path), 0)

    def test_max_three_presubmit_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            for i in range(MAX_PRESUBMIT_TRANSIENT_EPISODES_24H):
                record_canary_episode(
                    "US",
                    kind="PRE_SUBMIT_TRANSIENT",
                    outcome="TEMPORARY_EBAY_SERVER_FAILURE",
                    now=NOW + timedelta(minutes=i),
                    path=path,
                )
            decision = canary_may_continue("US", now=NOW + timedelta(hours=1), path=path)
            self.assertFalse(decision["ok"])
            self.assertEqual(decision["reason"], "MAX_PRESUBMIT_TRANSIENT_EPISODES_24H")

    def test_local_runtime_failure_does_not_open_cooldown(self) -> None:
        kind = classify_canary_failure(
            outcome="TEMPORARY_BROWSER_FAILURE",
            error_message="linux_chrome_cdp_failed: DISPLAY :99 not ready",
            search_submission_started=False,
        )
        self.assertEqual(kind["kind"], "LOCAL_RUNTIME_FAILURE")
        self.assertFalse(kind["opensMarketplaceCooldown"])
        self.assertFalse(kind["consumed"])

    def test_presubmit_sorry_unconsumed(self) -> None:
        kind = classify_canary_failure(
            outcome="TEMPORARY_EBAY_SERVER_FAILURE",
            error_message="TEMPORARY_EBAY_SERVER_FAILURE: eBay SORRY/error page",
            search_submission_started=False,
        )
        self.assertEqual(kind["kind"], "PRE_SUBMIT_TRANSIENT")
        self.assertTrue(kind["opensMarketplaceCooldown"])
        self.assertFalse(kind["consumed"])

    def test_postsubmit_transient_consumed(self) -> None:
        kind = classify_canary_failure(
            outcome="TEMPORARY_EBAY_SERVER_FAILURE",
            error_message="TEMPORARY_EBAY_SERVER_FAILURE after sold",
            search_submission_started=True,
        )
        self.assertEqual(kind["kind"], "POST_SUBMIT_TRANSIENT")
        self.assertTrue(kind["consumed"])

    def test_market_scoped_sorry_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ebay_availability_state.json"
            with patch.object(ccp, "CANARY_LEDGER_PATH", Path(tmp) / "ledger.json"):
                record_sorry(
                    now=NOW,
                    path=path,
                    reference="US homepage Error Page",
                    market="US",
                    from_probe=False,
                )
                us = peek_availability(now=NOW, path=path, market="US")
                au = peek_availability(now=NOW, path=path, market="AU")
                gb = peek_availability(now=NOW, path=path, market="GB")
                self.assertEqual(us.state, "COOLDOWN")
                self.assertEqual(us.market, "US")
                self.assertEqual(au.state, "HEALTHY")
                self.assertEqual(gb.state, "HEALTHY")

    def test_jp_eu_blocked_and_global_concurrency_one(self) -> None:
        self.assertFalse(is_region_dispatchable("JP"))
        self.assertFalse(is_region_dispatchable("EU"))
        self.assertEqual(global_concurrency(), 1)
        self.assertIsNone(pick_fair_market({"JP": 9, "EU": 9}, enabled_markets=("JP", "EU")))

    def test_market_switch_requires_cold_start(self) -> None:
        prior = PriorCardContext(
            job_id="j",
            attempt_id="a",
            price_key_id="pk",
            fingerprint="fp",
            target_id="t",
            final_url="https://www.ebay.com/sch/i.html?LH_Sold=1",
            query="q",
            x11_sold_state_verified=True,
            capture_correlated=True,
            card_verdict="PASS_PRICE_UPDATED",
            market="US",
            currency="USD",
        )
        self.assertEqual(required_runtime_mode(next_market="GB", prior=prior), RUNTIME_COLD_START)
        self.assertEqual(required_runtime_mode(next_market="US", prior=prior), RUNTIME_INTER_CARD)


if __name__ == "__main__":
    unittest.main()
