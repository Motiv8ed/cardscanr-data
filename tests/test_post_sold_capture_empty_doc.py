"""Offline tests for empty post-Sold capture readiness, fallbacks, and integrity."""
from __future__ import annotations

import unittest

from cardscanr_market_engine.owned_daily_outcomes import (
    POST_SOLD_CAPTURE_FAILURE,
    classify_exception_outcome,
)
from cardscanr_market_engine.providers.post_sold_capture import (
    CDP_CAPTURE_METHOD_FAILURE,
    CDP_DOCUMENT_NOT_READY,
    CDP_INTEGRITY_FAILURE,
    CDP_TARGET_AMBIGUOUS,
    CDP_TARGET_CHANGED,
    CDP_TARGET_NOT_FOUND,
    CDP_TARGET_STALE,
    CDP_TARGET_URL_MISMATCH,
    MIN_PLAUSIBLE_SOLD_BODY_CHARS,
    POST_SOLD_CAPTURE_FAILED,
    POST_SOLD_CAPTURE_READY,
    PageTargetCandidate,
    assess_document_readiness,
    capture_integrity_ok,
    capture_verified_sold_page,
    classify_empty_capture_failure,
    select_sold_page_target,
)


SOLD_URL = (
    "https://www.ebay.com.au/sch/i.html?"
    "_nkw=Bloodmoon+Ursaluna+54+prismatic+evolutions+Pokemon&_sacat=0&LH_Sold=1"
)
QUERY = "Bloodmoon Ursaluna 54 prismatic evolutions Pokemon"
SOLD_BODY = (
    "Results\nFilters\nBloodmoon Ursaluna 54 prismatic evolutions Pokemon\n"
    + ("Sold 30 Sep 2026\nAU $2.50\nBloodmoon Ursaluna 054/131 Holo\n" * 40)
)


def _page(url: str, title: str = "Bloodmoon | eBay", tid: str = "t1") -> dict:
    return {"id": tid, "type": "page", "url": url, "title": title}


class TestCaptureReadinessAndFallback(unittest.TestCase):
    def test_immediately_readable_document(self) -> None:
        def attach(c: PageTargetCandidate) -> dict:
            return {
                "ok": True,
                "url": SOLD_URL,
                "title": "Bloodmoon",
                "body_text": SOLD_BODY,
                "capture_method": "cdp_locator_inner_text",
                "readiness": assess_document_readiness(
                    {
                        "readyState": "complete",
                        "documentElementPresent": True,
                        "bodyPresent": True,
                        "bodyInnerTextLength": len(SOLD_BODY),
                        "outerHTMLLength": len(SOLD_BODY) + 100,
                        "frameUrl": SOLD_URL,
                        "listingNodeCount": 12,
                    }
                ),
                "methods": {"locator_inner_text": {"attempted": True, "length": len(SOLD_BODY)}},
            }

        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(SOLD_URL, tid="sold")],
            attach_and_read=attach,
            allow_one_local_retry=False,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.capture_phase, POST_SOLD_CAPTURE_READY)

    def test_temporarily_empty_then_ready(self) -> None:
        calls = {"n": 0}

        def attach(c: PageTargetCandidate) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "ok": False,
                    "url": SOLD_URL,
                    "title": "Bloodmoon",
                    "body_text": "",
                    "error": "empty_document_body_after_fallback_chain",
                    "readiness": assess_document_readiness(
                        {
                            "readyState": "loading",
                            "documentElementPresent": True,
                            "bodyPresent": True,
                            "bodyInnerTextLength": 0,
                            "outerHTMLLength": 0,
                            "frameUrl": SOLD_URL,
                            "listingNodeCount": 0,
                        }
                    ),
                    "methods": {"locator_inner_text": {"attempted": True, "length": 0}},
                }
            return {
                "ok": True,
                "url": SOLD_URL,
                "title": "Bloodmoon",
                "body_text": SOLD_BODY,
                "capture_method": "cdp_evaluate_inner_text",
                "readiness": assess_document_readiness(
                    {
                        "readyState": "complete",
                        "documentElementPresent": True,
                        "bodyPresent": True,
                        "bodyInnerTextLength": len(SOLD_BODY),
                        "outerHTMLLength": 5000,
                        "frameUrl": SOLD_URL,
                        "listingNodeCount": 8,
                    }
                ),
                "methods": {"evaluate_inner_text": {"attempted": True, "length": len(SOLD_BODY)}},
            }

        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(SOLD_URL, tid="sold")],
            attach_and_read=attach,
            disconnect=lambda: None,
            allow_one_local_retry=True,
        )
        self.assertTrue(result.success)
        self.assertTrue(result.retry_used)
        self.assertEqual(calls["n"], 2)

    def test_permanently_empty_without_x11_fallback(self) -> None:
        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(SOLD_URL, tid="sold")],
            attach_and_read=lambda c: {
                "ok": False,
                "url": SOLD_URL,
                "title": "Bloodmoon",
                "body_text": "",
                "error": "empty_document_body_after_fallback_chain",
                "readiness": assess_document_readiness(
                    {
                        "readyState": "complete",
                        "documentElementPresent": True,
                        "bodyPresent": True,
                        "bodyInnerTextLength": 0,
                        "outerHTMLLength": 0,
                        "frameUrl": SOLD_URL,
                        "listingNodeCount": 0,
                    }
                ),
                "methods": {
                    "locator_inner_text": {"attempted": True, "length": 0},
                    "evaluate_inner_text": {"attempted": True, "length": 0},
                    "page_content": {"attempted": True, "length": 0},
                    "evaluate_outer_html": {"attempted": True, "length": 0},
                },
            },
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, CDP_DOCUMENT_NOT_READY)
        self.assertTrue(result.x11_sold_state_verified)

    def test_high_level_empty_alternate_extraction_via_preverified(self) -> None:
        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(SOLD_URL, tid="sold")],
            attach_and_read=lambda c: {
                "ok": False,
                "url": SOLD_URL,
                "title": "Bloodmoon Ursaluna 54 Prismatic Evolutions Pokemon for sale | eBay",
                "body_text": "",
                "error": "empty_document_body_after_fallback_chain",
                "readiness": assess_document_readiness(
                    {
                        "readyState": "complete",
                        "documentElementPresent": True,
                        "bodyPresent": True,
                        "bodyInnerTextLength": 0,
                        "outerHTMLLength": 12000,
                        "frameUrl": SOLD_URL,
                        "listingNodeCount": 20,
                    }
                ),
                "methods": {
                    "locator_inner_text": {"attempted": True, "length": 0},
                    "page_content": {"attempted": True, "length": 0},
                },
            },
            pre_verified_document=SOLD_BODY,
            pre_verified_document_source="x11_clipboard_file:linux_sold_nav_test_body.txt",
            allow_one_local_retry=False,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.capture_method, "x11_clipboard_file:linux_sold_nav_test_body.txt")
        self.assertTrue(result.diagnostics.get("fallbackToPreVerified"))
        self.assertEqual(result.diagnostics.get("cdpEmptyClass"), CDP_CAPTURE_METHOD_FAILURE)

    def test_target_disappears(self) -> None:
        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(SOLD_URL, tid="sold")],
            attach_and_read=lambda c: {
                "ok": False,
                "error": "bound_page_handle_missing",
                "targetMissing": True,
                "body_text": "",
            },
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, CDP_TARGET_STALE)

    def test_target_url_changes(self) -> None:
        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(SOLD_URL, tid="sold")],
            attach_and_read=lambda c: {
                "ok": True,
                "url": "https://www.ebay.com.au/",
                "title": "eBay Australia",
                "body_text": SOLD_BODY,
                "urlChanged": True,
            },
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, CDP_TARGET_URL_MISMATCH)

    def test_wrong_target_rejected(self) -> None:
        ordinary = SOLD_URL.replace("LH_Sold=1", "_trksid=x")
        chosen, fail, _ = select_sold_page_target(
            [_page(ordinary, tid="ord")],
            expected_url=SOLD_URL,
            expected_query=QUERY,
        )
        self.assertIsNone(chosen)
        # Plausible page present but missing LH_Sold — not "target absent".
        self.assertEqual(fail, CDP_TARGET_URL_MISMATCH)

    def test_ambiguous_rejected(self) -> None:
        a = SOLD_URL + "&_pgn=1"
        b = SOLD_URL + "&_pgn=2"
        chosen, fail, _ = select_sold_page_target(
            [_page(a, tid="a"), _page(b, tid="b")],
            expected_url="https://www.ebay.com.au/sch/i.html?_nkw=Bloodmoon+Ursaluna+54+prismatic+evolutions+Pokemon&LH_Sold=1",
            expected_query=QUERY,
        )
        self.assertIsNone(chosen)
        self.assertEqual(fail, CDP_TARGET_AMBIGUOUS)

    def test_sorry_rejected(self) -> None:
        integ = capture_integrity_ok(
            url=SOLD_URL,
            title="Error Page | eBay",
            body_text=SOLD_BODY,
            expected_url=SOLD_URL,
            expected_query=QUERY,
        )
        self.assertFalse(integ["ok"])
        self.assertIn("error_page", integ["reasons"])

    def test_classic_sorry_title_rejected(self) -> None:
        integ = capture_integrity_ok(
            url=SOLD_URL,
            title="Sorry! Something went wrong | eBay",
            body_text="SORRY\nSomething went wrong on our end\n",
            expected_url=SOLD_URL,
            expected_query=QUERY,
        )
        self.assertFalse(integ["ok"])
        self.assertIn("sorry_error", integ["reasons"])

    def test_live_rejected(self) -> None:
        integ = capture_integrity_ok(
            url="https://www.ebay.com.au/ebaylive/search?q=x&LH_Sold=1",
            title="Live",
            body_text=SOLD_BODY,
            expected_url=SOLD_URL,
            expected_query=QUERY,
        )
        self.assertFalse(integ["ok"])
        self.assertIn("ebay_live", integ["reasons"])

    def test_challenge_rejected(self) -> None:
        integ = capture_integrity_ok(
            url="https://www.ebay.com.au/splashui/challenge?LH_Sold=1",
            title="Please verify",
            body_text=SOLD_BODY,
            expected_url=SOLD_URL,
            expected_query=QUERY,
        )
        self.assertFalse(integ["ok"])
        self.assertIn("challenge", integ["reasons"])

    def test_valid_integrity_accepted(self) -> None:
        integ = capture_integrity_ok(
            url=SOLD_URL,
            title="Bloodmoon Ursaluna 54 Prismatic Evolutions Pokemon for sale | eBay",
            body_text=SOLD_BODY,
            expected_url=SOLD_URL,
            expected_query=QUERY,
        )
        self.assertTrue(integ["ok"])

    def test_garbage_rejected(self) -> None:
        integ = capture_integrity_ok(
            url=SOLD_URL,
            title="Bloodmoon",
            body_text="hi",
            expected_url=SOLD_URL,
            expected_query=QUERY,
        )
        self.assertFalse(integ["ok"])
        self.assertIn("body_too_small", integ["reasons"])

    def test_successful_capture_reaches_parser_payload(self) -> None:
        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(SOLD_URL, tid="sold")],
            attach_and_read=lambda c: {
                "ok": True,
                "url": SOLD_URL,
                "title": "Bloodmoon Ursaluna 54 Prismatic Evolutions Pokemon for sale | eBay",
                "body_text": SOLD_BODY,
            },
            allow_one_local_retry=False,
        )
        self.assertTrue(result.success)
        self.assertGreaterEqual(len(result.html_or_text or ""), MIN_PLAUSIBLE_SOLD_BODY_CHARS)
        self.assertTrue((result.sold_state or {}).get("SOLD_STATE_VERIFIED"))

    def test_capture_failure_retains_last_good_and_no_fresh(self) -> None:
        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [],
            attach_and_read=lambda c: {"ok": False},
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertTrue(result.x11_sold_state_verified)
        outcome = classify_exception_outcome(
            Exception("POST_SOLD_CAPTURE_FAILURE"),
            diagnostics={
                "ownedDailyOutcome": POST_SOLD_CAPTURE_FAILURE,
                "reason": "post_sold_capture_failed",
                "markFresh": False,
                "lastGoodRetained": True,
            },
        )
        self.assertEqual(outcome, POST_SOLD_CAPTURE_FAILURE)

    def test_x11_sold_remains_true_after_capture_failure(self) -> None:
        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(SOLD_URL, tid="sold")],
            attach_and_read=lambda c: {"ok": False, "body_text": "", "error": "empty"},
            allow_one_local_retry=False,
        )
        self.assertEqual(result.capture_phase, POST_SOLD_CAPTURE_FAILED)
        self.assertTrue(result.x11_sold_state_verified)

    def test_classify_method_failure_when_outer_html_exists(self) -> None:
        cls = classify_empty_capture_failure(
            {
                "methods": {
                    "locator_inner_text": {"attempted": True, "length": 0},
                    "page_content": {"attempted": True, "length": 0},
                },
                "readiness": {"outerHTMLLength": 9000, "listingNodeCount": 5},
            }
        )
        self.assertEqual(cls, CDP_CAPTURE_METHOD_FAILURE)

    def test_integrity_failure_on_non_sold_body(self) -> None:
        big = ("Buy It Now\nAdd to cart\n" * 100)
        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(SOLD_URL, tid="sold")],
            attach_and_read=lambda c: {
                "ok": True,
                "url": SOLD_URL,
                "title": "Bloodmoon",
                "body_text": big,
            },
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, CDP_INTEGRITY_FAILURE)


if __name__ == "__main__":
    unittest.main()
