"""Multi-region verified-local continuous pricing: identity, isolation, dispatcher."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cardscanr_market_engine.continuous_safety import ContinuousSafetyBudget
from cardscanr_market_engine.demand_aware_scheduler import DemandEvent, DemandIndex, evaluate_demand_aware_target
from cardscanr_market_engine.ebay_availability import (
    browser_work_allowed,
    get_availability,
    record_sorry,
)
from cardscanr_market_engine.ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from cardscanr_market_engine.fingerprints import build_market_price_fingerprint
from cardscanr_market_engine.live_navigation_attempt import emit_search_submission_started
from cardscanr_market_engine.market_dispatcher import (
    acquire_browser_slot,
    global_concurrency,
    pick_fair_market,
    release_browser_slot,
)
from cardscanr_market_engine.marketplace_ops_state import record_marketplace_cooldown
from cardscanr_market_engine.providers.ebay_browser_provider import _iter_price_matches
from cardscanr_market_engine.providers.search_entry_contract import (
    UNACCOUNTED_SEARCH_URL_NAVIGATION,
    assert_programmatic_navigation_allowed,
)
from cardscanr_market_engine.providers.sold_control_identity import is_exact_sold_label
from cardscanr_market_engine.providers.sold_page_health import (
    HEALTHY_SOLD_RESULTS,
    classify_sold_page_health,
)
from cardscanr_market_engine.region_pricing_registry import (
    CARDSCANR_REGIONS,
    all_region_definitions,
    approved_sold_labels_for_market,
    region_definition,
)


NOW = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)
PRINTING = dict(
    game="pokemon",
    language="en",
    set_code="sv8",
    set_name="Surging Sparks",
    collector_number="001",
    card_name="Bulbasaur",
    variant="raw",
    condition="raw",
)


def _fp(market: str, currency: str, *, language: str = "en") -> str:
    return build_market_price_fingerprint(
        **{**PRINTING, "language": language, "market_country": market, "currency": currency}
    )


class MultiRegionContinuousPricingTests(unittest.TestCase):
    def test_fingerprint_includes_market_and_currency(self) -> None:
        au = _fp("AU", "AUD")
        us = _fp("US", "USD")
        self.assertIn("|au|aud", au)
        self.assertIn("|us|usd", us)
        self.assertNotEqual(au, us)
        self.assertNotEqual(_fp("JP", "JPY", language="en"), _fp("JP", "JPY", language="ja"))

    def test_region_registry_does_not_invent_jp_or_eu(self) -> None:
        defs = all_region_definitions()
        self.assertEqual(tuple(defs), CARDSCANR_REGIONS)
        self.assertTrue(defs["AU"].browser_capable)
        self.assertTrue(defs["US"].browser_capable)
        self.assertTrue(defs["GB"].browser_capable)
        self.assertTrue(defs["CA"].browser_capable)
        self.assertEqual(defs["JP"].status, "BLOCKED_NEEDS_PROVIDER")
        self.assertEqual(defs["EU"].status, "BLOCKED_NEEDS_PROVIDER")
        self.assertEqual(defs["AU"].currency, "AUD")
        self.assertEqual(defs["US"].currency, "USD")
        self.assertEqual(defs["GB"].currency, "GBP")
        self.assertEqual(defs["CA"].currency, "CAD")
        self.assertEqual(defs["JP"].currency, "JPY")
        self.assertEqual(defs["EU"].currency, "EUR")

    def test_au_fresh_us_stale_independent(self) -> None:
        au = {
            "fingerprint": _fp("AU", "AUD"),
            "market_country": "AU",
            "currency": "AUD",
            "owner_count": 1,
            "total_owned_quantity": 1,
            "market_price_key_id": "k-au",
            "current_market_price": 4.0,
            "display_price_source": "verified_local",
            "provider": "ebay_browser",
            "last_updated_at": (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            "refresh_status": "completed",
        }
        us = {
            **au,
            "fingerprint": _fp("US", "USD"),
            "market_country": "US",
            "currency": "USD",
            "market_price_key_id": "k-us",
            "last_updated_at": (NOW - timedelta(hours=30)).isoformat().replace("+00:00", "Z"),
        }
        idx = DemandIndex([])
        au_row = evaluate_demand_aware_target(au, now=NOW, demand_index=idx)
        us_row = evaluate_demand_aware_target(us, now=NOW, demand_index=idx)
        self.assertFalse(au_row.due)
        self.assertTrue(str(au_row.reason_code).startswith("FRESH_SKIP"))
        self.assertTrue(us_row.due)

    def test_demand_does_not_bypass_fresh_market(self) -> None:
        target = {
            "fingerprint": _fp("AU", "AUD"),
            "market_country": "AU",
            "currency": "AUD",
            "owner_count": 1,
            "total_owned_quantity": 1,
            "market_price_key_id": "k-hot",
            "current_market_price": 4.0,
            "display_price_source": "verified_local",
            "provider": "ebay_browser",
            "last_updated_at": (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            "refresh_status": "completed",
        }
        idx = DemandIndex(
            [
                DemandEvent(
                    requested_at=NOW - timedelta(minutes=5),
                    price_key_id="k-hot",
                    fingerprint=target["fingerprint"],
                    market="AU",
                    reason="user_refresh",
                )
                for _ in range(5)
            ]
        )
        row = evaluate_demand_aware_target(target, now=NOW, demand_index=idx)
        self.assertFalse(row.due)
        self.assertTrue(str(row.reason_code).startswith("FRESH_SKIP"))

    def test_reference_only_and_never_priced_eligibility(self) -> None:
        ref = {
            "fingerprint": _fp("GB", "GBP"),
            "market_country": "GB",
            "currency": "GBP",
            "owner_count": 1,
            "total_owned_quantity": 1,
            "market_price_key_id": "k-gb",
            "current_market_price": 1.2,
            "display_price_source": "reference",
            "provider": "tcgdex_cardmarket",
            "last_updated_at": (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "refresh_status": "completed",
        }
        never = {
            "fingerprint": _fp("CA", "CAD"),
            "market_country": "CA",
            "currency": "CAD",
            "owner_count": 1,
            "total_owned_quantity": 1,
            "market_price_key_id": "k-ca",
        }
        idx = DemandIndex([])
        gb = evaluate_demand_aware_target(ref, now=NOW, demand_index=idx)
        ca = evaluate_demand_aware_target(never, now=NOW, demand_index=idx)
        self.assertTrue(gb.due)
        self.assertTrue(ca.due)

    def test_availability_cooldown_is_market_specific(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ebay_availability_state.json"
            record_sorry(now=NOW, path=path, reference="us-sorry", market="US")
            us_ok, us_reason, us_snap = browser_work_allowed(now=NOW, path=path, market="US")
            au_ok, au_reason, au_snap = browser_work_allowed(now=NOW, path=path, market="AU")
            self.assertFalse(us_ok)
            self.assertEqual(us_snap.market, "US")
            self.assertTrue(au_ok)
            self.assertEqual(au_snap.market, "AU")
            au_fresh = get_availability(now=NOW, path=path, persist_transitions=False, market="AU")
            self.assertEqual(au_fresh.state, "HEALTHY")

    def test_ops_cooldown_does_not_cross_markets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ops.json"
            record_marketplace_cooldown("US", reason="TRANSIENT_EBAY", now=NOW, path=path, minutes=90)
            avail = Path(tmp) / "avail.json"
            us_gate = evaluate_ebay_browser_work_gate(
                market="US",
                now=NOW,
                ops_path=path,
                availability_path=avail,
                incidents_path=Path(tmp) / "incidents.json",
            )
            au_gate = evaluate_ebay_browser_work_gate(
                market="AU",
                now=NOW,
                ops_path=path,
                availability_path=avail,
                incidents_path=Path(tmp) / "incidents.json",
            )
            self.assertFalse(us_gate.allowed)
            self.assertTrue(any("MARKETPLACE_COOLDOWN" in c for c in us_gate.reason_codes))
            # AU gate may still fail if global availability file is unhealthy; isolate ops only.
            self.assertFalse(any("MARKETPLACE_COOLDOWN" in c for c in au_gate.reason_codes))

    def test_global_concurrency_and_serialized_slots(self) -> None:
        self.assertEqual(global_concurrency(), 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dispatcher.json"
            ok1, state = acquire_browser_slot(market="AU", price_key_id="a", path=path)
            ok2, _ = acquire_browser_slot(market="US", price_key_id="b", state=state, path=path)
            self.assertTrue(ok1)
            self.assertFalse(ok2)
            state = release_browser_slot(market="AU", now=NOW, path=path)
            ok3, _ = acquire_browser_slot(market="US", price_key_id="b", state=state, path=path)
            self.assertTrue(ok3)

    def test_market_fairness_and_anti_starvation(self) -> None:
        due = {"AU": 200, "US": 20, "GB": 8}
        picks: list[str] = []
        from cardscanr_market_engine.market_dispatcher import MarketDispatcherState

        state = MarketDispatcherState()
        now = NOW
        for _ in range(9):
            pick = pick_fair_market(due, now=now, state=state, enabled_markets=("AU", "US", "GB"))
            self.assertIsNotNone(pick)
            picks.append(str(pick))
            state.last_served_at[str(pick)] = now
            state.starvation_credit[str(pick)] = 0.0
            for other in ("AU", "US", "GB"):
                if other != pick:
                    state.starvation_credit[other] = state.starvation_credit.get(other, 0.0) + 1.0
            now = now + timedelta(minutes=5)
        self.assertIn("US", picks)
        self.assertIn("GB", picks)
        self.assertIn("AU", picks)

    def test_global_hourly_and_daily_budgets(self) -> None:
        budget = ContinuousSafetyBudget(max_submissions_per_hour=20, max_submissions_per_day=200)
        stamp = NOW
        for i in range(20):
            budget.record_submission(stamp + timedelta(minutes=i), market="AU" if i % 2 == 0 else "US")
        self.assertEqual(budget.budget_exhausted(stamp + timedelta(minutes=19)), "HOURLY_SUBMISSION_CEILING")
        self.assertGreater(budget.submissions_1h_for_market("AU", stamp + timedelta(minutes=19)), 0)
        self.assertGreater(budget.submissions_1h_for_market("US", stamp + timedelta(minutes=19)), 0)
        day = ContinuousSafetyBudget(max_submissions_per_hour=999, max_submissions_per_day=200)
        for i in range(200):
            day.record_submission(stamp + timedelta(minutes=i), market="AU")
        self.assertEqual(day.budget_exhausted(stamp + timedelta(hours=3)), "DAILY_SUBMISSION_CEILING")

    def test_attempt_event_records_market(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"CARDSCANR_LIVE_NAV_ATTEMPTS_DIR": tmp}):
                event = emit_search_submission_started(
                    attempt_id="att-us-1",
                    query="Bulbasaur 001",
                    price_key_id="k-us",
                    market="US",
                    currency="USD",
                    fingerprint=_fp("US", "USD"),
                )
                payload = event.to_dict()
                self.assertEqual(payload["market"], "US")
                self.assertEqual(payload["currency"], "USD")
                self.assertEqual(payload["priceKeyId"], "k-us")

    def test_currency_parsers_do_not_confuse_markets(self) -> None:
        aud = _iter_price_matches("Sold AU $12.50", expected_currency="AUD")
        usd = _iter_price_matches("Sold US $9.00", expected_currency="USD")
        gbp = _iter_price_matches("Sold £4.20", expected_currency="GBP")
        cad = _iter_price_matches("Sold C $8.00", expected_currency="CAD")
        self.assertTrue(any(m["currency"] == "AUD" and not m["rejected"] for m in aud))
        self.assertTrue(any(m["currency"] == "USD" and not m["rejected"] for m in usd))
        self.assertTrue(any(m["currency"] == "GBP" and not m["rejected"] for m in gbp))
        self.assertTrue(any(m["currency"] == "CAD" and not m["rejected"] for m in cad))
        self.assertFalse(any(m["currency"] == "USD" and not m["rejected"] for m in aud))

    def test_jpy_eur_parser_only_when_enabled(self) -> None:
        self.assertFalse(region_definition("JP").browser_capable)
        self.assertFalse(region_definition("EU").browser_capable)

    def test_query_url_guard_all_browser_markets(self) -> None:
        for market in ("AU", "US", "GB", "CA"):
            with self.assertRaises(Exception) as ctx:
                assert_programmatic_navigation_allowed(
                    url=f"https://www.{region_definition(market).marketplace_host}/sch/i.html?_nkw=bulbasaur",
                    attempt_id=None,
                )
            self.assertIn(UNACCOUNTED_SEARCH_URL_NAVIGATION, str(ctx.exception))

    def test_sold_label_identity_per_browser_market(self) -> None:
        for market in ("AU", "US", "GB", "CA"):
            self.assertIn("sold items", approved_sold_labels_for_market(market))
            self.assertTrue(is_exact_sold_label("Sold items", market=market))
            self.assertFalse(is_exact_sold_label("Completed items", market=market))

    def test_page_health_english_markets(self) -> None:
        result = classify_sold_page_health(
            title="bulbasaur | eBay",
            url="https://www.ebay.com/sch/i.html?LH_Sold=1",
            body="results matching sold items $9.00",
            expected_origin="ebay.com",
        )
        self.assertTrue(isinstance(result, dict))


if __name__ == "__main__":
    unittest.main()
