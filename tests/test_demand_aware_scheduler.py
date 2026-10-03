"""Demand-aware market-specific scheduler. ZERO eBay."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.demand_aware_scheduler import (
    DemandEvent,
    DemandIndex,
    evaluate_demand_aware_target,
    select_fair_lane_mix,
)
from cardscanr_market_engine.owned_daily_scheduler import (
    OwnedDailySchedulerConfig,
    OwnedPrintingRefreshScheduler,
)
from tests.test_owned_daily_scheduler import FakeOwnedClient, config

NOW = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)
PRINTING_FP = "pokemon|en|sv8|001|bulbasaur|raw|raw"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _verified(market: str, currency: str, *, age_h: float, key: str, owners: int = 1) -> dict:
    return {
        "fingerprint": f"{PRINTING_FP}|{market.lower()}|{currency.lower()}",
        "market_country": market,
        "currency": currency,
        "owner_count": owners,
        "total_owned_quantity": owners,
        "market_price_key_id": key,
        "current_market_price": 2.5,
        "display_price_source": "verified_local",
        "provider": "ebay_browser",
        "last_updated_at": iso(NOW - timedelta(hours=age_h)),
        "refresh_status": "completed",
    }


def _reference(market: str, currency: str, *, age_h: float, key: str) -> dict:
    row = _verified(market, currency, age_h=age_h, key=key)
    row["display_price_source"] = "reference"
    row["provider"] = "tcgdex_cardmarket"
    return row


def _never(market: str, currency: str, *, key: str) -> dict:
    return {
        "fingerprint": f"{PRINTING_FP}|{market.lower()}|{currency.lower()}",
        "market_country": market,
        "currency": currency,
        "owner_count": 1,
        "total_owned_quantity": 1,
        "market_price_key_id": key,
        "current_market_price": None,
        "display_price_source": None,
        "provider": None,
        "last_updated_at": None,
        "refresh_status": None,
    }


def _high_demand(key: str, market: str, *, hours_ago: float = 0.2) -> DemandEvent:
    return DemandEvent(
        requested_at=NOW - timedelta(hours=hours_ago),
        price_key_id=key,
        fingerprint=f"{PRINTING_FP}|{market.lower()}|aud" if market == "AU" else "",
        market=market,
        reason="user_refresh",
    )


class DemandAwareUnitTests(unittest.TestCase):
    def test_popular_fresh_skips(self) -> None:
        t = _verified("AU", "AUD", age_h=3, key="k-pop-fresh")
        idx = DemandIndex(
            [
                _high_demand("k-pop-fresh", "AU", hours_ago=0.2),
                _high_demand("k-pop-fresh", "AU", hours_ago=0.5),
                _high_demand("k-pop-fresh", "AU", hours_ago=2.0),
            ]
        )
        row = evaluate_demand_aware_target(t, now=NOW, demand_index=idx)
        self.assertFalse(row.due)
        self.assertFalse(row.would_hit_ebay)
        self.assertEqual(row.demand_class, "HIGH")
        self.assertEqual(row.freshness_threshold_hours, 12)
        self.assertTrue(row.reason_code.startswith("FRESH_SKIP"))
        self.assertEqual(row.scheduler_lane, "FRESH_SKIP")

    def test_high_demand_11h59_skip(self) -> None:
        t = _verified("AU", "AUD", age_h=11.0 + 59 / 60.0, key="k-1159")
        idx = DemandIndex([_high_demand("k-1159", "AU", hours_ago=h) for h in (0.2, 1.0, 2.0)])
        row = evaluate_demand_aware_target(t, now=NOW, demand_index=idx)
        self.assertFalse(row.due)
        self.assertEqual(row.freshness_threshold_hours, 12)

    def test_high_demand_ge_12h_due(self) -> None:
        t = _verified("AU", "AUD", age_h=12.0, key="k-12")
        idx = DemandIndex([_high_demand("k-12", "AU", hours_ago=h) for h in (0.2, 1.0, 2.0)])
        row = evaluate_demand_aware_target(t, now=NOW, demand_index=idx)
        self.assertTrue(row.due)

    def test_normal_12h_skip(self) -> None:
        t = _verified("AU", "AUD", age_h=12, key="k-n12")
        row = evaluate_demand_aware_target(t, now=NOW)
        self.assertFalse(row.due)
        self.assertEqual(row.freshness_threshold_hours, 24)

    def test_normal_23h59_skip(self) -> None:
        t = _verified("AU", "AUD", age_h=23.0 + 59 / 60.0, key="k-2359")
        row = evaluate_demand_aware_target(t, now=NOW)
        self.assertFalse(row.due)

    def test_demand_decays_after_7d(self) -> None:
        t = _verified("AU", "AUD", age_h=3, key="k-oldpop")
        idx = DemandIndex(
            [
                DemandEvent(
                    requested_at=NOW - timedelta(days=8),
                    price_key_id="k-oldpop",
                    market="AU",
                    reason="user_refresh",
                )
            ]
        )
        row = evaluate_demand_aware_target(t, now=NOW, demand_index=idx)
        self.assertEqual(row.demand_class, "LOW")
        self.assertEqual(row.requests_7d, 0)
        self.assertEqual(row.demand_score, 0.0)

    def test_empty_lane_capacity_redistributes(self) -> None:
        rows = [
            evaluate_demand_aware_target(_never("AU", "AUD", key=f"cov-{i}"), now=NOW)
            for i in range(8)
        ]
        mix = select_fair_lane_mix(rows, budget=5)
        self.assertEqual(len(mix), 5)
        self.assertTrue(all(r.scheduler_lane == "COVERAGE" for r in mix))

    def test_single_user_request_is_not_catalogue_wide_high(self) -> None:
        t = _verified("AU", "AUD", age_h=3, key="k-one")
        idx = DemandIndex([_high_demand("k-one", "AU")])
        row = evaluate_demand_aware_target(t, now=NOW, demand_index=idx)
        self.assertNotEqual(row.demand_class, "HIGH")
        self.assertEqual(row.freshness_threshold_hours, 12)
        self.assertFalse(row.due)

    def test_popular_ge_12h_due(self) -> None:
        t = _verified("AU", "AUD", age_h=12.1, key="k-pop-12")
        idx = DemandIndex(
            [_high_demand("k-pop-12", "AU", hours_ago=h) for h in (0.2, 1.0, 2.0)]
        )
        row = evaluate_demand_aware_target(t, now=NOW, demand_index=idx)
        self.assertTrue(row.due)
        self.assertEqual(row.demand_class, "HIGH")
        self.assertEqual(row.freshness_threshold_hours, 12)
        self.assertEqual(row.scheduler_lane, "DEMAND")
        self.assertIn("DUE_HIGH_DEMAND", row.reason_code)

    def test_normal_lt_24h_skip(self) -> None:
        t = _verified("AU", "AUD", age_h=18, key="k-norm-18")
        row = evaluate_demand_aware_target(t, now=NOW, demand_index=DemandIndex([]))
        self.assertFalse(row.due)
        self.assertEqual(row.demand_class, "LOW")
        self.assertEqual(row.freshness_threshold_hours, 24)
        self.assertTrue(row.reason_code.startswith("FRESH_SKIP"))

    def test_normal_ge_24h_due(self) -> None:
        t = _verified("AU", "AUD", age_h=24.2, key="k-norm-24")
        row = evaluate_demand_aware_target(t, now=NOW, demand_index=DemandIndex([]))
        self.assertTrue(row.due)
        self.assertEqual(row.scheduler_lane, "STALE_OWNED")

    def test_reference_only_due_despite_recent_stamp(self) -> None:
        t = _reference("AU", "AUD", age_h=0.5, key="k-ref")
        idx = DemandIndex([_high_demand("k-ref", "AU")])
        row = evaluate_demand_aware_target(t, now=NOW, demand_index=idx)
        self.assertTrue(row.due)
        self.assertEqual(row.source_class, "reference_only")
        self.assertEqual(row.reason_code, "DUE_REFERENCE_ONLY_NEEDS_VERIFIED_LOCAL")

    def test_never_priced_due(self) -> None:
        t = _never("AU", "AUD", key="k-never")
        row = evaluate_demand_aware_target(t, now=NOW)
        self.assertTrue(row.due)
        self.assertEqual(row.reason_code, "DUE_NEVER_PRICED")
        self.assertEqual(row.scheduler_lane, "COVERAGE")

    def test_demand_does_not_bypass_freshness(self) -> None:
        t = _verified("AU", "AUD", age_h=1, key="k-hot")
        events = [_high_demand("k-hot", "AU", hours_ago=h) for h in (0.1, 0.2, 0.3, 0.4)]
        row = evaluate_demand_aware_target(t, now=NOW, demand_index=DemandIndex(events))
        self.assertGreater(row.demand_score, 0)
        self.assertFalse(row.due)

    def test_low_demand_old_gains_age_boost(self) -> None:
        young = evaluate_demand_aware_target(
            _verified("AU", "AUD", age_h=25, key="k-y"), now=NOW
        )
        old = evaluate_demand_aware_target(
            _verified("AU", "AUD", age_h=240, key="k-o"), now=NOW
        )
        self.assertGreater(old.age_boost, young.age_boost)
        self.assertGreater(old.final_priority, young.final_priority)

    def test_owned_daily_reason_is_not_demand(self) -> None:
        ev = DemandEvent(
            requested_at=NOW - timedelta(minutes=10),
            price_key_id="k-eng",
            market="AU",
            reason="owned_daily:P1_STALE_GT_24H",
        )
        t = _verified("AU", "AUD", age_h=3, key="k-eng")
        row = evaluate_demand_aware_target(t, now=NOW, demand_index=DemandIndex([ev]))
        self.assertEqual(row.demand_class, "LOW")
        self.assertEqual(row.requests_24h, 0)

    def test_dedupe_active_canonical_job(self) -> None:
        t = _never("AU", "AUD", key="k-dup")
        row = evaluate_demand_aware_target(
            t, now=NOW, active_job={"id": "job-1", "status": "queued"}
        )
        self.assertFalse(row.due)
        self.assertEqual(row.reason_code, "DEDUPED_ACTIVE_CANONICAL_JOB")

    def test_market_isolation_same_printing(self) -> None:
        au = _verified("AU", "AUD", age_h=3, key="k-au")
        us = _verified("US", "USD", age_h=30, key="k-us")
        gb = _reference("GB", "GBP", age_h=1, key="k-gb")
        ca = _never("CA", "CAD", key="k-ca")
        idx = DemandIndex([_high_demand("k-au", "AU")] * 3)
        rau = evaluate_demand_aware_target(au, now=NOW, demand_index=idx)
        rus = evaluate_demand_aware_target(us, now=NOW, demand_index=idx)
        rgb = evaluate_demand_aware_target(gb, now=NOW, demand_index=idx)
        rca = evaluate_demand_aware_target(ca, now=NOW, demand_index=idx)
        self.assertFalse(rau.due)
        self.assertTrue(rus.due)
        self.assertTrue(rgb.due)
        self.assertTrue(rca.due)
        self.assertEqual(rau.market, "AU")
        self.assertEqual(rus.market, "US")
        self.assertNotEqual(rau.reason_code, rus.reason_code)

    def test_fair_mix_does_not_fill_with_fresh(self) -> None:
        rows = []
        for i in range(8):
            t = _verified("AU", "AUD", age_h=2, key=f"fresh-{i}")
            rows.append(evaluate_demand_aware_target(t, now=NOW, demand_index=DemandIndex([_high_demand(f"fresh-{i}", "AU")])))
        stale = evaluate_demand_aware_target(_verified("AU", "AUD", age_h=30, key="stale-1"), now=NOW)
        never = evaluate_demand_aware_target(_never("AU", "AUD", key="never-1"), now=NOW)
        mix = select_fair_lane_mix(rows + [stale, never], budget=10)
        ids = {r.price_key_id for r in mix}
        self.assertNotIn("fresh-0", ids)
        self.assertIn("stale-1", ids)
        self.assertIn("never-1", ids)
        self.assertEqual(len(mix), 2)

    def test_backlog_cannot_starve(self) -> None:
        rows = []
        for i in range(20):
            t = _verified("AU", "AUD", age_h=13, key=f"hot-{i}")
            rows.append(
                evaluate_demand_aware_target(
                    t, now=NOW, demand_index=DemandIndex([_high_demand(f"hot-{i}", "AU")] * 3)
                )
            )
        backlog = evaluate_demand_aware_target(
            _verified("AU", "AUD", age_h=400, key="old-low"), now=NOW
        )
        coverage = evaluate_demand_aware_target(_never("AU", "AUD", key="cov-1"), now=NOW)
        mix = select_fair_lane_mix(rows + [backlog, coverage], budget=10)
        lanes = {r.scheduler_lane for r in mix}
        ids = {r.price_key_id for r in mix}
        self.assertIn("DEMAND", lanes)
        self.assertTrue("old-low" in ids or "cov-1" in ids)
        self.assertIn("COVERAGE", lanes.union({"STALE_OWNED"}))


class DemandAwareSchedulerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cooldown_patch = patch(
            "cardscanr_market_engine.owned_daily_scheduler.get_active_cooldown",
            return_value=None,
        )
        self._cooldown_patch.start()
        self.addCleanup(self._cooldown_patch.stop)
        self._avail_patch = patch(
            "cardscanr_market_engine.owned_daily_scheduler.browser_work_allowed",
            return_value=(True, "EBAY_AVAILABILITY_HEALTHY", None),
        )
        self._avail_patch.start()
        self.addCleanup(self._avail_patch.stop)

    def test_scheduler_skips_fresh_high_demand(self) -> None:
        targets = [_verified("AU", "AUD", age_h=4, key="k-f")]
        client = FakeOwnedClient(targets)
        client.list_recent_user_demand_jobs = lambda hours=168: [
            {
                "price_key_id": "k-f",
                "reason": "user_refresh",
                "requested_at": iso(NOW - timedelta(minutes=5)),
            }
        ]
        sched = OwnedPrintingRefreshScheduler(
            client=client, config=config(), now_func=lambda: NOW
        )
        report = sched.run_once()
        self.assertEqual(report["summary"]["jobsEnqueued"], 0)
        self.assertGreaterEqual(report["metrics"]["OWNED_PRICE_SKIPPED_FRESH"], 1)

    def test_browser_gate_outranks_demand(self) -> None:
        targets = [_verified("AU", "AUD", age_h=30, key="k-hot")]
        client = FakeOwnedClient(targets)
        client.list_recent_user_demand_jobs = lambda hours=168: [
            {
                "price_key_id": "k-hot",
                "reason": "user_refresh",
                "requested_at": iso(NOW - timedelta(minutes=5)),
            }
        ] * 3
        sched = OwnedPrintingRefreshScheduler(
            client=client, config=config(), now_func=lambda: NOW
        )
        snap = type(
            "Snap",
            (),
            {"state": "COOLDOWN", "next_probe_at": None, "consecutive_sorry_events": 1},
        )()
        with patch(
            "cardscanr_market_engine.owned_daily_scheduler.browser_work_allowed",
            return_value=(False, "EBAY_AVAILABILITY_COOLDOWN", snap),
        ):
            decision = sched.evaluate_target(targets[0], now=NOW)
        self.assertFalse(decision.should_enqueue)
        self.assertIn("COOLDOWN", str(decision.reason).upper())
        t = _never("AU", "AUD", key="k1")
        client = FakeOwnedClient([t, dict(t)])
        sched = OwnedPrintingRefreshScheduler(
            client=client, config=config(), now_func=lambda: NOW
        )
        report = sched.run_once()
        self.assertEqual(report["summary"]["jobsEnqueued"], 1)


class HorizonSimulationTests(unittest.TestCase):
    def test_horizons_popular_and_normal(self) -> None:
        popular = _verified("AU", "AUD", age_h=1, key="pop")
        normal = _verified("AU", "AUD", age_h=1, key="norm")
        idx = DemandIndex(
            [
                DemandEvent(
                    requested_at=NOW - timedelta(hours=h),
                    price_key_id="pop",
                    market="AU",
                    reason="user_refresh",
                )
                for h in (0.2, 1.0, 2.5)
            ]
        )
        proofs = {}
        for label, delta in [
            ("NOW", 0),
            ("+12h", 12),
            ("+24h", 24),
            ("+48h", 48),
            ("+72h", 72),
            ("+7d", 168),
        ]:
            now = NOW + timedelta(hours=delta)
            p = evaluate_demand_aware_target(popular, now=now, demand_index=idx)
            n = evaluate_demand_aware_target(normal, now=now, demand_index=DemandIndex([]))
            proofs[label] = {"popularDue": p.due, "normalDue": n.due, "popAge": p.verified_age_hours}
        self.assertFalse(proofs["NOW"]["popularDue"])
        self.assertFalse(proofs["NOW"]["normalDue"])
        self.assertTrue(proofs["+12h"]["popularDue"])
        self.assertFalse(proofs["+12h"]["normalDue"])
        self.assertTrue(proofs["+24h"]["normalDue"])
        self.assertTrue(proofs["+7d"]["popularDue"])
        self.assertTrue(proofs["+7d"]["normalDue"])


if __name__ == "__main__":
    unittest.main()
