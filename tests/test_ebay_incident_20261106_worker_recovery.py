"""Regression tests for 2026-11-06 AU eBay worker HARD_STOP (incomplete Sold + broken INTER_CARD tab)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cardscanr_market_engine.browser_lifecycle_policy import (
    INTER_CARD_RECOVERY_STRATEGY,
    INTER_CARD_STRATEGY,
    RUNTIME_COLD_START,
    RUNTIME_INTER_CARD,
    PriorCardContext,
)
from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.continuous_worker_policy import HARD_STOP_OUTCOMES
from cardscanr_market_engine.job_runner import maybe_invalidate_inter_card_after_failure
from cardscanr_market_engine.navigation_runtime_context import (
    NavigationRuntimeContext,
    apply_context_to_environ,
    clear_context_from_environ,
    load_navigation_runtime_context,
)
from cardscanr_market_engine.owned_daily_outcomes import CHALLENGE_REQUIRED, POST_SOLD_CAPTURE_FAILURE
from cardscanr_market_engine.providers.linux_x11_ebay_nav import navigate_query_to_sold
from cardscanr_market_engine.providers.post_sold_capture import (
    CDP_DOCUMENT_NOT_READY,
    POST_SOLD_CAPTURE_FAILED,
    PageTargetCandidate,
    assess_document_readiness,
    capture_verified_sold_page,
    verify_captured_sold_state,
)
from cardscanr_market_engine.providers.sold_page_health import evaluate_sold_verification

SOLD_URL = (
    "https://www.ebay.com.au/sch/i.html?"
    "_nkw=Dragonite+5+wizards+black+star+promos+Pokemon&_sacat=0&LH_Sold=1"
)
TITLE_OK = "Dragonite 5 wizards black star promos Pokemon for sale | eBay"


class IncompleteSoldEvidenceTests(unittest.TestCase):
    def test_url_title_only_with_tiny_body_does_not_verify(self) -> None:
        tiny_body = TITLE_OK + "\n" + ("x" * 180)  # URL-ish title + garbage, no sold structure
        ev = evaluate_sold_verification(url=SOLD_URL, title=TITLE_OK, body=tiny_body)
        self.assertFalse(ev["verified"])
        self.assertFalse(ev["x11SoldStateVerified"])
        sold = verify_captured_sold_state(url=SOLD_URL, title=TITLE_OK, body_text=tiny_body)
        self.assertFalse(sold["SOLD_STATE_VERIFIED"])

    def test_cdp_empty_document_not_verified(self) -> None:
        def attach(_c: PageTargetCandidate) -> dict:
            return {
                "ok": False,
                "url": SOLD_URL,
                "title": TITLE_OK,
                "body_text": "",
                "error": "empty_document_body_after_fallback_chain",
                "readiness": assess_document_readiness(
                    {
                        "readyState": "complete",
                        "documentElementPresent": True,
                        "bodyPresent": True,
                        "bodyInnerTextLength": 209,
                        "outerHTMLLength": 0,
                        "frameUrl": SOLD_URL,
                        "listingNodeCount": 0,
                    }
                ),
                "methods": {"locator_inner_text": {"attempted": True, "length": 0}},
            }

        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query="Dragonite 5 wizards black star promos Pokemon",
            list_targets=lambda: [
                {"id": "t-dragonite", "type": "page", "url": SOLD_URL, "title": TITLE_OK}
            ],
            attach_and_read=attach,
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, CDP_DOCUMENT_NOT_READY)


class InterCardRecoveryAfterCaptureFailureTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_context_from_environ()

    def test_capture_failure_invalidates_inter_card_for_next_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ctx_path = Path(tmp) / "nav_runtime_context.json"
            prior = PriorCardContext(
                job_id="prior-1",
                target_id="BROKEN_TAB",
                final_url=SOLD_URL,
                query="Dragonite",
                x11_sold_state_verified=True,
                capture_correlated=True,
                card_verdict="PASS_PRICE_UPDATED",
                market="AU",
                currency="AUD",
            )
            with mock.patch(
                "cardscanr_market_engine.navigation_runtime_context.DEFAULT_CONTEXT_PATH",
                ctx_path,
            ):
                apply_context_to_environ(
                    NavigationRuntimeContext(
                        runtime_mode=RUNTIME_INTER_CARD,
                        expected_prior=prior,
                    )
                )
                ctx_path.write_text(
                    '{"runtimeMode":"INTER_CARD","expectedPrior":{"jobId":"prior-1"}}',
                    encoding="utf-8",
                )
                os.environ["CARDSCANR_NAV_CONTEXT_JSON_PATH"] = str(ctx_path)
                maybe_invalidate_inter_card_after_failure(
                    logger=lambda *_args, **_kwargs: None,
                    owned_outcome=POST_SOLD_CAPTURE_FAILURE,
                    result={
                        "postSoldCapturePhase": POST_SOLD_CAPTURE_FAILED,
                        "failureClass": CDP_DOCUMENT_NOT_READY,
                        "incompleteSoldEvidence": True,
                    },
                    provider_diagnostics={
                        "diagnostics": {
                            "failureClass": CDP_DOCUMENT_NOT_READY,
                            "incompleteSoldEvidence": True,
                        }
                    },
                )
                loaded = load_navigation_runtime_context()
                self.assertEqual(loaded.runtime_mode, RUNTIME_COLD_START)
                self.assertIsNone(loaded.expected_prior)

    def test_next_card_search_uses_recovery_not_reuse_same_tab(self) -> None:
        apply_context_to_environ(NavigationRuntimeContext(runtime_mode=RUNTIME_COLD_START))
        captured: dict = {}

        def _fake_wsl(args: list[str], **kwargs: object) -> dict:
            captured["args"] = list(args)
            captured["env"] = dict(kwargs.get("env_exports") or {})
            return {
                "ok": False,
                "error": "SEARCH_INPUT_NOT_CONFIRMED",
                "diagnostics": {"strategy": INTER_CARD_RECOVERY_STRATEGY, "goHome": True},
            }

        with mock.patch(
            "cardscanr_market_engine.providers.linux_x11_ebay_nav._wsl_python",
            side_effect=_fake_wsl,
        ):
            navigate_query_to_sold("Pikachu 58 base Pokemon", reset_homepage=True)
        self.assertIn("--home", captured["args"])
        self.assertIn(RUNTIME_COLD_START, captured["args"])
        self.assertNotIn(RUNTIME_INTER_CARD, captured["args"])


class ContinuousSafetyLatchTests(unittest.TestCase):
    def test_recovery_failure_still_increments_transient_latch(self) -> None:
        budget = ContinuousSafetyBudget(max_consecutive_transient=3, max_transient_per_hour=5)
        reason = None
        for _ in range(3):
            reason = budget.record_transient()
        self.assertEqual(reason, "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES")
        self.assertEqual(
            budget.last_hard_stop,
            "MAX_CONSECUTIVE_TRANSIENT_MARKETPLACE_FAILURES",
        )

    def test_challenge_still_hard_stop_outcome(self) -> None:
        self.assertIn(CHALLENGE_REQUIRED, HARD_STOP_OUTCOMES)
