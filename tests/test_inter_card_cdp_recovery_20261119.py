"""INTER_CARD recovery after Chrome/CDP target set changes."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.browser_lifecycle_policy import RUNTIME_COLD_START, RUNTIME_INTER_CARD, PriorCardContext
from cardscanr_market_engine.inter_card_cdp_recovery import (
    CDP_RECOVERY_STATE_PATH,
    observe_inter_card_no_request_outcome,
    reconcile_nav_context_for_cdp_change,
)
from cardscanr_market_engine.navigation_runtime_context import (
    INTER_CARD_EXPECTED_TARGET_REJECTED,
    NavigationRuntimeContext,
)


class InterCardCdpRecoveryTests(unittest.TestCase):
    def test_cdp_fingerprint_change_clears_expected_target(self) -> None:
        prior = PriorCardContext(
            job_id="j1",
            target_id="CDP-VOLTORB",
            query="Voltorb",
            x11_sold_state_verified=True,
            card_verdict="PASS_PRICE_UPDATED",
        )
        ctx = NavigationRuntimeContext(
            runtime_mode=RUNTIME_INTER_CARD,
            expected_prior=prior,
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "recovery.json"
            with (
                mock.patch("cardscanr_market_engine.inter_card_cdp_recovery.CDP_RECOVERY_STATE_PATH", state_path),
                mock.patch(
                    "cardscanr_market_engine.local_browser_runtime.probe_local_browser_runtime",
                ) as probe,
                mock.patch(
                    "cardscanr_market_engine.inter_card_cdp_recovery._fingerprint_cdp_targets",
                    return_value="fp-new",
                ),
            ):
                state_path.write_text(
                    json.dumps({"cdpTopLevelFingerprint": "r0:CDP-VOLTORB"}),
                    encoding="utf-8",
                )
                probe.return_value = mock.Mock(raw_targets=[{"id": "X", "type": "page"}])
                with mock.patch(
                    "cardscanr_market_engine.inter_card_cdp_recovery._fingerprint_cdp_targets",
                    return_value="r1:X",
                ):
                    out = reconcile_nav_context_for_cdp_change(ctx)
        self.assertTrue(out.prior_handoff_stale)
        assert out.expected_prior is not None
        self.assertIsNone(out.expected_prior.target_id)

    def test_two_no_request_rejections_force_cold_start(self) -> None:
        prior = PriorCardContext(
            job_id="j1",
            target_id="CDP-VOLTORB",
            query="Voltorb",
            x11_sold_state_verified=True,
            card_verdict="PASS_PRICE_UPDATED",
        )
        ctx = NavigationRuntimeContext(
            runtime_mode=RUNTIME_INTER_CARD,
            expected_prior=prior,
        )
        with tempfile.TemporaryDirectory() as tmp:
            nav_path = Path(tmp) / "nav.json"
            state_path = Path(tmp) / "recovery.json"
            with (
                mock.patch("cardscanr_market_engine.inter_card_cdp_recovery.CDP_RECOVERY_STATE_PATH", state_path),
                mock.patch(
                    "cardscanr_market_engine.inter_card_cdp_recovery.bind_persisted_nav_context_path",
                    return_value=nav_path,
                ),
            ):
                observe_inter_card_no_request_outcome(
                    ctx,
                    runtime_error=INTER_CARD_EXPECTED_TARGET_REJECTED,
                    search_submission_started=False,
                    raw_error="Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED",
                )
                out, _latch = observe_inter_card_no_request_outcome(
                    ctx,
                    runtime_error=INTER_CARD_EXPECTED_TARGET_REJECTED,
                    search_submission_started=False,
                    raw_error="Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED",
                )
        self.assertEqual(out.runtime_mode, RUNTIME_COLD_START)
        self.assertIsNone(out.expected_prior)


if __name__ == "__main__":
    unittest.main()