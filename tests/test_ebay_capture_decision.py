"""P2: authoritative decision function and 4096-row policy conformance."""
from __future__ import annotations

import csv
import dataclasses
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cardscanr_market_engine.providers.capture_decision import (
    PageOutcome,
    classify_policy_signals,
    classify_snapshot,
)
from cardscanr_market_engine.providers.capture_evidence import PROTOCOL_VERSION, Confidence, Tristate
from tests.support.ebay_capture_testkit import fact, minimal_valid_snapshot, with_facts

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "docs/ebay_r6_p0/EBAY_R6_P0_20261110/MIXED_SIGNAL_OUTCOME_TABLE.csv"


class TruthTableTests(unittest.TestCase):
    def test_all_4096_policy_rows_match_csv(self) -> None:
        rows = list(csv.DictReader(TABLE.open(encoding="utf-8")))
        self.assertEqual(len(rows), 4096)
        mismatches: list[str] = []
        for row in rows:
            dim_keys = (
                "auth_observed",
                "active_challenge_observed",
                "sorry_observed",
                "capture_incomplete",
                "optional_banner_present",
                "prior_state_present",
                "sold_looking_markers",
                "hidden_script_css_comment_tokens",
                "binding_valid_current",
                "access_403_observed",
                "complete_current_sold_document",
                "verified_current_zero_results",
            )
            signals = {k: row[k] == "True" for k in dim_keys}
            outcome = classify_policy_signals(signals)
            if outcome.value != row["outcome"]:
                mismatches.append(f"{row['row_id']}: {outcome.value}!={row['outcome']}")
        self.assertEqual(mismatches, [], msg="\n".join(mismatches[:20]))


class SecurityInvariantTests(unittest.TestCase):
    def test_auth_with_valid_binding_beats_optional(self) -> None:
        snap = minimal_valid_snapshot(
            **with_facts(
                fact("auth_observed", Tristate.TRUE),
                fact("optional_banner_present", Tristate.TRUE),
                fact("complete_current_sold_document", Tristate.TRUE),
            )
        )
        decision = classify_snapshot(snap)
        self.assertEqual(decision.outcome, PageOutcome.AUTH_REQUIRED)
        self.assertFalse(decision.may_parse_price)

    def test_invalid_envelope_is_ambiguous_not_auth(self) -> None:
        snap = dataclasses.replace(
            minimal_valid_snapshot(**with_facts(fact("auth_observed", Tristate.TRUE))),
            protocol_version="R5-OLD",
        )
        decision = classify_snapshot(snap)
        self.assertEqual(decision.outcome, PageOutcome.AMBIGUOUS_NO_PRICE)

    def test_only_verified_sold_may_parse(self) -> None:
        snap = minimal_valid_snapshot(
            **with_facts(
                fact("complete_current_sold_document", Tristate.TRUE),
            )
        )
        decision = classify_snapshot(snap)
        self.assertEqual(decision.outcome, PageOutcome.VERIFIED_SOLD_RESULTS)
        self.assertTrue(decision.may_parse_price)
        self.assertTrue(decision.may_produce_usable_comp)

    def test_stale_lease_invalidates_binding(self) -> None:
        expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        snap = minimal_valid_snapshot(lease_expiry=expired, **with_facts(fact("complete_current_sold_document", Tristate.TRUE)))
        decision = classify_snapshot(snap)
        self.assertEqual(decision.outcome, PageOutcome.AMBIGUOUS_NO_PRICE)
        self.assertFalse(decision.may_parse_price)

    def test_diagnostic_only_auth_does_not_grant_verified_sold(self) -> None:
        snap = minimal_valid_snapshot(
            **with_facts(
                fact("auth_observed", Tristate.TRUE, assurance=Confidence.DIAGNOSTIC_ONLY),
                fact("complete_current_sold_document", Tristate.TRUE),
            )
        )
        decision = classify_snapshot(snap)
        self.assertNotEqual(decision.outcome, PageOutcome.VERIFIED_SOLD_RESULTS)


class ManifestMetadataTests(unittest.TestCase):
    def test_outcome_table_metadata_counts(self) -> None:
        meta = json.loads(
            (ROOT / "docs/ebay_r6_p0/EBAY_R6_P0_20261110/OUTCOME_TABLE_METADATA.json").read_text(encoding="utf-8")
        )
        self.assertEqual(meta["rows"], 4096)
        self.assertEqual(meta["protocol_version"], PROTOCOL_VERSION)


if __name__ == "__main__":
    unittest.main()
