"""P2: property and targeted mutant tests for capture decision."""
from __future__ import annotations

import copy
import itertools
import unittest

from cardscanr_market_engine.providers.capture_decision import PageOutcome, classify_snapshot
from cardscanr_market_engine.providers.capture_evidence import Confidence, Tristate
from tests.support.ebay_capture_testkit import fact, minimal_valid_snapshot, with_facts

SECURITY_OUTCOMES = frozenset(
    {
        PageOutcome.AUTH_REQUIRED,
        PageOutcome.CHALLENGE_STOP,
        PageOutcome.ACCESS_BLOCKED,
        PageOutcome.AMBIGUOUS_NO_PRICE,
    }
)


class PropertyTests(unittest.TestCase):
    def test_adding_auth_never_creates_verified_sold(self) -> None:
        base = minimal_valid_snapshot(**with_facts(fact("complete_current_sold_document", Tristate.TRUE)))
        for extra in (
            fact("optional_banner_present", Tristate.TRUE),
            fact("sold_looking_markers", Tristate.TRUE),
            fact("prior_navigation_context", Tristate.TRUE),
        ):
            snap = minimal_valid_snapshot(
                **with_facts(
                    fact("complete_current_sold_document", Tristate.TRUE),
                    extra,
                    fact("auth_observed", Tristate.TRUE),
                )
            )
            decision = classify_snapshot(snap)
            self.assertIn(decision.outcome, SECURITY_OUTCOMES)

    def test_optional_and_prior_never_clear_security(self) -> None:
        snap = minimal_valid_snapshot(
            **with_facts(
                fact("auth_observed", Tristate.TRUE),
                fact("optional_banner_present", Tristate.TRUE),
                fact("prior_navigation_context", Tristate.TRUE),
                fact("sold_looking_markers", Tristate.TRUE),
            )
        )
        self.assertEqual(classify_snapshot(snap).outcome, PageOutcome.AUTH_REQUIRED)

    def test_non_pass_never_yields_price_permission(self) -> None:
        cases = [
            with_facts(fact("sorry_observed", Tristate.TRUE)),
            with_facts(fact("capture_incomplete", Tristate.TRUE)),
            with_facts(),
        ]
        for facts in cases:
            decision = classify_snapshot(minimal_valid_snapshot(**facts))
            if decision.outcome != PageOutcome.VERIFIED_SOLD_RESULTS:
                self.assertFalse(decision.may_parse_price)
                self.assertFalse(decision.may_calculate_price)

    def test_property_grid_auth_monotonic(self) -> None:
        count = 0
        dims = (False, True)
        for optional, sold, prior, incomplete, challenge, sorry, zero in itertools.product(dims, repeat=7):
            flags = (
                fact("complete_current_sold_document", Tristate.TRUE),
                fact("optional_banner_present", Tristate.TRUE if optional else Tristate.FALSE),
                fact("sold_looking_markers", Tristate.TRUE if sold else Tristate.FALSE),
                fact("prior_navigation_context", Tristate.TRUE if prior else Tristate.FALSE),
                fact("capture_incomplete", Tristate.TRUE if incomplete else Tristate.FALSE),
                fact("active_challenge_observed", Tristate.TRUE if challenge else Tristate.FALSE),
                fact("sorry_observed", Tristate.TRUE if sorry else Tristate.FALSE),
                fact("verified_current_zero_results", Tristate.TRUE if zero else Tristate.FALSE),
            )
            without_auth = classify_snapshot(minimal_valid_snapshot(**with_facts(*flags)))
            with_auth = classify_snapshot(
                minimal_valid_snapshot(**with_facts(fact("auth_observed", Tristate.TRUE), *flags))
            )
            count += 1
            if without_auth.outcome == PageOutcome.VERIFIED_SOLD_RESULTS:
                self.assertEqual(with_auth.outcome, PageOutcome.AUTH_REQUIRED)
        self.assertGreaterEqual(count, 125)


class MutantTests(unittest.TestCase):
    def test_caller_optional_bypass_mutant_still_auth(self) -> None:
        snap = minimal_valid_snapshot(
            **with_facts(
                fact("auth_observed", Tristate.TRUE),
                fact("optional_banner_present", Tristate.TRUE),
            )
        )
        mutant = copy.copy(snap)
        decision = classify_snapshot(mutant)
        self.assertEqual(decision.outcome, PageOutcome.AUTH_REQUIRED)

    def test_empty_body_downgrade_mutant_incomplete(self) -> None:
        base = minimal_valid_snapshot()
        snap = minimal_valid_snapshot(
            visible_text={
                "value": "",
                "origin": "CURRENT_CAPTURE",
                "representation": "clipboard_text",
                "assurance": Confidence.UNSUPPORTED.value,
                "evidence_id": "clip:empty",
                "observed_at": base.completed_at.isoformat(),
            }
        )
        decision = classify_snapshot(snap)
        self.assertNotEqual(decision.outcome, PageOutcome.VERIFIED_SOLD_RESULTS)

    def test_permission_flag_only_bypass_rejected(self) -> None:
        snap = minimal_valid_snapshot(**with_facts(fact("capture_incomplete", Tristate.TRUE)))
        decision = classify_snapshot(snap)
        self.assertFalse(decision.may_parse_price)
        self.assertEqual(decision.outcome, PageOutcome.INCOMPLETE_TRANSIENT)


if __name__ == "__main__":
    unittest.main()
