"""P3: visibility and UNKNOWN observation handling."""
from __future__ import annotations

import unittest

from cardscanr_market_engine.providers.capture_decision import PageOutcome, classify_snapshot
from cardscanr_market_engine.providers.capture_evidence import (
    CaptureRepresentation,
    Confidence,
    Tristate,
    derive_policy_signals,
)
from tests.support.ebay_capture_testkit import fact, minimal_valid_snapshot, with_facts


class VisibilityTests(unittest.TestCase):
    def test_html_source_visible_text_not_complete_sold(self) -> None:
        snap = minimal_valid_snapshot(
            visible_text={
                "value": "<style>.auth{}</style>Sign in",
                "origin": "RAW_SOURCE",
                "representation": CaptureRepresentation.HTML_SOURCE.value,
                "assurance": Confidence.DIAGNOSTIC_ONLY.value,
                "evidence_id": "html:source",
                "observed_at": minimal_valid_snapshot().completed_at.isoformat(),
            },
            **with_facts(fact("complete_current_sold_document", Tristate.TRUE)),
        )
        signals = derive_policy_signals(snap)
        self.assertTrue(signals["capture_incomplete"] or not signals["complete_current_sold_document"])
        decision = classify_snapshot(snap)
        self.assertNotEqual(decision.outcome, PageOutcome.VERIFIED_SOLD_RESULTS)

    def test_hidden_script_tokens_do_not_create_auth(self) -> None:
        snap = minimal_valid_snapshot(
            **with_facts(
                fact("hidden_script_css_comment_tokens", Tristate.TRUE),
                fact("auth_observed", Tristate.FALSE),
            )
        )
        decision = classify_snapshot(snap)
        self.assertNotEqual(decision.outcome, PageOutcome.AUTH_REQUIRED)


if __name__ == "__main__":
    unittest.main()
