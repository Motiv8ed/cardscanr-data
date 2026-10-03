#!/usr/bin/env python3
"""Regression tests for control-plane incident identity + CAS reconciliation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.control_plane_incidents import (
    clear_marketplace_cooldown_if_matches,
    invalidate_control_plane_incident,
    register_incident,
    INCIDENT_TYPE_CHALLENGE,
    INCIDENT_TYPE_TRANSIENT_EBAY,
)
from cardscanr_market_engine.ebay_availability import (
    clear_challenge_for_manual_restore,
    get_availability,
    load_availability,
    record_challenge,
    record_sorry,
    save_availability,
)
from cardscanr_market_engine.marketplace_ops_state import (
    get_active_cooldown,
    list_active_cooldowns,
    load_ops_state,
    maybe_record_failure_cooldown,
    record_marketplace_cooldown,
    save_ops_state,
)
from cardscanr_market_engine.owned_daily_outcomes import (
    CHALLENGE_REQUIRED,
    PRE_FLIGHT_CONTROL_PLANE_BLOCKED,
    classify_exception_outcome,
)
from tools.linux_x11_single_probe_ready import _classify_verdict


class ControlPlaneIncidentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ops = self.root / "ops.json"
        self.avail = self.root / "avail.json"
        self.inc = self.root / "incidents.json"
        os.environ["MARKET_OPS_STATE_PATH"] = str(self.ops)
        os.environ["EBAY_AVAILABILITY_STATE_PATH"] = str(self.avail)
        os.environ["CONTROL_PLANE_INCIDENTS_PATH"] = str(self.inc)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("MARKET_OPS_STATE_PATH", None))
        self.addCleanup(lambda: os.environ.pop("EBAY_AVAILABILITY_STATE_PATH", None))
        self.addCleanup(lambda: os.environ.pop("CONTROL_PLANE_INCIDENTS_PATH", None))

    def _seed_challenge_incident(
        self,
        *,
        market: str = "AU",
        recorded_at: datetime | None = None,
        message: str = "challenge detected live",
    ) -> dict:
        now = recorded_at or datetime(2026, 10, 1, 2, 27, 33, tzinfo=timezone.utc)
        cooldown = record_marketplace_cooldown(
            market,
            reason="CHALLENGE_REQUIRED",
            message=message,
            hours=12,
            now=now,
            path=self.ops,
            incident_id="will_replace",
        )
        incident = register_incident(
            market=market,
            incident_type=INCIDENT_TYPE_CHALLENGE,
            classification="CHALLENGE_REQUIRED",
            message=message,
            recorded_at=now,
            incident_id=None,
            derived={
                "marketplaceCooldownReason": "CHALLENGE_REQUIRED",
                "marketplaceCooldownRecordedAt": cooldown.to_dict()["recordedAt"],
                "marketplaceCooldownUntil": cooldown.to_dict()["until"],
                "availabilityState": "CHALLENGE_REQUIRED",
                "availabilityFailureReference": message,
            },
            path=self.inc,
        )
        # Bind cooldown + availability to incident id.
        record_marketplace_cooldown(
            market,
            reason="CHALLENGE_REQUIRED",
            message=message,
            hours=12,
            now=now,
            path=self.ops,
            incident_id=incident["incidentId"],
        )
        # Patch derived with real incident id.
        from cardscanr_market_engine.control_plane_incidents import _load_ledger, _save_ledger

        ledger = _load_ledger(path=self.inc)
        row = dict(ledger["incidents"][incident["incidentId"]])
        derived = dict(row.get("derived") or {})
        derived["marketplaceCooldownIncidentId"] = incident["incidentId"]
        row["derived"] = derived
        ledger["incidents"][incident["incidentId"]] = row
        _save_ledger(ledger, path=self.inc, force=True)
        record_challenge(
            now=now,
            path=self.avail,
            reference=message,
            market=market,
            incident_id=incident["incidentId"],
        )
        return incident

    def test_a_false_incident_invalidation_reconciles_derived_state(self) -> None:
        frozen = datetime(2026, 10, 1, 3, 0, 0, tzinfo=timezone.utc)
        incident = self._seed_challenge_incident()
        self.assertIsNotNone(get_active_cooldown("AU", path=self.ops, now=frozen))
        self.assertEqual(get_availability(now=frozen, path=self.avail).state, "CHALLENGE_REQUIRED")
        result = invalidate_control_plane_incident(
            incident["incidentId"],
            invalidation_reason="proven false offline",
            incidents_file=self.inc,
            marketplace_ops_path=self.ops,
            availability_path=self.avail,
            now=frozen,
        )
        self.assertTrue(result["ok"])
        self.assertIsNone(get_active_cooldown("AU", path=self.ops, now=frozen))
        self.assertEqual(get_availability(now=frozen, path=self.avail).state, "PROBE_REQUIRED")

    def test_b_newer_real_challenge_preserved(self) -> None:
        false_x = self._seed_challenge_incident(
            recorded_at=datetime(2026, 10, 1, 2, 27, 33, tzinfo=timezone.utc),
            message="false captcha",
        )
        # Newer genuine challenge Y.
        y_at = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)
        y = self._seed_challenge_incident(recorded_at=y_at, message="real visible captcha")
        result = invalidate_control_plane_incident(
            false_x["incidentId"],
            invalidation_reason="X proven false",
            incidents_file=self.inc,
            marketplace_ops_path=self.ops,
            availability_path=self.avail,
        )
        self.assertTrue(result["ok"])
        # Y's cooldown remains (same market overwritten by Y seed — active cooldown is Y).
        active = get_active_cooldown("AU", now=y_at + timedelta(hours=1), path=self.ops)
        self.assertIsNotNone(active)
        self.assertEqual(active.incident_id, y["incidentId"])
        self.assertEqual(get_availability(path=self.avail).state, "CHALLENGE_REQUIRED")
        self.assertEqual(
            get_availability(path=self.avail).last_challenge_incident_id,
            y["incidentId"],
        )

    def test_c_newer_403_preserved(self) -> None:
        false_x = self._seed_challenge_incident(message="false captcha")
        # Clear challenge availability then open SORRY cooldown as newer Y.
        clear_challenge_for_manual_restore(path=self.avail)
        record_sorry(
            now=datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc),
            path=self.avail,
            reference="ebay sorry error page",
            market="AU",
        )
        # Independent marketplace TRANSIENT_EBAY cooldown.
        record_marketplace_cooldown(
            "AU",
            reason="TRANSIENT_EBAY",
            message="ebay sorry",
            minutes=15,
            now=datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc),
            path=self.ops,
            incident_id="cpi_sorry_y",
        )
        register_incident(
            market="AU",
            incident_type=INCIDENT_TYPE_TRANSIENT_EBAY,
            classification="TRANSIENT_EBAY",
            message="ebay sorry",
            recorded_at=datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc),
            incident_id="cpi_sorry_y",
            derived={
                "marketplaceCooldownReason": "TRANSIENT_EBAY",
                "marketplaceCooldownRecordedAt": "2026-10-01T09:00:00Z",
                "marketplaceCooldownUntil": "2026-10-01T09:15:00Z",
                "marketplaceCooldownIncidentId": "cpi_sorry_y",
            },
            path=self.inc,
        )
        result = invalidate_control_plane_incident(
            false_x["incidentId"],
            invalidation_reason="X proven false",
            incidents_file=self.inc,
            marketplace_ops_path=self.ops,
            availability_path=self.avail,
            now=datetime(2026, 10, 1, 9, 5, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(result["ok"])
        active = get_active_cooldown(
            "AU",
            now=datetime(2026, 10, 1, 9, 5, 0, tzinfo=timezone.utc),
            path=self.ops,
        )
        self.assertIsNotNone(active)
        self.assertEqual(active.reason, "TRANSIENT_EBAY")
        self.assertEqual(
            get_availability(
                now=datetime(2026, 10, 1, 9, 5, 0, tzinfo=timezone.utc),
                path=self.avail,
            ).state,
            "COOLDOWN",
        )

    def test_d_idempotency(self) -> None:
        incident = self._seed_challenge_incident()
        r1 = invalidate_control_plane_incident(
            incident["incidentId"],
            invalidation_reason="false",
            incidents_file=self.inc,
            marketplace_ops_path=self.ops,
            availability_path=self.avail,
        )
        r2 = invalidate_control_plane_incident(
            incident["incidentId"],
            invalidation_reason="false again",
            incidents_file=self.inc,
            marketplace_ops_path=self.ops,
            availability_path=self.avail,
        )
        self.assertTrue(r1["ok"])
        self.assertTrue(r2["ok"])
        self.assertTrue(r2.get("idempotent"))
        self.assertIsNone(get_active_cooldown("AU", path=self.ops))

    def test_e_concurrent_update_cas(self) -> None:
        now = datetime(2026, 10, 1, 2, 27, 33, tzinfo=timezone.utc)
        record_marketplace_cooldown(
            "AU",
            reason="CHALLENGE_REQUIRED",
            message="false",
            hours=12,
            now=now,
            path=self.ops,
            incident_id="cpi_x",
        )
        # Y arrives (newer recordedAt / different message).
        record_marketplace_cooldown(
            "AU",
            reason="CHALLENGE_REQUIRED",
            message="real newer challenge",
            hours=12,
            now=datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc),
            path=self.ops,
            incident_id="cpi_y",
        )
        cas = clear_marketplace_cooldown_if_matches(
            "AU",
            expected_reason="CHALLENGE_REQUIRED",
            expected_recorded_at="2026-10-01T02:27:33Z",
            expected_incident_id="cpi_x",
            path=self.ops,
        )
        self.assertFalse(cas["cleared"])
        active = get_active_cooldown(
            "AU",
            now=datetime(2026, 10, 1, 10, 30, 0, tzinfo=timezone.utc),
            path=self.ops,
        )
        self.assertIsNotNone(active)
        self.assertEqual(active.incident_id, "cpi_y")

    def test_f_market_isolation(self) -> None:
        frozen = datetime(2026, 10, 1, 3, 30, 0, tzinfo=timezone.utc)
        incident = self._seed_challenge_incident(market="AU")
        record_marketplace_cooldown(
            "US",
            reason="CHALLENGE_REQUIRED",
            message="us challenge",
            hours=12,
            now=datetime(2026, 10, 1, 3, 0, 0, tzinfo=timezone.utc),
            path=self.ops,
            incident_id="cpi_us",
        )
        record_marketplace_cooldown(
            "GB",
            reason="AUTH_REQUIRED",
            message="gb auth",
            hours=6,
            now=datetime(2026, 10, 1, 3, 0, 0, tzinfo=timezone.utc),
            path=self.ops,
            incident_id="cpi_gb",
        )
        invalidate_control_plane_incident(
            incident["incidentId"],
            invalidation_reason="AU false",
            incidents_file=self.inc,
            marketplace_ops_path=self.ops,
            availability_path=self.avail,
            now=frozen,
        )
        self.assertIsNone(get_active_cooldown("AU", path=self.ops, now=frozen))
        self.assertIsNotNone(get_active_cooldown("US", path=self.ops, now=frozen))
        self.assertIsNotNone(get_active_cooldown("GB", path=self.ops, now=frozen))

    def test_g_zero_request_preflight_not_challenge(self) -> None:
        outcome = classify_exception_outcome(
            "CHALLENGE_REQUIRED: marketplace temporarily deferred until 2026-10-01T14:27:33Z",
            diagnostics={
                "providerOutcome": "marketplace_ops_cooldown",
                "operationalStatus": "CHALLENGE_REQUIRED",
                "cooldownReason": "CHALLENGE_REQUIRED",
            },
        )
        self.assertEqual(outcome, PRE_FLIGHT_CONTROL_PLANE_BLOCKED)
        verdict = _classify_verdict(
            outcome=outcome,
            err="CHALLENGE_REQUIRED: marketplace temporarily deferred until 2026-10-01T14:27:33Z",
            url="",
            search=None,
            sold=None,
            http_status=None,
            fifth_probe=True,
        )
        self.assertEqual(verdict, "PRE_FLIGHT_CONTROL_PLANE_BLOCKED")
        self.assertNotEqual(verdict, "MANUAL_CHALLENGE_REQUIRED")

    def test_h_verdict_semantics_current_vs_historical(self) -> None:
        historical = _classify_verdict(
            outcome=PRE_FLIGHT_CONTROL_PLANE_BLOCKED,
            err="marketplace temporarily deferred",
            url="",
            search=None,
            sold=None,
            http_status=None,
            fifth_probe=True,
        )
        self.assertEqual(historical, "PRE_FLIGHT_CONTROL_PLANE_BLOCKED")
        current = _classify_verdict(
            outcome=CHALLENGE_REQUIRED,
            err="eBay returned a verification challenge; captcha bypass is not attempted",
            url="https://www.ebay.com.au/splashui/challenge",
            search={"challenge": True},
            sold=None,
            http_status=None,
            fifth_probe=True,
        )
        self.assertEqual(current, "MANUAL_CHALLENGE_REQUIRED")

    def test_i_environment_deferral_independent(self) -> None:
        # No JSON cooldown, but env deferral still blocks classification as preflight.
        self.assertIsNone(get_active_cooldown("AU", path=self.ops))
        outcome = classify_exception_outcome(
            "MARKETPLACE_CHALLENGE_REQUIRED: marketplace challenge unresolved",
            diagnostics={
                "providerOutcome": "marketplace_challenge_deferred",
                "operationalStatus": "MARKETPLACE_CHALLENGE_REQUIRED",
            },
        )
        self.assertEqual(outcome, PRE_FLIGHT_CONTROL_PLANE_BLOCKED)
        # Cleanup of unrelated incident must not erase env config (env is external).
        env_before = os.environ.get("MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS")
        os.environ["MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS"] = "AU"
        try:
            incident = self._seed_challenge_incident()
            invalidate_control_plane_incident(
                incident["incidentId"],
                invalidation_reason="false",
                incidents_file=self.inc,
                marketplace_ops_path=self.ops,
                availability_path=self.avail,
            )
            self.assertEqual(os.environ.get("MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS"), "AU")
        finally:
            if env_before is None:
                os.environ.pop("MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS", None)
            else:
                os.environ["MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS"] = env_before

    def test_j_source_precedence_untouched_by_cleanup(self) -> None:
        # Cleanup only touches ops/availability/incidents files — not price payloads.
        price_payload = {"display_price_source": "verified_local", "current_market_price": 1.99}
        price_path = self.root / "price.json"
        price_path.write_text(json.dumps(price_payload), encoding="utf-8")
        incident = self._seed_challenge_incident()
        invalidate_control_plane_incident(
            incident["incidentId"],
            invalidation_reason="false",
            incidents_file=self.inc,
            marketplace_ops_path=self.ops,
            availability_path=self.avail,
        )
        after = json.loads(price_path.read_text(encoding="utf-8"))
        self.assertEqual(after, price_payload)

    def test_k_last_good_freshness_untouched(self) -> None:
        last_good = {
            "current_market_price": 1.07,
            "last_updated_at": "2026-09-29T04:30:31.279103+00:00",
            "refresh_status": "completed",
        }
        path = self.root / "last_good.json"
        path.write_text(json.dumps(last_good), encoding="utf-8")
        incident = self._seed_challenge_incident()
        invalidate_control_plane_incident(
            incident["incidentId"],
            invalidation_reason="false",
            incidents_file=self.inc,
            marketplace_ops_path=self.ops,
            availability_path=self.avail,
        )
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), last_good)

    def test_l_ownership_safety(self) -> None:
        ownership = {"mutations": 0, "owners": ["andrew"]}
        path = self.root / "ownership.json"
        path.write_text(json.dumps(ownership), encoding="utf-8")
        incident = self._seed_challenge_incident()
        invalidate_control_plane_incident(
            incident["incidentId"],
            invalidation_reason="false",
            incidents_file=self.inc,
            marketplace_ops_path=self.ops,
            availability_path=self.avail,
        )
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(after["mutations"], 0)
        self.assertEqual(after, ownership)

    def test_m_other_marketplace_state_survives(self) -> None:
        frozen = datetime(2026, 10, 1, 4, 5, 0, tzinfo=timezone.utc)
        incident = self._seed_challenge_incident(market="AU")
        record_marketplace_cooldown(
            "CA",
            reason="TRANSIENT_EBAY",
            message="ca sorry",
            minutes=15,
            now=datetime(2026, 10, 1, 4, 0, 0, tzinfo=timezone.utc),
            path=self.ops,
            incident_id="cpi_ca",
        )
        before = load_ops_state(path=self.ops)
        self.assertIn("CA", before["markets"])
        invalidate_control_plane_incident(
            incident["incidentId"],
            invalidation_reason="AU false",
            incidents_file=self.inc,
            marketplace_ops_path=self.ops,
            availability_path=self.avail,
            now=frozen,
        )
        after = load_ops_state(path=self.ops)
        self.assertNotIn("AU", after.get("markets") or {})
        self.assertIn("CA", after.get("markets") or {})
        self.assertEqual(after["markets"]["CA"]["incidentId"], "cpi_ca")

    def test_maybe_record_attaches_incident_id(self) -> None:
        state = maybe_record_failure_cooldown(
            market="AU",
            message="eBay returned a verification challenge; captcha bypass is not attempted",
            diagnostics={"providerOutcome": "challenge_detected"},
            now=datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc),
            path=self.ops,
        )
        self.assertIsNotNone(state)
        assert state is not None
        self.assertTrue(str(state.incident_id or "").startswith("cpi_"))
        from cardscanr_market_engine.control_plane_incidents import get_incident

        self.assertIsNotNone(get_incident(str(state.incident_id), path=self.inc))


if __name__ == "__main__":
    unittest.main()
