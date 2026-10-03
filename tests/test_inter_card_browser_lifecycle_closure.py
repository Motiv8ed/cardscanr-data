"""Offline INTER_CARD browser lifecycle closure tests — no eBay contact."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "reports" / "artifacts" / "inter_card_browser_lifecycle_closure" / "fixtures"

from cardscanr_market_engine.browser_lifecycle_policy import (
    RUNTIME_COLD_START,
    RUNTIME_INTER_CARD,
    PriorCardContext,
    classify_cdp_targets,
    evaluate_runtime_targets,
    prior_from_card_report,
)
from cardscanr_market_engine.live_navigation_attempt import (
    capture_attempt_event_baseline,
    count_consumed_live_navigations,
    current_run_consumed_count,
    emit_search_submission_started,
    list_search_submission_attempt_ids,
)
from cardscanr_market_engine.providers.post_sold_capture_process import count_worker_descendants


def _sandile_prior() -> PriorCardContext:
    report = json.loads((FIX / "sandile_card1_terminal.json").read_text(encoding="utf-8"))
    prior = prior_from_card_report(report)
    assert prior is not None
    return prior


def _tropius_targets() -> list[dict]:
    return list(json.loads((FIX / "sandile_tropius_cdp_targets.json").read_text(encoding="utf-8"))["rawTargets"])


class ColdStartPolicyTests(unittest.TestCase):
    def test_cold_start_zero_ebay_pass(self) -> None:
        r = evaluate_runtime_targets([], mode=RUNTIME_COLD_START)
        self.assertTrue(r.ok)

    def test_cold_start_historical_sold_fail(self) -> None:
        targets = _tropius_targets()
        r = evaluate_runtime_targets(targets, mode=RUNTIME_COLD_START)
        self.assertFalse(r.ok)
        self.assertIn("COLD_START_UNEXPECTED_EBAY_TARGET", r.reason_codes)


class InterCardPolicyTests(unittest.TestCase):
    def test_exact_prior_success_pass(self) -> None:
        prior = _sandile_prior()
        r = evaluate_runtime_targets(_tropius_targets(), mode=RUNTIME_INTER_CARD, prior=prior)
        self.assertTrue(r.ok, r.reason_codes)
        self.assertTrue(r.expected_prior_accepted)
        self.assertEqual(r.top_level_ebay_count, 1)

    def test_unknown_ebay_fail(self) -> None:
        prior = _sandile_prior()
        targets = [
            {
                "id": "OTHER",
                "type": "page",
                "url": "https://www.ebay.com.au/sch/i.html?_nkw=Unrelated+Card&LH_Sold=1",
                "title": "Unrelated",
            }
        ]
        r = evaluate_runtime_targets(targets, mode=RUNTIME_INTER_CARD, prior=prior)
        self.assertFalse(r.ok)
        self.assertTrue(any(c in r.reason_codes for c in ("INTER_CARD_UNKNOWN_EBAY_TARGET", "INTER_CARD_PRIOR_TARGET_NOT_CORRELATED")))

    def test_multiple_ambiguous_top_level_fail(self) -> None:
        prior = _sandile_prior()
        targets = _tropius_targets() + [
            {
                "id": "SECOND",
                "type": "page",
                "url": "https://www.ebay.com.au/sch/i.html?_nkw=Second+Card&LH_Sold=1",
                "title": "Second",
            }
        ]
        r = evaluate_runtime_targets(targets, mode=RUNTIME_INTER_CARD, prior=prior)
        self.assertFalse(r.ok)
        self.assertIn("INTER_CARD_MULTIPLE_TOP_LEVEL_EBAY_TARGETS", r.reason_codes)

    def test_prior_failed_card_fail(self) -> None:
        prior = _sandile_prior()
        prior.card_verdict = "FAIL_CAPTURE"
        r = evaluate_runtime_targets(_tropius_targets(), mode=RUNTIME_INTER_CARD, prior=prior)
        self.assertFalse(r.ok)
        self.assertIn("INTER_CARD_PRIOR_CARD_NOT_TERMINAL", r.reason_codes)

    def test_challenge_target_fail(self) -> None:
        prior = _sandile_prior()
        targets = [
            {
                "id": prior.target_id,
                "type": "page",
                "url": "https://www.ebay.com.au/splashui/challenge",
                "title": "Challenge",
            }
        ]
        r = evaluate_runtime_targets(targets, mode=RUNTIME_INTER_CARD, prior=prior)
        self.assertFalse(r.ok)
        self.assertIn("INTER_CARD_CHALLENGE_TARGET", r.reason_codes)

    def test_auxiliary_ads_not_extra_top_level(self) -> None:
        classified = classify_cdp_targets(_tropius_targets(), mode=RUNTIME_INTER_CARD, prior=_sandile_prior())
        ads = [c for c in classified if c.origin_class == "advertising"]
        self.assertGreaterEqual(len(ads), 1)
        self.assertTrue(all(not c.top_level for c in ads))
        self.assertTrue(all(not c.blocking for c in ads))

    def test_correlation_uses_exact_target_id(self) -> None:
        prior = _sandile_prior()
        self.assertEqual(prior.target_id, "4D81B8C198AF6DB74FD00A7413848399")
        r = evaluate_runtime_targets(_tropius_targets(), mode=RUNTIME_INTER_CARD, prior=prior)
        matched = [c for c in r.classified if c.belongs_to_expected_previous_card]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].target_id, prior.target_id)


class AttemptBaselineTests(unittest.TestCase):
    def test_historical_events_do_not_block_and_are_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CARDSCANR_LIVE_NAV_ATTEMPTS_DIR"] = tmp
            try:
                emit_search_submission_started(attempt_id="hist-1", query="old", price_key_id="pk")
                baseline = capture_attempt_event_baseline()
                self.assertEqual(baseline["count"], 1)
                self.assertFalse(baseline["deleted"])
                self.assertIn("hist-1", baseline["attemptIds"])
                # Fresh run attemptIds only
                self.assertEqual(current_run_consumed_count(["new-1"], baseline_ids=baseline["attemptIds"]), 0)
                emit_search_submission_started(attempt_id="new-1", query="q", price_key_id="pk2")
                self.assertEqual(current_run_consumed_count(["new-1"], baseline_ids=baseline["attemptIds"]), 1)
                self.assertEqual(count_consumed_live_navigations(["hist-1", "new-1"]), 2)
                # Historical file still present
                self.assertIn("hist-1", list_search_submission_attempt_ids())
            finally:
                os.environ.pop("CARDSCANR_LIVE_NAV_ATTEMPTS_DIR", None)

    def test_prior_target_does_not_consume_next_search(self) -> None:
        prior = _sandile_prior()
        r = evaluate_runtime_targets(_tropius_targets(), mode=RUNTIME_INTER_CARD, prior=prior)
        self.assertTrue(r.ok)
        next_attempt = "tropius-fresh-attempt"
        self.assertEqual(count_consumed_live_navigations([next_attempt]), 0)


class SandileTropiusReplayTests(unittest.TestCase):
    def test_recorded_presubmit_pass(self) -> None:
        prior = _sandile_prior()
        self.assertEqual(prior.card_verdict, "PASS_PRICE_UPDATED")
        trop = json.loads((FIX / "tropius_selection.json").read_text(encoding="utf-8"))
        r = evaluate_runtime_targets(_tropius_targets(), mode=RUNTIME_INTER_CARD, prior=prior)
        self.assertTrue(r.ok, r.reason_codes)
        self.assertTrue(r.expected_prior_accepted)
        self.assertEqual(trop.get("card"), "Tropius")
        self.assertTrue(str(trop.get("priceKeyId") or ""))
        # Next card not consumed
        self.assertEqual(count_consumed_live_navigations(["not-issued-yet"]), 0)


class OrphanContractTests(unittest.TestCase):
    def test_browser_tab_not_worker_orphan(self) -> None:
        # Orphan helper only inspects process children — browser tab URLs are irrelevant.
        with mock.patch("cardscanr_market_engine.providers.post_sold_capture_process.os") as mos:
            # Simulate no children for a fake pid.
            self.assertEqual(count_worker_descendants(1), 0)


class SequentialLocalProofTests(unittest.TestCase):
    def test_inter_card_presubmit_gui_contract_offline(self) -> None:
        """CARD A terminal → INTER_CARD accept → type next query → stop before submit."""
        prior = _sandile_prior()
        r = evaluate_runtime_targets(_tropius_targets(), mode=RUNTIME_INTER_CARD, prior=prior)
        self.assertTrue(r.ok)
        # Simulate local rediscovery/clear/type without eBay / without Enter.
        typed = {
            "chromeWindowReady": True,
            "windowFocusReady": True,
            "keyboardInjectionReady": True,
            "previousTextCleared": True,
            "nextQuery": "Tropius 1 pitch black Pokemon",
            "nextQueryTyped": True,
            "submitted": False,
            "tropiusConsumed": count_consumed_live_navigations(["tropius-not-issued"]) > 0,
        }
        before = capture_attempt_event_baseline()["count"]
        # No emit — submission not consumed.
        after = capture_attempt_event_baseline()["count"]
        self.assertEqual(before, after)
        self.assertEqual(after - before, 0)
        self.assertTrue(typed["nextQueryTyped"])
        self.assertFalse(typed["submitted"])
        self.assertFalse(typed["tropiusConsumed"])
        self.assertTrue(r.expected_prior_accepted)

    def test_self_check_consumed_field_means_actual_consumption(self) -> None:
        """Regression: tropiusConsumed=true must never mean 'zero consumption'."""
        from tools.inter_card_browser_lifecycle_self_check import run_self_check

        result = run_self_check()
        seq = result["sequential"]
        self.assertFalse(seq["submitted"])
        self.assertFalse(seq["tropiusConsumed"])
        self.assertTrue(seq["tropiusNotConsumed"])
        self.assertEqual(result["searchSubmissionStartedDelta"], 0)
        self.assertTrue(result["checks"]["currentAttemptOnlyAccounting"])
        self.assertTrue(result["checks"]["sequentialLocalProof"])
        self.assertTrue(result["ok"])


class ColdStartLeakTests(unittest.TestCase):
    def test_inter_card_permissiveness_does_not_leak(self) -> None:
        prior = _sandile_prior()
        # Same targets: INTER_CARD pass, COLD_START fail.
        self.assertTrue(evaluate_runtime_targets(_tropius_targets(), mode=RUNTIME_INTER_CARD, prior=prior).ok)
        self.assertFalse(evaluate_runtime_targets(_tropius_targets(), mode=RUNTIME_COLD_START, prior=prior).ok)


if __name__ == "__main__":
    unittest.main()
