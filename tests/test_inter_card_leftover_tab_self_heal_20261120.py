"""INTER_CARD leftover-tab self-heal: close pricer tabs, COLD_START, one cold retry."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.browser_lifecycle_policy import (
    RUNTIME_COLD_START,
    RUNTIME_INTER_CARD,
    PriorCardContext,
)
from cardscanr_market_engine.inter_card_cdp_recovery import (
    INTER_CARD_EXPECTED_TARGET_REJECTED,
    observe_inter_card_no_request_outcome,
)
from cardscanr_market_engine.navigation_runtime_context import NavigationRuntimeContext
from cardscanr_market_engine.owned_daily_outcomes import CHALLENGE_REQUIRED

NOW = "2026-11-20T10:00:00Z"
KABUTOPS_URL = "https://www.ebay.com.au/sch/i.html?_nkw=Kabutops&LH_Sold=1"
SORRY_URL = "https://www.ebay.com.au/sorry.html"


class LeftoverTabSelfHealObserveTests(unittest.TestCase):
    def _prior(self, *, query: str = "Moltres") -> PriorCardContext:
        return PriorCardContext(
            job_id="moltres-job",
            target_id=None,
            query=query,
            x11_sold_state_verified=True,
            card_verdict="PASS_PRICE_UPDATED",
        )

    def _reject_kabutops_tab(self, ctx: NavigationRuntimeContext) -> None:
        observe_inter_card_no_request_outcome(
            ctx,
            runtime_error=INTER_CARD_EXPECTED_TARGET_REJECTED,
            search_submission_started=False,
            raw_error="Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED",
            diagnostics={
                "urlBeforeSearch": KABUTOPS_URL,
                "titleBeforeSearch": "Kabutops sold",
                "targetPolicy": {"reason_codes": [INTER_CARD_EXPECTED_TARGET_REJECTED]},
            },
            page_url=KABUTOPS_URL,
            page_title="Kabutops sold",
        )

    def test_two_leftover_rejections_self_heal_then_cold_start(self) -> None:
        ctx = NavigationRuntimeContext(
            runtime_mode=RUNTIME_INTER_CARD,
            expected_prior=self._prior(),
        )
        ctx.current_job_id = "job-2"
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "recovery.json"
            nav_path = Path(tmp) / "nav.json"
            receipt_path = Path(tmp) / "receipts.jsonl"
            close_mock = mock.Mock(
                return_value={"resetMethod": "cdp_close_ebay_tabs_same_process", "closedTargetIds": ["TAB-KABU"]}
            )
            with (
                mock.patch("cardscanr_market_engine.inter_card_cdp_recovery.CDP_RECOVERY_STATE_PATH", state_path),
                mock.patch(
                    "cardscanr_market_engine.inter_card_cdp_recovery.INTER_CARD_SELF_HEAL_RECEIPTS_PATH",
                    receipt_path,
                ),
                mock.patch(
                    "cardscanr_market_engine.inter_card_cdp_recovery.bind_persisted_nav_context_path",
                    return_value=nav_path,
                ),
                mock.patch(
                    "cardscanr_market_engine.inter_card_cdp_recovery._close_pricer_ebay_tabs_for_cold_start",
                    close_mock,
                ),
            ):
                ctx_after_1, latch1, r1 = observe_inter_card_no_request_outcome(
                    ctx,
                    runtime_error=INTER_CARD_EXPECTED_TARGET_REJECTED,
                    search_submission_started=False,
                    raw_error="Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED",
                )
                self.assertIsNone(r1)
                self.assertIsNone(latch1)
                out, latch2, receipt = observe_inter_card_no_request_outcome(
                    ctx_after_1,
                    runtime_error=INTER_CARD_EXPECTED_TARGET_REJECTED,
                    search_submission_started=False,
                    raw_error="Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED",
                )
            close_mock.assert_called_once()
            self.assertEqual(out.runtime_mode, RUNTIME_COLD_START)
            self.assertIsNone(out.expected_prior)
            self.assertIsNotNone(receipt)
            assert receipt is not None
            self.assertEqual(receipt.get("status"), "EXECUTED")
            self.assertIn("TAB-KABU", receipt.get("closedTargetIds") or [])
            nav_saved = json.loads(nav_path.read_text(encoding="utf-8"))
            self.assertEqual(nav_saved.get("runtimeMode"), RUNTIME_COLD_START)
            self.assertIsNone(nav_saved.get("expectedPrior"))
            self.assertTrue(receipt_path.is_file())

    def test_sorry_page_does_not_self_heal(self) -> None:
        ctx = NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=self._prior())
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "recovery.json"
            with (
                mock.patch("cardscanr_market_engine.inter_card_cdp_recovery.CDP_RECOVERY_STATE_PATH", state_path),
                mock.patch(
                    "cardscanr_market_engine.inter_card_cdp_recovery._close_pricer_ebay_tabs_for_cold_start",
                ) as close_mock,
            ):
                _, latch, receipt = observe_inter_card_no_request_outcome(
                    ctx,
                    runtime_error=INTER_CARD_EXPECTED_TARGET_REJECTED,
                    search_submission_started=False,
                    raw_error="Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED",
                    page_url=SORRY_URL,
                    page_title="Sorry",
                )
            close_mock.assert_not_called()
            self.assertEqual(latch, CHALLENGE_REQUIRED)
            self.assertIsNone(receipt)

    def test_self_heal_cap_blocks_tab_close(self) -> None:
        ctx = NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=self._prior())
        ctx.current_job_id = "job-cap"
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "recovery.json"
            state_path.write_text(
                json.dumps(
                    {
                        "selfHealReceipts": [
                            {"status": "EXECUTED", "atUtc": NOW},
                            {"status": "EXECUTED", "atUtc": NOW},
                            {"status": "EXECUTED", "atUtc": NOW},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch("cardscanr_market_engine.inter_card_cdp_recovery.CDP_RECOVERY_STATE_PATH", state_path),
                mock.patch(
                    "cardscanr_market_engine.inter_card_cdp_recovery._utc_now",
                    return_value=__import__("datetime").datetime(2026, 11, 20, 10, 0, tzinfo=__import__("datetime").timezone.utc),
                ),
                mock.patch(
                    "cardscanr_market_engine.inter_card_cdp_recovery._close_pricer_ebay_tabs_for_cold_start",
                ) as close_mock,
            ):
                observe_inter_card_no_request_outcome(
                    ctx,
                    runtime_error=INTER_CARD_EXPECTED_TARGET_REJECTED,
                    search_submission_started=False,
                    raw_error="Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED",
                )
                _, _, receipt = observe_inter_card_no_request_outcome(
                    ctx,
                    runtime_error=INTER_CARD_EXPECTED_TARGET_REJECTED,
                    search_submission_started=False,
                    raw_error="Desktop sold navigation failed: INTER_CARD_EXPECTED_TARGET_REJECTED",
                )
            close_mock.assert_not_called()
            assert receipt is not None
            self.assertEqual(receipt.get("status"), "CAP_EXCEEDED")


class OwnedDailyOffHealthyPersistTests(unittest.TestCase):
    def test_alternating_fail_success_does_not_false_latch_hard_stop(self) -> None:
        from tests.test_worker_submission_cap_accounting_20261119 import (
            WorkerTransientHealthyPersistTests,
        )

        WorkerTransientHealthyPersistTests().test_alternating_fail_success_does_not_false_latch_hard_stop()


if __name__ == "__main__":
    unittest.main()