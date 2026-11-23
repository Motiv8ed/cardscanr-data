"""Regression: eBay challenge incidents retain actionable, privacy-safe facts.

All tests are offline and use isolated temporary incident/ops JSON files.
They neither launch Chrome nor contact eBay or production Supabase.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cardscanr_market_engine.challenge_incident_evidence import (
    EVIDENCE_SCHEMA,
    build_challenge_incident_evidence,
)
from cardscanr_market_engine.control_plane_incidents import get_incident
from cardscanr_market_engine.marketplace_ops_state import maybe_record_failure_cooldown


_GATE_URL = ("https://www.ebay.com.au/splashui/challenge"
             "?referrer=https%3A%2F%2Fwww.ebay.com.au&token=do-not-store")
_CHALLENGE_DIAG = {
    "providerOutcome": "inter_card_challenge_target",
    "operationalStatus": "CHALLENGE_REQUIRED",
    "resultCode": "INTER_CARD_CHALLENGE_TARGET",
    "urlBeforeSearch": _GATE_URL,
    "titleBeforeSearch": "Verify yourself to continue",
    "targetPolicy": {
        "reasonCodes": ["INTER_CARD_CHALLENGE_TARGET"],
        "classified": [
            {
                "type": "page", "topLevel": True, "blocking": True,
                "originClass": "ebay_challenge",
                "reason": "INTER_CARD_CHALLENGE_TARGET",
                "url": _GATE_URL,
                "title": "Verify yourself to continue",
                "targetId": "potentially-sensitive-target-id",
            },
            {
                "type": "service_worker", "topLevel": False,
                "url": "https://www.ebay.com.au/service-worker.js",
                "title": "",
            },
        ],
    },
}


class ChallengeIncidentEvidenceTests(unittest.TestCase):
    def test_challenge_evidence_retains_exact_safe_classification(self) -> None:
        evidence = build_challenge_incident_evidence(
            _CHALLENGE_DIAG, category="CHALLENGE_REQUIRED"
        )
        self.assertEqual(evidence["schema"], EVIDENCE_SCHEMA)
        self.assertEqual(evidence["evidenceQuality"], "GATE_PAGE_OBSERVED")
        self.assertEqual(evidence["reasonCodes"], ["INTER_CARD_CHALLENGE_TARGET"])
        self.assertEqual(evidence["policyClassifiedTargetCount"], 2)
        target = evidence["targetEvidence"][0]
        self.assertTrue(target["directGateEvidence"])
        self.assertEqual(target["hostClass"], "EBAY_MARKETPLACE")
        self.assertEqual(target["pathClass"], "SPLASHUI_CHALLENGE")
        self.assertEqual(target["gateTitle"], "verify yourself to continue")
        self.assertFalse(evidence["automaticResumeAllowed"])
        serialized = json.dumps(evidence)
        for secret in ("do-not-store", "referrer=", "target-id", "_nkw="):
            self.assertNotIn(secret, serialized)
        self.assertNotIn('"url":', serialized)
        self.assertNotIn('"title":', serialized)

    def test_genuine_challenge_in_nested_desktop_diagnostics(self) -> None:
        nested = {"desktopNav": {"search": {
            "resultCode": "INTER_CARD_CHALLENGE_TARGET",
            "url": "https://www.ebay.com.au/splashui/challenge?secret=mytoken",
            "title": "Security measure",
            "targetPolicy": _CHALLENGE_DIAG["targetPolicy"],
        }}}
        evidence = build_challenge_incident_evidence(
            nested, category="CHALLENGE_REQUIRED"
        )
        self.assertEqual(evidence["evidenceQuality"], "GATE_PAGE_OBSERVED")
        self.assertIn("INTER_CARD_CHALLENGE_TARGET", evidence["reasonCodes"])
        self.assertEqual(evidence["navigation"]["gateTitle"], "security measure")
        self.assertNotIn("mytoken", json.dumps(evidence))

    def test_normal_results_with_challenge_code_is_unverified_not_auto_cleared(self) -> None:
        evidence = build_challenge_incident_evidence(
            {
                "resultCode": "INTER_CARD_CHALLENGE_TARGET",
                "url": "https://www.ebay.com.au/sch/i.html?_nkw=Cubone+104",
                "title": "Cubone results for 104",
                "targetPolicy": {
                    "reasonCodes": ["INTER_CARD_CHALLENGE_TARGET"],
                    "classified": [{
                        "type": "page", "topLevel": True,
                        "originClass": "ebay_marketplace", "blocking": True,
                        "reason": "INTER_CARD_CHALLENGE_TARGET",
                        "url": "https://www.ebay.com.au/sch/i.html?_nkw=Cubone+104",
                        "title": "Cubone results for 104",
                    }],
                },
            },
            category="CHALLENGE_REQUIRED",
        )
        self.assertEqual(evidence["evidenceQuality"], "CODE_ONLY_UNCORROBORATED")
        self.assertEqual(evidence["navigation"]["pathClass"], "SEARCH_RESULTS")
        self.assertFalse(evidence["automaticResumeAllowed"])
        self.assertNotIn("Cubone", json.dumps(evidence))

    def test_fake_challenge_url_not_accepted_as_ebay_evidence(self) -> None:
        evidence = build_challenge_incident_evidence(
            {
                "resultCode": "INTER_CARD_CHALLENGE_TARGET",
                "url": "https://attacker.invalid/challenge?token=secret",
                "title": "Unrelated listing",
            },
            category="CHALLENGE_REQUIRED",
        )
        self.assertEqual(evidence["navigation"]["hostClass"], "OTHER")
        self.assertFalse(evidence["navigation"]["gateUrlObserved"])
        self.assertEqual(evidence["evidenceQuality"], "CODE_ONLY_UNCORROBORATED")
        self.assertNotIn("attacker.invalid", json.dumps(evidence))
        self.assertNotIn("secret", json.dumps(evidence))

    def test_title_and_query_from_listing_never_stored(self) -> None:
        evidence = build_challenge_incident_evidence({
            "title": "Secret-private-card-name",
            "url": "https://www.ebay.com.au/sch/i.html?_nkw=Secret+private",
            "resultCode": "INTER_CARD_CHALLENGE_TARGET",
        }, category="CHALLENGE_REQUIRED")
        payload = json.dumps(evidence)
        self.assertNotIn("Secret-private-card-name", payload)
        self.assertNotIn("_nkw", payload)
        self.assertIn("titleSha256", payload)

    def test_pardon_title_only_stores_canonical_whitelisted_text(self) -> None:
        evidence = build_challenge_incident_evidence({
            "url": "https://www.ebay.com.au/sch/i.html",
            "title": "Pardon our interruption incident private-user@example.com",
        }, category="CHALLENGE_REQUIRED")
        self.assertEqual(evidence["navigation"]["gateTitle"], "pardon our interruption")
        self.assertNotIn("private-user", json.dumps(evidence))

    def test_missing_diagnostics_never_implies_healthy(self) -> None:
        evidence = build_challenge_incident_evidence(
            None, category="CHALLENGE_REQUIRED"
        )
        self.assertEqual(evidence["evidenceQuality"], "NO_DIRECT_PAGE_EVIDENCE")
        self.assertFalse(evidence["automaticResumeAllowed"])

    def test_real_cooldown_path_persists_evidence_to_same_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            ops = directory / "marketplace_ops_state.json"
            incidents = directory / "control_plane_incidents.json"
            time = datetime(2026, 11, 22, 4, 14, 0, tzinfo=timezone.utc)
            state = maybe_record_failure_cooldown(
                market="AU",
                message="Desktop sold navigation failed: INTER_CARD_CHALLENGE_TARGET",
                diagnostics=_CHALLENGE_DIAG,
                now=time,
                path=ops,
                incidents_path=incidents,
                source_attempt_id="attempt-test-failure",
            )
            self.assertIsNotNone(state)
            assert state is not None
            incident = get_incident(str(state.incident_id), path=incidents)
            self.assertIsInstance(incident, dict)
            assert incident is not None
            self.assertEqual(incident["status"], "active")
            self.assertEqual(incident["classification"], "CHALLENGE_REQUIRED")
            self.assertEqual(incident["sourceAttemptId"], "attempt-test-failure")
            self.assertEqual(
                incident["evidence"]["evidenceQuality"], "GATE_PAGE_OBSERVED"
            )
            self.assertFalse(incident["evidence"]["automaticResumeAllowed"])
            serialized = json.dumps(incident["evidence"])
            self.assertNotIn("do-not-store", serialized)

    def test_code_only_diagnostic_still_creates_hard_stop_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = maybe_record_failure_cooldown(
                market="AU",
                message="Desktop sold navigation failed: INTER_CARD_CHALLENGE_TARGET",
                diagnostics={
                    "providerOutcome": "inter_card_challenge_target",
                    "resultCode": "INTER_CARD_CHALLENGE_TARGET",
                    "url": "https://www.ebay.com.au/sch/i.html?_nkw=Bulbasaur",
                },
                now=datetime(2026, 11, 22, 4, 14, tzinfo=timezone.utc),
                path=directory / "marketplace_ops_state.json",
                incidents_path=directory / "control_plane_incidents.json",
            )
            self.assertIsNotNone(state)
            assert state is not None
            incident = get_incident(
                str(state.incident_id),
                path=directory / "control_plane_incidents.json",
            )
            assert incident is not None
            self.assertEqual(
                incident["evidence"]["evidenceQuality"], "CODE_ONLY_UNCORROBORATED"
            )
            self.assertEqual(incident["classification"], "CHALLENGE_REQUIRED")

    def test_gate_reflection_does_not_create_duplicate_incident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = maybe_record_failure_cooldown(
                market="AU",
                message="marketplace challenge deferred",
                diagnostics={
                    "providerOutcome": "marketplace_challenge_deferred",
                    "resultCode": "INTER_CARD_CHALLENGE_TARGET",
                },
                path=directory / "marketplace_ops_state.json",
                incidents_path=directory / "control_plane_incidents.json",
            )
            self.assertIsNone(state)
            self.assertFalse((directory / "control_plane_incidents.json").exists())

    def test_gate_target_after_first_eight_is_not_lost(self) -> None:
        normal_targets = [{
            "type": "service_worker", "topLevel": False,
            "originClass": "auxiliary", "blocking": False,
            "url": f"https://example.invalid/assets/worker-{i}.js",
            "title": "",
        } for i in range(9)]
        target = {
            "type": "page", "topLevel": True, "originClass": "ebay_challenge",
            "blocking": True, "reason": "INTER_CARD_CHALLENGE_TARGET",
            "url": "https://www.ebay.com.au/splashui/challenge",
            "title": "Verify yourself to continue",
        }
        evidence = build_challenge_incident_evidence({
            "resultCode": "INTER_CARD_CHALLENGE_TARGET",
            "targetPolicy": {"reasonCodes": ["INTER_CARD_CHALLENGE_TARGET"],
                             "classified": normal_targets + [target]},
        }, category="CHALLENGE_REQUIRED")
        self.assertEqual(evidence["policyClassifiedTargetCount"], 10)
        self.assertEqual(evidence["evidenceQuality"], "GATE_PAGE_OBSERVED")
        self.assertLessEqual(len(evidence["targetEvidence"]), 8)
        self.assertTrue(evidence["targetEvidence"][0]["directGateEvidence"])
        self.assertFalse(evidence["automaticResumeAllowed"])

    def test_code_only_with_ten_targets_remains_fail_closed(self) -> None:
        normal_targets = [{
            "type": "service_worker", "topLevel": False,
            "originClass": "auxiliary", "blocking": False,
            "url": f"https://example.invalid/assets/worker-{i}.js",
            "title": "",
        } for i in range(9)]
        normal_targets.append({
            "type": "page", "topLevel": True, "originClass": "ebay_marketplace",
            "blocking": True, "reason": "INTER_CARD_CHALLENGE_TARGET",
            "url": "https://www.ebay.com.au/sch/i.html?_nkw=private-card",
            "title": "Normal sold results",
        })
        evidence = build_challenge_incident_evidence({
            "resultCode": "INTER_CARD_CHALLENGE_TARGET",
            "targetPolicy": {"reasonCodes": ["INTER_CARD_CHALLENGE_TARGET"],
                             "classified": normal_targets},
        }, category="CHALLENGE_REQUIRED")
        self.assertEqual(evidence["policyClassifiedTargetCount"], 10)
        self.assertEqual(evidence["evidenceQuality"], "CODE_ONLY_UNCORROBORATED")
        self.assertFalse(evidence["targetEvidence"][0]["directGateEvidence"])
        self.assertFalse(evidence["automaticResumeAllowed"])
        self.assertNotIn("private-card", json.dumps(evidence))



if __name__ == "__main__":
    unittest.main()
