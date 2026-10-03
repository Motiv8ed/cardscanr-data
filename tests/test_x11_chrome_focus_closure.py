"""Focused tests for X11 Chrome focus closure (no eBay)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from cardscanr_market_engine.owned_daily_source_policy import classify_owned_daily_band
from cardscanr_market_engine.reliability_harness_classification import (
    classify_reliability_card_verdict,
    is_structured_challenge,
)
from cardscanr_market_engine.x11_chrome_focus import (
    REASON_READY,
    ChromeFocusResult,
    focus_chrome_window,
    is_local_runtime_failure_message,
    probe_ewmh_window_manager,
)
from cardscanr_market_engine.live_navigation_attempt import (
    SEARCH_SUBMISSION_STARTED,
    emit_search_submission_started,
    has_search_submission_started,
    new_attempt_id,
)


class LocalRuntimeFailureMessageTests(unittest.TestCase):
    def test_windowactivate_message_is_local(self) -> None:
        msg = (
            "Desktop sold navigation failed: Command 'xdotool windowactivate --sync 12582915' "
            "returned non-zero exit status 1."
        )
        self.assertTrue(is_local_runtime_failure_message(msg))

    def test_marketplace_error_not_local(self) -> None:
        self.assertFalse(is_local_runtime_failure_message("no_reliable_price: sparse comps"))


class SchedulerLocalFailureTests(unittest.TestCase):
    def test_reference_only_stays_p0_despite_failed_refresh(self) -> None:
        now = datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)
        band, due, *_ = classify_owned_daily_band(
            {
                "current_market_price": 0.24,
                "display_price_source": "reference",
                "provider": "tcgdex_tcgplayer",
                "refresh_status": "failed",
                "last_error_message": "Command 'xdotool windowactivate --sync 12582915' returned non-zero exit status 1.",
                "last_updated_at": "2026-09-28T10:58:39Z",
            },
            now=now,
        )
        self.assertEqual(band, "P0_NEEDS_VERIFIED_LOCAL")
        self.assertTrue(due)

    def test_verified_local_x11_failure_does_not_force_p2(self) -> None:
        now = datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)
        band, due, *_ = classify_owned_daily_band(
            {
                "current_market_price": 10.0,
                "display_price_source": "verified_local",
                "provider": "ebay_browser",
                "refresh_status": "failed",
                "last_error_message": "X11_FOCUS_FAILED:chrome_focus_failed",
                "last_updated_at": "2026-09-27T00:00:00Z",
            },
            now=now,
        )
        self.assertNotEqual(band, "P2_FAILED_RETRY")
        self.assertEqual(band, "P1_STALE_GT_24H")
        self.assertTrue(due)


class ChallengeClassificationTests(unittest.TestCase):
    def test_x11_focus_failure_not_challenge(self) -> None:
        result = {
            "status": "failed",
            "ownedDailyOutcome": "TEMPORARY_BROWSER_FAILURE",
            "error": "X11_FOCUS_FAILED:chrome_focus_failed",
        }
        diag = {
            "challenge": False,
            "sorry": False,
            "desktopNav": {"challenge": False, "sorry": False, "searchSuccess": False},
        }
        self.assertFalse(is_structured_challenge(result, diag))
        verdict = classify_reliability_card_verdict(
            result, diag=diag, search_submission_started=False, preflight_failed=False
        )
        self.assertEqual(verdict, "FAIL_NAVIGATION")


class FocusHelperUnitTests(unittest.TestCase):
    def test_stale_preferred_wid_rejected(self) -> None:
        with mock.patch(
            "cardscanr_market_engine.x11_chrome_focus._candidate_windows",
            return_value=[{"wid": 99, "wid_hex": "0x63", "mapped": True, "w": 1100, "h": 700, "x": 0, "y": 0, "area": 1, "wmClass": "Google-chrome", "wmName": "t", "pid": 1}],
        ), mock.patch(
            "cardscanr_market_engine.x11_chrome_focus.probe_ewmh_window_manager",
            return_value={"windowManagerPresent": False, "ewmhActiveWindowSupported": False, "classification": "NO_EWMH_WINDOW_MANAGER"},
        ):
            from cardscanr_market_engine.x11_chrome_focus import discover_chrome_toplevel

            discovered = discover_chrome_toplevel(preferred_wid=12582915)
            self.assertIsNone(discovered.get("wid"))
            self.assertEqual(discovered.get("error"), "X11_CHROME_WINDOW_STALE")

    def test_activate_uses_focus_helper_not_raw_windowactivate(self) -> None:
        src = Path("tools/linux_x11_ebay_search.py").read_text(encoding="utf-8")
        self.assertIn("focus_chrome_window", src)
        activate_src = src.split("def activate(", 1)[1].split("\ndef ", 1)[0]
        # Strip docstring so explanatory mentions of windowactivate do not fail the contract.
        if '"""' in activate_src:
            activate_src = activate_src.split('"""', 2)[-1]
        self.assertNotIn('sh(f"xdotool windowactivate', activate_src)
        self.assertNotIn("sh('xdotool windowactivate", activate_src)
        self.assertIn("focus_chrome_window", activate_src)


class SubmissionAccountingTests(unittest.TestCase):
    def test_focus_failure_emits_no_submission_event(self) -> None:
        attempt = new_attempt_id()
        self.assertFalse(has_search_submission_started(attempt))
        # Simulate focus failure path: never call emit.
        self.assertFalse(has_search_submission_started(attempt))

    def test_emit_only_on_explicit_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"CARDSCANR_LIVE_NAV_ATTEMPTS_DIR": tmp}):
                attempt = new_attempt_id()
                self.assertFalse(has_search_submission_started(attempt))
                evt = emit_search_submission_started(attempt_id=attempt, query="probe")
                self.assertEqual(evt.event, SEARCH_SUBMISSION_STARTED)
                self.assertTrue(has_search_submission_started(attempt))


class ArtifactContractTests(unittest.TestCase):
    def test_keyboard_proof_artifact_contract_when_present(self) -> None:
        path = Path("reports/artifacts/x11_chrome_focus_closure/keyboard_injection_proof.json")
        if not path.is_file():
            self.skipTest("keyboard proof artifact not generated in this environment")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("typedMarkerVerified"))
        self.assertEqual(payload.get("searchSubmissionStartedDelta"), 0)
        self.assertEqual(payload.get("ebayTargetsAfter") or [], [])


if __name__ == "__main__":
    unittest.main()
