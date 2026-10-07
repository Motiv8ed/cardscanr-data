#!/usr/bin/env python3
"""Offline closure: post-Sold page health + CDP target classification taxonomy."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.marketplace_ops_state import classify_provider_failure
from cardscanr_market_engine.owned_daily_outcomes import (
    EBAY_AUTH_REQUIRED,
    POST_SOLD_CAPTURE_FAILURE,
    TEMPORARY_EBAY_SERVER_FAILURE,
    classify_exception_outcome,
)
from cardscanr_market_engine.providers.errors import ProviderAuthenticationRequiredError, ProviderTemporaryError
from cardscanr_market_engine.failure_policy import FAILURE_CLASS_IDENTITY, build_failure_policy
from cardscanr_market_engine.providers.post_sold_capture import (
    CDP_TARGET_ATTACH_FAILURE,
    CDP_TARGET_NOT_FOUND,
    MARKETPLACE_ERROR_PAGE,
    POST_SOLD_CAPTURE_READY,
    PageTargetCandidate,
    capture_verified_sold_page,
    select_sold_page_target,
)
from cardscanr_market_engine.providers.sold_navigation_phases import (
    FixtureClock,
    TERMINAL_SOLD_ERROR_PAGE,
    TERMINAL_SOLD_STATE_VERIFIED,
    classify_sold_observation,
    run_sold_fixture,
)
from cardscanr_market_engine.providers.sold_page_health import (
    EBAY_ERROR_PAGE,
    current_url_matches_card,
    evaluate_sold_verification,
    is_ebay_error_page,
    rejection_evidence_from_scored,
)

FIXTURES = ROOT / "reports" / "artifacts" / "post_sold_page_health_closure" / "fixtures"
ORDINARY = "https://www.ebay.com.au/sch/i.html?_nkw=Test+Card&_sacat=0"
SOLD = ORDINARY + "&LH_Sold=1"
TITLE_OK = "Test Card for sale | eBay"
TITLE_ERR = "Error Page | eBay"
SOLD_BODY = (
    "Results\nFilters\nSold items\n"
    + ("Sold 1 Oct 2026\nAU $4.41\nTest Card 1\n" * 20)
)


def _page(url: str, title: str, tid: str = "t1") -> dict:
    return {
        "id": tid,
        "type": "page",
        "url": url,
        "title": title,
        "webSocketDebuggerUrl": f"ws://127.0.0.1:9444/devtools/page/{tid}",
    }


class SoldHealthContractTests(unittest.TestCase):
    def test_lh_sold_healthy_results_verified(self) -> None:
        ev = evaluate_sold_verification(url=SOLD, title=TITLE_OK, body=SOLD_BODY)
        self.assertTrue(ev["soldFilterStateVerified"])
        self.assertTrue(ev["soldPageHealthVerified"])
        self.assertTrue(ev["x11SoldStateVerified"])
        self.assertTrue(ev["verified"])

    def test_lh_sold_healthy_zero_results_verified(self) -> None:
        zero = json.loads((FIXTURES / "healthy_zero_results.json").read_text(encoding="utf-8"))
        ev = evaluate_sold_verification(
            url=zero["expectedUrl"], title=zero["title"], body=zero["body"]
        )
        self.assertTrue(ev["soldFilterStateVerified"])
        self.assertTrue(ev["soldPageHealthVerified"])
        self.assertTrue(ev["x11SoldStateVerified"])

    def test_lh_sold_error_page_not_verified(self) -> None:
        ev = evaluate_sold_verification(url=SOLD, title=TITLE_ERR, body="")
        self.assertTrue(ev["soldFilterStateVerified"])
        self.assertFalse(ev["soldPageHealthVerified"])
        self.assertFalse(ev["x11SoldStateVerified"])
        self.assertEqual(ev["terminal"], EBAY_ERROR_PAGE)
        self.assertFalse(ev["captureReady"])

    def test_passive_recaptcha_iframe_healthy_pass(self) -> None:
        body = SOLD_BODY + "\n<iframe src='https://www.google.com/recaptcha/api2/anchor'></iframe>\n"
        ev = evaluate_sold_verification(url=SOLD, title=TITLE_OK, body=body)
        self.assertTrue(ev["x11SoldStateVerified"])

    def test_captcha_challenge_class(self) -> None:
        ev = evaluate_sold_verification(
            url=SOLD, title="Please verify yourself", body="captcha challenge"
        )
        self.assertEqual(ev["terminal"], "EBAY_CHALLENGE")
        self.assertFalse(ev["x11SoldStateVerified"])

    def test_signin_redirect_is_auth_required_not_timeout_or_captcha(self) -> None:
        url = (
            "https://signin.ebay.com/ws/eBayISAPI.dll?SignIn&siteid=0"
            "&ru=https%3A%2F%2Fwww.ebay.com%2Fsch%2Fi.html%3FLH_Sold%3D1"
        )
        ev = evaluate_sold_verification(url=url, title="Sign in or Register | eBay")
        self.assertEqual(ev["terminal"], "EBAY_AUTH_REQUIRED")
        self.assertNotEqual(ev["terminal"], "EBAY_CHALLENGE")
        self.assertFalse(ev["x11SoldStateVerified"])
        obs = classify_sold_observation(url=url, title="Sign in or Register | eBay")
        self.assertEqual(obs["terminal"], "EBAY_AUTH_REQUIRED")
        clock = FixtureClock(
            frames=[
                (0.0, ORDINARY, TITLE_OK, {}),
                (0.8, url, "Sign in or Register | eBay", {}),
            ],
            sold_control_at=0.2,
            click_at=0.4,
        )
        result = run_sold_fixture(clock, poll_s=0.05)
        self.assertEqual(result["terminal"], "EBAY_AUTH_REQUIRED")
        self.assertNotEqual(result["terminal"], TERMINAL_SOLD_STATE_VERIFIED)
        self.assertNotEqual(result.get("error"), "SOLD_STATE_VERIFICATION_TIMEOUT")
        self.assertFalse(result["ok"])

    def test_auth_exception_maps_to_ebay_auth_required_not_challenge(self) -> None:
        exc = ProviderAuthenticationRequiredError(
            "EBAY_AUTH_REQUIRED: eBay redirected pricing to sign-in",
            diagnostics={"providerOutcome": "authentication_required", "url": "https://signin.ebay.com/"},
        )
        self.assertEqual(classify_exception_outcome(exc, diagnostics=exc.diagnostics), EBAY_AUTH_REQUIRED)
        policy = build_failure_policy(exc)
        self.assertFalse(policy.retryable)
        self.assertEqual(policy.classification, FAILURE_CLASS_IDENTITY)
        timeout = ProviderTemporaryError(
            "Desktop sold navigation failed: SOLD_STATE_VERIFICATION_TIMEOUT",
            diagnostics={"desktopNav": {"url": "https://signin.ebay.com/ws/eBayISAPI.dll?SignIn"}},
        )
        self.assertEqual(
            classify_exception_outcome(timeout, diagnostics=timeout.diagnostics),
            EBAY_AUTH_REQUIRED,
        )

    def test_sorry_class_distinct(self) -> None:
        ev = evaluate_sold_verification(
            url=SOLD,
            title="Something went wrong | eBay",
            body="SORRY\nSomething went wrong on our end\n",
        )
        self.assertEqual(ev["terminal"], "EBAY_SORRY")
        self.assertNotEqual(ev["terminal"], EBAY_ERROR_PAGE)

    def test_403_class(self) -> None:
        ev = evaluate_sold_verification(url=SOLD, title=TITLE_OK, body="", http_status=403)
        self.assertEqual(ev["terminal"], "EBAY_ACCESS_DENIED_403")


class TargetTaxonomyTests(unittest.TestCase):
    def test_error_page_target_not_cdp_target_not_found(self) -> None:
        fx = json.loads((FIXTURES / "rowlet_error_page_targets.json").read_text(encoding="utf-8"))
        chosen, fail, scored = select_sold_page_target(
            fx["targets"],
            expected_url=fx["expectedUrl"],
            expected_query=fx["expectedQuery"],
            require_lh_sold=True,
        )
        self.assertIsNone(chosen)
        self.assertEqual(fail, MARKETPLACE_ERROR_PAGE)
        self.assertNotEqual(fail, CDP_TARGET_NOT_FOUND)
        rej = rejection_evidence_from_scored(
            scored,
            expected_url=fx["expectedUrl"],
            expected_query=fx["expectedQuery"],
            fail_cls=fail,
        )
        self.assertEqual(rej["targetCount"], 1)
        self.assertTrue(rej["expectedTargetFound"])
        self.assertIn("error_page", rej["rejectionReasons"])
        self.assertEqual(rej["healthClassification"], EBAY_ERROR_PAGE)
        self.assertEqual(rej["failureClass"], MARKETPLACE_ERROR_PAGE)

    def test_truly_absent_target(self) -> None:
        chosen, fail, _ = select_sold_page_target(
            [],
            expected_url=SOLD,
            expected_query="Test Card",
            require_lh_sold=True,
        )
        self.assertIsNone(chosen)
        self.assertEqual(fail, CDP_TARGET_NOT_FOUND)

    def test_attach_failure_class(self) -> None:
        def attach(_c: PageTargetCandidate) -> dict:
            raise RuntimeError("target closed")

        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD,
            expected_query="Test Card",
            list_targets=lambda: [_page(SOLD, TITLE_OK)],
            attach_and_read=attach,
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, CDP_TARGET_ATTACH_FAILURE)

    def test_healthy_target_selection_and_capture(self) -> None:
        fx = json.loads((FIXTURES / "healthy_sold_targets.json").read_text(encoding="utf-8"))

        def attach(c: PageTargetCandidate) -> dict:
            return {
                "ok": True,
                "url": c.url,
                "title": c.title,
                "body_text": fx["body"],
                "capture_method": "fixture",
                "readiness": {"readyState": "complete", "bodyInnerTextLength": len(fx["body"])},
            }

        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=fx["expectedUrl"],
            expected_query=fx["expectedQuery"],
            list_targets=lambda: fx["targets"],
            attach_and_read=attach,
            allow_one_local_retry=False,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.capture_phase, POST_SOLD_CAPTURE_READY)

    def test_error_page_capture_not_run(self) -> None:
        fx = json.loads((FIXTURES / "rowlet_error_page_targets.json").read_text(encoding="utf-8"))
        called = {"n": 0}

        def attach(_c: PageTargetCandidate) -> dict:
            called["n"] += 1
            return {"ok": True, "url": SOLD, "title": TITLE_OK, "body_text": SOLD_BODY}

        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=fx["expectedUrl"],
            expected_query=fx["expectedQuery"],
            list_targets=lambda: fx["targets"],
            attach_and_read=attach,
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, MARKETPLACE_ERROR_PAGE)
        self.assertEqual(called["n"], 0)
        self.assertEqual(result.diagnostics.get("capture"), "NOT_RUN")
        self.assertTrue(result.diagnostics.get("expectedTargetFound"))


class RowletReplayTests(unittest.TestCase):
    def test_rowlet_sold_fixture_error_page(self) -> None:
        fx = json.loads((FIXTURES / "rowlet_error_page_targets.json").read_text(encoding="utf-8"))
        url = fx["expectedUrl"]
        clock = FixtureClock(
            frames=[
                (0.0, ORDINARY.replace("Test+Card", "Rowlet+10+perfect+order+Pokemon"), TITLE_OK, {}),
                (0.2, url, TITLE_ERR, {}),
            ],
            sold_control_at=0.05,
            click_at=0.08,
        )
        out = run_sold_fixture(clock, poll_s=0.05)
        self.assertFalse(out["ok"])
        self.assertEqual(out["terminal"], TERMINAL_SOLD_ERROR_PAGE)
        d = out["diagnostics"]
        self.assertTrue(d.get("soldFilterStateVerified"))
        self.assertFalse(d.get("soldPageHealthVerified"))
        self.assertFalse(d.get("x11SoldStateVerified"))
        self.assertEqual(d.get("marketplacePageClass"), EBAY_ERROR_PAGE)
        self.assertEqual(d.get("capture"), "NOT_RUN")

    def test_rowlet_historical_misclassification_documented(self) -> None:
        fx = json.loads((FIXTURES / "rowlet_error_page_targets.json").read_text(encoding="utf-8"))
        self.assertEqual(fx["historicalReportedFailure"], "CDP_TARGET_NOT_FOUND")
        self.assertEqual(fx["historicalActual"], "CURRENT_TARGET_PRESENT_BUT_UNHEALTHY_ERROR_PAGE")
        self.assertTrue(is_ebay_error_page(title="Error Page | eBay", url=fx["expectedUrl"]))


class TransientHealthTests(unittest.TestCase):
    def test_transient_error_then_healthy(self) -> None:
        clock = FixtureClock(
            frames=[
                (0.0, ORDINARY, TITLE_OK, {}),
                (0.2, SOLD, TITLE_ERR, {}),
                (1.0, SOLD, TITLE_OK, {}),
            ],
            sold_control_at=0.05,
            click_at=0.08,
        )
        out = run_sold_fixture(clock, poll_s=0.05)
        self.assertTrue(out["ok"])
        self.assertEqual(out["terminal"], TERMINAL_SOLD_STATE_VERIFIED)
        self.assertTrue(out["diagnostics"]["soldPageHealthVerified"])


class InterCardTargetIdentityTests(unittest.TestCase):
    def test_same_target_id_new_url_valid(self) -> None:
        fx = json.loads((FIXTURES / "inter_card_same_target_id.json").read_text(encoding="utf-8"))
        self.assertEqual(fx["prior"]["targetId"], fx["current"]["targetId"])
        corr = current_url_matches_card(
            url=fx["current"]["url"],
            expected_query=fx["current"]["query"],
            prior_query=fx["prior"]["query"],
        )
        self.assertTrue(corr["matchesCurrentQuery"])
        self.assertTrue(corr["validInterCardReuse"])
        self.assertFalse(corr["stalePriorQuery"])


class FreshnessAndControlPlaneTests(unittest.TestCase):
    def test_error_page_outcome_not_capture_failure(self) -> None:
        exc = ProviderTemporaryError(
            "TEMPORARY_EBAY_SERVER_FAILURE: marketplace EBAY_ERROR_PAGE",
            diagnostics={
                "reason": "ebay_error_page",
                "failureClass": "MARKETPLACE_ERROR_PAGE",
                "marketplacePageClass": "EBAY_ERROR_PAGE",
                "markFresh": False,
            },
        )
        outcome = classify_exception_outcome(exc, diagnostics=exc.diagnostics)
        self.assertEqual(outcome, TEMPORARY_EBAY_SERVER_FAILURE)
        self.assertNotEqual(outcome, POST_SOLD_CAPTURE_FAILURE)

    def test_error_page_is_transient_ebay_not_challenge(self) -> None:
        cat = classify_provider_failure(
            "marketplace EBAY_ERROR_PAGE",
            diagnostics={"failureClass": "MARKETPLACE_ERROR_PAGE", "reason": "ebay_error_page"},
        )
        self.assertEqual(cat, "TRANSIENT_EBAY")
        self.assertNotEqual(cat, "CHALLENGE_REQUIRED")

    def test_no_browser_retry_flag(self) -> None:
        obs = classify_sold_observation(url=SOLD, title=TITLE_ERR)
        self.assertFalse(obs.get("verified"))
        # Fixture runner sets retries=0 always.
        clock = FixtureClock(
            frames=[(0.0, ORDINARY, TITLE_OK, {}), (0.2, SOLD, TITLE_ERR, {})],
            sold_control_at=0.05,
            click_at=0.08,
        )
        out = run_sold_fixture(clock, poll_s=0.05)
        self.assertEqual(out["diagnostics"].get("retries"), 0)


class HealthyThirteenReplayTests(unittest.TestCase):
    """Represent the 13 healthy rollout cards via local healthy Sold fixtures."""

    HEALTHY_CARDS = [
        "Meowth 56 jungle Pokemon",
        "Fearow 36 jungle Pokemon",
        "Gloom 37 jungle Pokemon",
        "Dragonite 5 wizards black star promos Pokemon",
        "Zubat 49 chaos rising Pokemon",
        "Sprigatito 13 scarlet violet Pokemon",
        "Charmander 20 ascended heroes Pokemon",
        "Nidoqueen 23 jungle Pokemon",
        "Pikachu 58 base Pokemon",
        "Sandslash 41 fossil Pokemon",
        "Arbok 31 fossil Pokemon",
        "Nidoran [F] 6 ec5 Pokemon",
        "Bulbasaur 1 pokemon go Pokemon",
    ]

    def test_healthy_replay_no_false_negatives(self) -> None:
        false_neg = 0
        for q in self.HEALTHY_CARDS:
            nkw = q.replace(" ", "+").replace("[", "%5B").replace("]", "%5D")
            url = f"https://www.ebay.com.au/sch/i.html?_nkw={nkw}&_sacat=0&LH_Sold=1"
            title = f"{q.split()[0]} for sale | eBay"
            body = f"Results\nFilters\nSold items\nSold 1 Oct 2026\nAU $1.00\n{q}\n"
            ev = evaluate_sold_verification(url=url, title=title, body=body)
            if not ev.get("x11SoldStateVerified"):
                false_neg += 1
        self.assertEqual(len(self.HEALTHY_CARDS), 13)
        self.assertEqual(false_neg, 0)


if __name__ == "__main__":
    unittest.main()
