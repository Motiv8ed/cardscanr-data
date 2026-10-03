"""Offline tests: post-Sold CDP target binding + capture contract (no live eBay)."""
from __future__ import annotations

import unittest

from cardscanr_market_engine.owned_daily_outcomes import (
    POST_SOLD_CAPTURE_FAILURE,
    classify_exception_outcome,
)
from cardscanr_market_engine.providers.post_sold_capture import (
    CDP_CAPTURE_FAILURE,
    CDP_CONNECT_FAILURE,
    CDP_DOCUMENT_NOT_READY,
    CDP_INTEGRITY_FAILURE,
    CDP_SOLD_READBACK_LOGIC_FAILURE,
    CDP_TARGET_AMBIGUOUS,
    CDP_TARGET_NOT_FOUND,
    CDP_TIMEOUT,
    POST_SOLD_CAPTURE_FAILED,
    POST_SOLD_CAPTURE_READY,
    PageTargetCandidate,
    capture_verified_sold_page,
    select_sold_page_target,
    verify_captured_sold_state,
)


SOLD_URL = (
    "https://www.ebay.com.au/sch/i.html?"
    "_nkw=Ambipom+79+phantasmal+flames+Pokemon&_sacat=0&_from=R40&rt=nc&LH_Sold=1"
)
ORDINARY_URL = (
    "https://www.ebay.com.au/sch/i.html?"
    "_nkw=Ambipom+79+phantasmal+flames+Pokemon&_sacat=0&_from=R40&_trksid=m570.l1313"
)
QUERY = "Ambipom 79 phantasmal flames Pokemon"
SOLD_BODY = "Sold 30 Sep\nA$3.21\nSold 29 Sep\nA$2.99\n"


def _page(url: str, title: str = "Ambipom | eBay", tid: str = "t1", ttype: str = "page") -> dict:
    return {"id": tid, "type": ttype, "url": url, "title": title}


class TestSelectSoldPageTarget(unittest.TestCase):
    def test_01_one_correct_sold_page_target(self) -> None:
        chosen, fail, scored = select_sold_page_target(
            [_page(SOLD_URL, tid="sold")],
            expected_url=SOLD_URL,
            expected_query=QUERY,
        )
        self.assertIsNone(fail)
        assert chosen is not None
        self.assertEqual(chosen.target_id, "sold")
        self.assertIn("lh_sold", chosen.reasons)

    def test_02_correct_plus_about_blank(self) -> None:
        chosen, fail, _ = select_sold_page_target(
            [
                _page("about:blank", title="Untitled", tid="blank"),
                _page(SOLD_URL, tid="sold"),
            ],
            expected_url=SOLD_URL,
            expected_query=QUERY,
        )
        self.assertIsNone(fail)
        assert chosen is not None
        self.assertEqual(chosen.target_id, "sold")

    def test_03_correct_plus_stale_ordinary_search(self) -> None:
        chosen, fail, _ = select_sold_page_target(
            [
                _page(ORDINARY_URL, title="Ambipom for sale | eBay", tid="ordinary"),
                _page(SOLD_URL, tid="sold"),
            ],
            expected_url=SOLD_URL,
            expected_query=QUERY,
        )
        self.assertIsNone(fail)
        assert chosen is not None
        self.assertEqual(chosen.target_id, "sold")

    def test_04_multiple_ebay_one_exact_sold_match(self) -> None:
        chosen, fail, _ = select_sold_page_target(
            [
                _page("https://www.ebay.com.au/", title="eBay Australia", tid="home"),
                _page(ORDINARY_URL, tid="ordinary"),
                _page(SOLD_URL, tid="sold"),
                _page("chrome://extensions/", tid="ext", ttype="page"),
            ],
            expected_url=SOLD_URL,
            expected_query=QUERY,
        )
        self.assertIsNone(fail)
        assert chosen is not None
        self.assertEqual(chosen.target_id, "sold")

    def test_05_ambiguous_sold_targets(self) -> None:
        sold_a = (
            "https://www.ebay.com.au/sch/i.html?"
            "_nkw=Ambipom+79+phantasmal+flames+Pokemon&_sacat=0&LH_Sold=1&rt=nc&_pgn=1"
        )
        sold_b = (
            "https://www.ebay.com.au/sch/i.html?"
            "_nkw=Ambipom+79+phantasmal+flames+Pokemon&_sacat=0&LH_Sold=1&rt=nc&_pgn=2"
        )
        chosen, fail, _ = select_sold_page_target(
            [
                _page(sold_a, tid="sold-a"),
                _page(sold_b, tid="sold-b"),
            ],
            # Expected URL differs from both page variants → equal nkw+lh_sold scores.
            expected_url=(
                "https://www.ebay.com.au/sch/i.html?"
                "_nkw=Ambipom+79+phantasmal+flames+Pokemon&LH_Sold=1"
            ),
            expected_query=QUERY,
        )
        # Two LH_Sold Ambipom tabs near equal score → ambiguous (do not pick first).
        self.assertEqual(fail, CDP_TARGET_AMBIGUOUS)
        self.assertIsNone(chosen)

    def test_06_no_page_target(self) -> None:
        chosen, fail, _ = select_sold_page_target(
            [
                _page("about:blank", tid="b"),
                {"id": "sw", "type": "service_worker", "url": "https://www.ebay.com.au/", "title": ""},
            ],
            expected_url=SOLD_URL,
            expected_query=QUERY,
        )
        self.assertEqual(fail, CDP_TARGET_NOT_FOUND)
        self.assertIsNone(chosen)


class TestCaptureVerifiedSoldPage(unittest.TestCase):
    def test_07_cdp_connection_failure(self) -> None:
        def boom() -> list:
            raise ConnectionError("cdp refused")

        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=boom,
            attach_and_read=lambda c: {"ok": False},
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, CDP_CONNECT_FAILURE)
        self.assertEqual(result.capture_phase, POST_SOLD_CAPTURE_FAILED)
        self.assertTrue(result.x11_sold_state_verified)

    def test_08_first_attach_failure_then_local_retry(self) -> None:
        calls = {"n": 0, "disconnect": 0}

        def list_targets() -> list:
            return [_page(SOLD_URL, tid="sold")]

        def attach(c: PageTargetCandidate) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                return {"ok": False, "error": "Target closed"}
            return {
                "ok": True,
                "url": SOLD_URL,
                "title": "Ambipom | eBay",
                "body_text": SOLD_BODY,
                "capture_method": "cdp_read_only",
                "elapsed_ms": 12,
            }

        def disconnect() -> None:
            calls["disconnect"] += 1

        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=list_targets,
            attach_and_read=attach,
            disconnect=disconnect,
            allow_one_local_retry=True,
        )
        self.assertTrue(result.success)
        self.assertTrue(result.retry_used)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(calls["disconnect"], 1)
        self.assertEqual(result.capture_phase, POST_SOLD_CAPTURE_READY)
        self.assertEqual(result.html_or_text, SOLD_BODY)

    def test_09_capture_timeout(self) -> None:
        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(SOLD_URL, tid="sold")],
            attach_and_read=lambda c: {"ok": False, "error": "Timeout 30000ms exceeded"},
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, CDP_TIMEOUT)

    def test_10_document_capture_failure(self) -> None:
        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(SOLD_URL, tid="sold")],
            attach_and_read=lambda c: {"ok": False, "error": "inner_text failed"},
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure_class, CDP_CAPTURE_FAILURE)

    def test_11_x11_verified_plus_cdp_failure_remains_sold_verified(self) -> None:
        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(ORDINARY_URL, tid="wrong")],
            attach_and_read=lambda c: {"ok": True, "url": ORDINARY_URL, "title": "x", "body_text": "Buy It Now"},
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertTrue(result.x11_sold_state_verified)
        self.assertEqual(result.capture_phase, POST_SOLD_CAPTURE_FAILED)
        # Binding should refuse ordinary tab without LH_Sold.
        self.assertEqual(result.failure_class, CDP_TARGET_NOT_FOUND)

    def test_12_capture_failure_does_not_advance_freshness_diag(self) -> None:
        # Outcome classification must map to POST_SOLD_CAPTURE_FAILURE (no fresh).
        outcome = classify_exception_outcome(
            Exception("POST_SOLD_CAPTURE_FAILURE: local CDP capture failed"),
            diagnostics={
                "ownedDailyOutcome": POST_SOLD_CAPTURE_FAILURE,
                "markFresh": False,
                "lastGoodRetained": True,
                "x11SoldStateVerified": True,
                "reason": "post_sold_capture_failed",
            },
        )
        self.assertEqual(outcome, POST_SOLD_CAPTURE_FAILURE)

    def test_13_capture_failure_retains_last_good_flag(self) -> None:
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
        # Provider raises with lastGoodRetained; contract preserves X11 Sold.
        self.assertEqual(result.capture_phase, POST_SOLD_CAPTURE_FAILED)

    def test_14_capture_failure_does_not_mutate_ownership(self) -> None:
        # Pure function — no ownership side effects by construction.
        before = {"ownershipMutations": 0}
        capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: (_ for _ in ()).throw(RuntimeError("cdp")),
            attach_and_read=lambda c: {"ok": False},
            allow_one_local_retry=False,
        )
        self.assertEqual(before["ownershipMutations"], 0)

    def test_15_successful_capture_hands_document_to_parser(self) -> None:
        docs: list[str] = []

        def attach(c: PageTargetCandidate) -> dict:
            return {
                "ok": True,
                "url": SOLD_URL,
                "title": "Ambipom 79 Phantasmal Flames Pokemon for sale | eBay",
                "body_text": SOLD_BODY + "\nSold listings\n",
                "capture_method": "cdp_read_only",
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
        docs.append(str(result.html_or_text or ""))
        self.assertIn("Sold 30 Sep", docs[0])
        sold = verify_captured_sold_state(
            url=str(result.target_url),
            title=str(result.target_title),
            body_text=str(result.html_or_text),
        )
        self.assertTrue(sold["SOLD_STATE_VERIFIED"])
        self.assertGreaterEqual(sold["soldDateLines"], 2)

    def test_empty_body_is_document_not_ready(self) -> None:
        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(SOLD_URL, tid="sold")],
            attach_and_read=lambda c: {
                "ok": True,
                "url": SOLD_URL,
                "title": "Ambipom",
                "body_text": "   ",
            },
            allow_one_local_retry=False,
        )
        self.assertEqual(result.failure_class, CDP_DOCUMENT_NOT_READY)

    def test_sold_readback_logic_failure_preserves_x11(self) -> None:
        # Bound Sold URL but body lacks sold-date evidence → integrity/sold failure.
        result = capture_verified_sold_page(
            x11_sold_state_verified=True,
            expected_url=SOLD_URL,
            expected_query=QUERY,
            list_targets=lambda: [_page(SOLD_URL, tid="sold")],
            attach_and_read=lambda c: {
                "ok": True,
                "url": SOLD_URL,
                "title": "Ambipom",
                "body_text": "Buy It Now\nAdd to cart\n" * 50,
            },
            allow_one_local_retry=False,
        )
        self.assertFalse(result.success)
        self.assertTrue(result.x11_sold_state_verified)
        self.assertIn(result.failure_class, {CDP_SOLD_READBACK_LOGIC_FAILURE, CDP_INTEGRITY_FAILURE})


class TestAmbipomWrongTargetRegression(unittest.TestCase):
    """Reproduces Ambipom: ebay_pages[-1] would pick ordinary tab over Sold."""

    def test_legacy_last_ebay_page_is_wrong_when_ordinary_last(self) -> None:
        targets = [
            _page(SOLD_URL, tid="sold"),
            _page(ORDINARY_URL, tid="ordinary-last"),
        ]
        # Legacy behavior: last ebay page
        legacy = targets[-1]
        self.assertNotIn("LH_Sold=1", legacy["url"])
        # New binding must pick Sold
        chosen, fail, _ = select_sold_page_target(
            targets, expected_url=SOLD_URL, expected_query=QUERY
        )
        self.assertIsNone(fail)
        assert chosen is not None
        self.assertEqual(chosen.target_id, "sold")


if __name__ == "__main__":
    unittest.main()
