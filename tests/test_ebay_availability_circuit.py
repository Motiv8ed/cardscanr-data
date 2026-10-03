"""Deterministic tests for global eBay availability circuit breaker + job leases."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from cardscanr_market_engine.ebay_availability import (
    EBAY_AVAILABILITY_COOLDOWN,
    EBAY_AVAILABILITY_CONFIRMED_HEALTHY,
    EBAY_CHALLENGE_REQUIRED,
    begin_probe,
    browser_work_allowed,
    get_availability,
    load_availability,
    record_challenge,
    record_healthy_browser_check,
    record_sorry,
    save_availability,
    seed_from_observed_sorrys,
)
from cardscanr_market_engine.job_lease import (
    may_steal_running_lease,
    recovery_action_for_running_job,
)
from cardscanr_market_engine.owned_daily_outcomes import (
    TEMPORARY_EBAY_SERVER_FAILURE,
    classify_exception_outcome,
)
from cardscanr_market_engine.providers.errors import ProviderTemporaryError


NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


class EbayAvailabilityCircuitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ebay_availability_state.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_first_sorry_opens_60m_cooldown(self) -> None:
        snap = record_sorry(now=NOW, path=self.path, reference="ref-1")
        self.assertEqual(snap.state, "COOLDOWN")
        self.assertEqual(snap.consecutive_sorry_events, 1)
        self.assertEqual(snap.current_cooldown_seconds, 60 * 60)
        self.assertEqual(snap.next_probe_at, NOW + timedelta(hours=1))
        allowed, reason, _ = browser_work_allowed(now=NOW + timedelta(minutes=30), path=self.path)
        self.assertFalse(allowed)
        self.assertEqual(reason, EBAY_AVAILABILITY_COOLDOWN)

    def test_restart_preserves_cooldown(self) -> None:
        record_sorry(now=NOW, path=self.path, reference="ref-1")
        reloaded = load_availability(path=self.path)
        self.assertEqual(reloaded.state, "COOLDOWN")
        self.assertEqual(reloaded.next_probe_at, NOW + timedelta(hours=1))
        # Simulate worker restart clock still inside cooldown.
        allowed, reason, snap = browser_work_allowed(now=NOW + timedelta(minutes=59), path=self.path)
        self.assertFalse(allowed)
        self.assertEqual(reason, EBAY_AVAILABILITY_COOLDOWN)
        self.assertEqual(snap.state, "COOLDOWN")

    def test_cooldown_expiry_requires_single_probe(self) -> None:
        record_sorry(now=NOW, path=self.path)
        after = NOW + timedelta(hours=1, minutes=1)
        allowed_normal, reason_normal, _ = browser_work_allowed(now=after, path=self.path, for_probe=False)
        self.assertFalse(allowed_normal)
        self.assertEqual(reason_normal, EBAY_AVAILABILITY_COOLDOWN)
        allowed_probe, reason_probe, snap = browser_work_allowed(now=after, path=self.path, for_probe=True)
        self.assertTrue(allowed_probe)
        self.assertEqual(snap.state, "PROBE_REQUIRED")
        begin_probe(now=after, path=self.path)
        allowed_second, reason_second, _ = browser_work_allowed(now=after, path=self.path, for_probe=True)
        self.assertFalse(allowed_second)
        self.assertEqual(reason_second, "EBAY_AVAILABILITY_PROBE_IN_FLIGHT")

    def test_probe_sorry_opens_6h_cooldown(self) -> None:
        record_sorry(now=NOW, path=self.path)
        after = NOW + timedelta(hours=1, minutes=1)
        begin_probe(now=after, path=self.path)
        snap = record_sorry(now=after, path=self.path, from_probe=True, reference="probe-sorry")
        self.assertEqual(snap.state, "COOLDOWN")
        self.assertEqual(snap.consecutive_sorry_events, 2)
        self.assertEqual(snap.current_cooldown_seconds, 6 * 3600)
        self.assertEqual(snap.next_probe_at, after + timedelta(hours=6))

    def test_probe_healthy_sets_recovery_count_one(self) -> None:
        record_sorry(now=NOW, path=self.path)
        after = NOW + timedelta(hours=1, minutes=1)
        begin_probe(now=after, path=self.path)
        snap = record_healthy_browser_check(now=after, path=self.path, from_probe=True)
        self.assertEqual(snap.state, "HEALTHY")
        self.assertEqual(snap.recovery_health_count, 1)
        self.assertFalse(snap.confirmed_healthy)
        self.assertEqual(snap.consecutive_sorry_events, 0)

    def test_three_natural_healthy_checks_confirm(self) -> None:
        record_sorry(now=NOW, path=self.path)
        t = NOW + timedelta(hours=1, minutes=1)
        begin_probe(now=t, path=self.path)
        record_healthy_browser_check(now=t, path=self.path, from_probe=True)
        t2 = t + timedelta(minutes=5)
        record_healthy_browser_check(now=t2, path=self.path, from_probe=False)
        t3 = t2 + timedelta(minutes=5)
        snap = record_healthy_browser_check(now=t3, path=self.path, from_probe=False)
        self.assertEqual(snap.recovery_health_count, 3)
        self.assertTrue(snap.confirmed_healthy)
        self.assertEqual(snap.last_outcome, EBAY_AVAILABILITY_CONFIRMED_HEALTHY)

    def test_challenge_halts_across_restart(self) -> None:
        record_challenge(now=NOW, path=self.path, reference="captcha")
        allowed, reason, _ = browser_work_allowed(now=NOW + timedelta(days=1), path=self.path, for_probe=True)
        self.assertFalse(allowed)
        self.assertEqual(reason, EBAY_CHALLENGE_REQUIRED)
        reloaded = get_availability(now=NOW + timedelta(days=2), path=self.path)
        self.assertEqual(reloaded.state, "CHALLENGE_REQUIRED")

    def test_seed_from_pilot_preserves_6h_after_two_sorrys(self) -> None:
        last = datetime(2026, 9, 29, 6, 30, tzinfo=timezone.utc)
        snap = seed_from_observed_sorrys(
            last_sorry_at=last,
            consecutive_sorry_events=2,
            path=self.path,
            reference="Kakuna Chaos Rising SORRY",
            now=last + timedelta(minutes=5),
        )
        self.assertEqual(snap.state, "COOLDOWN")
        self.assertEqual(snap.consecutive_sorry_events, 2)
        self.assertEqual(snap.next_probe_at, last + timedelta(hours=6))


class OwnedDailyHoldDuringCooldownTests(unittest.TestCase):
    def test_unrelated_cards_not_consumable_during_cooldown(self) -> None:
        from cardscanr_market_engine.owned_daily_scheduler import (
            OwnedDailySchedulerConfig,
            OwnedPrintingRefreshScheduler,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ebay_availability_state.json"
            record_sorry(now=NOW, path=path, reference="r1")
            import os

            os.environ["EBAY_AVAILABILITY_STATE_PATH"] = str(path)
            try:
                # Also patch marketplace cooldown absences.
                from unittest.mock import patch

                with patch(
                    "cardscanr_market_engine.owned_daily_scheduler.get_active_cooldown",
                    return_value=None,
                ):
                    sched = OwnedPrintingRefreshScheduler(
                        client=object(),
                        config=OwnedDailySchedulerConfig(
                            supabase_url="https://example.supabase.co",
                            supabase_service_role_key="secret",
                            max_enqueues_per_run=10,
                            queue_low_watermark=0,
                            queue_high_watermark=100,
                            dry_run=True,
                            sync_keys_before_run=False,
                            allowed_markets=["AU"],
                            rolling_window_hours=6,
                            success_fresh_hours=24,
                            latest_report_path=Path(tmp) / "latest.json",
                            runs_report_path=Path(tmp) / "runs.jsonl",
                            enable_full_daily=False,
                        ),
                    )
                    target = {
                        "market_price_key_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        "fingerprint": "pokemon|en|sv1|1|bulbasaur|raw|raw|au|aud",
                        "market_country": "AU",
                        "currency": "AUD",
                        "owner_count": 1,
                        "total_owned_quantity": 1,
                        "due_for_owned_daily": True,
                        "current_market_price": 1.0,
                        "last_updated_at": (NOW - timedelta(hours=48)).isoformat(),
                        "refresh_status": "completed",
                        "next_refresh_due_at": (NOW - timedelta(hours=1)).isoformat(),
                    }
                    decision = sched.evaluate_target(target, now=NOW + timedelta(minutes=10))
                    self.assertFalse(decision.should_enqueue)
                    self.assertEqual(decision.reason, EBAY_AVAILABILITY_COOLDOWN)
            finally:
                os.environ.pop("EBAY_AVAILABILITY_STATE_PATH", None)


class FreshnessOnTransientTests(unittest.TestCase):
    def test_sorry_outcome_is_temporary_ebay_not_healthy(self) -> None:
        exc = ProviderTemporaryError(
            "TEMPORARY_EBAY_SERVER_FAILURE: eBay SORRY/error page",
            diagnostics={"reason": "ebay_sorry_error_page"},
        )
        outcome = classify_exception_outcome(exc, diagnostics=exc.diagnostics)
        self.assertEqual(outcome, TEMPORARY_EBAY_SERVER_FAILURE)
        self.assertNotIn(outcome, {"UPDATED_FROM_EBAY", "UNCHANGED_FROM_EBAY", "CHECKED_NO_NEW_EXACT_EVIDENCE"})


class JobLeaseRecoveryTests(unittest.TestCase):
    def test_stale_running_recoverable_active_not_stolen(self) -> None:
        now = NOW
        fresh_lock = (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        stale_lock = (now - timedelta(minutes=120)).isoformat().replace("+00:00", "Z")
        self.assertEqual(
            recovery_action_for_running_job(
                status="running",
                locked_at=fresh_lock,
                now=now,
                stale_after_minutes=90,
            ),
            "leave_running",
        )
        self.assertFalse(
            may_steal_running_lease(
                status="running",
                locked_at=fresh_lock,
                now=now,
                stale_after_minutes=90,
            )
        )
        self.assertEqual(
            recovery_action_for_running_job(
                status="running",
                locked_at=stale_lock,
                now=now,
                stale_after_minutes=90,
            ),
            "fail_stale_running",
        )
        self.assertTrue(
            may_steal_running_lease(
                status="running",
                locked_at=stale_lock,
                now=now,
                stale_after_minutes=90,
            )
        )

    def test_queued_ignored(self) -> None:
        self.assertEqual(
            recovery_action_for_running_job(status="queued", locked_at=None, now=NOW),
            "ignore_non_running",
        )


if __name__ == "__main__":
    unittest.main()
