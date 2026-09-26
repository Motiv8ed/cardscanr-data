"""Regression tests for international evidence sufficiency gate."""
from __future__ import annotations

import unittest

from cardscanr_market_engine.international.evidence_gate import (
    UNAVAILABLE_HIGH_DISPERSION,
    UNAVAILABLE_INSUFFICIENT,
    UNAVAILABLE_LOW_CONFIDENCE,
    evaluate_international_evidence_gate,
)


class InternationalEvidenceGateTests(unittest.TestCase):
    def test_zero_comps_unavailable(self) -> None:
        decision = evaluate_international_evidence_gate(
            included_count=0,
            confidence="low",
            recommended_price=None,
        )
        self.assertEqual(decision.outcome, "unavailable")
        self.assertFalse(decision.allows_user_facing_price)
        self.assertEqual(decision.reason, UNAVAILABLE_INSUFFICIENT)

    def test_one_comp_low_confidence_unavailable(self) -> None:
        decision = evaluate_international_evidence_gate(
            included_count=1,
            confidence="low",
            recommended_price=45.84,
            price_reliability="single_comp_low_confidence",
        )
        self.assertEqual(decision.outcome, "unavailable")
        self.assertFalse(decision.allows_user_facing_price)
        self.assertEqual(decision.reason, UNAVAILABLE_LOW_CONFIDENCE)

    def test_two_comps_still_unavailable(self) -> None:
        decision = evaluate_international_evidence_gate(
            included_count=2,
            confidence="low",
            recommended_price=20.0,
            price_reliability="reliable",
        )
        self.assertEqual(decision.outcome, "unavailable")
        self.assertFalse(decision.allows_user_facing_price)
        self.assertEqual(decision.reason, UNAVAILABLE_LOW_CONFIDENCE)

    def test_multiple_medium_comps_use_range(self) -> None:
        decision = evaluate_international_evidence_gate(
            included_count=4,
            confidence="medium",
            recommended_price=26.4,
            price_spread_ratio=1.4,
            price_reliability="reliable",
        )
        self.assertEqual(decision.outcome, "range_estimate")
        self.assertTrue(decision.allows_user_facing_price)
        self.assertTrue(decision.show_as_range)

    def test_high_confidence_many_comps_numeric(self) -> None:
        decision = evaluate_international_evidence_gate(
            included_count=9,
            confidence="high",
            recommended_price=30.0,
            price_spread_ratio=1.5,
            price_reliability="reliable",
        )
        self.assertEqual(decision.outcome, "numeric_estimate")
        self.assertTrue(decision.allows_user_facing_price)
        self.assertFalse(decision.show_as_range)

    def test_high_dispersion_unavailable_even_with_many_comps(self) -> None:
        decision = evaluate_international_evidence_gate(
            included_count=9,
            confidence="high",
            recommended_price=40.0,
            price_spread_ratio=6.2,
            price_reliability="reliable",
        )
        self.assertEqual(decision.outcome, "unavailable")
        self.assertFalse(decision.allows_user_facing_price)
        self.assertEqual(decision.reason, UNAVAILABLE_HIGH_DISPERSION)


if __name__ == "__main__":
    unittest.main()
