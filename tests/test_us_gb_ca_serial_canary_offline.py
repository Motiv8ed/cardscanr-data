"""Offline guards for US/GB/CA serialized canaries and blocked JP/EU."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cardscanr_market_engine.browser_lifecycle_policy import (
    RUNTIME_COLD_START,
    RUNTIME_INTER_CARD,
    PriorCardContext,
    is_ebay_marketplace_host,
    required_runtime_mode,
)
from cardscanr_market_engine.continuous_worker_policy import classify_continuous_gate
from cardscanr_market_engine.market_dispatcher import pick_fair_market
from cardscanr_market_engine.navigation_runtime_context import (
    NavigationRuntimeContext,
    prepare_context_for_market,
)
from cardscanr_market_engine.region_pricing_registry import is_region_dispatchable, region_definition
from cardscanr_market_engine.region_pricing_status import region_status_row
from cardscanr_market_engine.region_pricing_registry import CARDSCANR_REGIONS
from cardscanr_market_engine import region_pricing_registry as region_registry


def _healthy_prior(market: str, host: str) -> PriorCardContext:
    return PriorCardContext(
        job_id="j1",
        attempt_id="a1",
        price_key_id="pk1",
        fingerprint="fp",
        target_id="T1",
        final_url=f"https://www.{host}/sch/i.html?LH_Sold=1",
        query="bulbasaur 001",
        x11_sold_state_verified=True,
        capture_correlated=True,
        card_verdict="PASS_PRICE_UPDATED",
        market=market,
        currency={"US": "USD", "GB": "GBP", "CA": "CAD", "AU": "AUD"}[market],
    )


class BlockedRegionAndMarketSwitchTests(unittest.TestCase):
    def test_jp_blocked_status(self) -> None:
        row = region_status_row("JP")
        self.assertEqual(row["provider"], "NONE")
        self.assertEqual(row["workerState"], "BLOCKED")
        self.assertFalse(row["enabled"])
        self.assertFalse(is_region_dispatchable("JP"))
        gate = classify_continuous_gate(market="JP")
        self.assertEqual(gate["workerState"], "BLOCKED")
        self.assertTrue(gate.get("blocked"))

    def test_eu_blocked_status(self) -> None:
        row = region_status_row("EU")
        self.assertEqual(row["provider"], "NONE")
        self.assertEqual(row["workerState"], "BLOCKED")
        self.assertFalse(row["enabled"])
        self.assertFalse(is_region_dispatchable("EU"))

    def test_blocked_markets_not_dispatched(self) -> None:
        pick = pick_fair_market(
            {"AU": 10, "JP": 99, "EU": 99},
            enabled_markets=("AU", "JP", "EU"),
        )
        self.assertEqual(pick, "AU")
        self.assertIsNone(
            pick_fair_market({"JP": 5, "EU": 5}, enabled_markets=("JP", "EU"))
        )

    def test_homepage_hosts(self) -> None:
        self.assertEqual(region_definition("US").homepage, "https://www.ebay.com/")
        self.assertEqual(region_definition("GB").homepage, "https://www.ebay.co.uk/")
        self.assertEqual(region_definition("CA").homepage, "https://www.ebay.ca/")
        self.assertTrue(is_ebay_marketplace_host("www.ebay.com"))
        self.assertTrue(is_ebay_marketplace_host("ebay.co.uk"))
        self.assertTrue(is_ebay_marketplace_host("www.ebay.ca"))

    def test_au_to_us_requires_cold_start(self) -> None:
        prior = _healthy_prior("AU", "ebay.com.au")
        self.assertEqual(required_runtime_mode(next_market="US", prior=prior), RUNTIME_COLD_START)
        ctx = NavigationRuntimeContext(runtime_mode=RUNTIME_INTER_CARD, expected_prior=prior)
        prepare_context_for_market(ctx, market="US", currency="USD")
        self.assertEqual(ctx.runtime_mode, RUNTIME_COLD_START)
        self.assertIsNone(ctx.expected_prior)
        self.assertEqual(ctx.marketplace_home, "https://www.ebay.com/")

    def test_us_inter_card_stays_us(self) -> None:
        prior = _healthy_prior("US", "ebay.com")
        self.assertEqual(required_runtime_mode(next_market="US", prior=prior), RUNTIME_INTER_CARD)
        ctx = NavigationRuntimeContext(runtime_mode=RUNTIME_COLD_START, expected_prior=prior)
        prepare_context_for_market(ctx, market="US", currency="USD")
        self.assertEqual(ctx.runtime_mode, RUNTIME_INTER_CARD)
        self.assertIsNotNone(ctx.expected_prior)

    def test_us_to_gb_requires_cold_start(self) -> None:
        prior = _healthy_prior("US", "ebay.com")
        self.assertEqual(required_runtime_mode(next_market="GB", prior=prior), RUNTIME_COLD_START)
        ctx = NavigationRuntimeContext(expected_prior=prior)
        prepare_context_for_market(ctx, market="GB", currency="GBP")
        self.assertEqual(ctx.runtime_mode, RUNTIME_COLD_START)
        self.assertEqual(ctx.marketplace_home, "https://www.ebay.co.uk/")

    def test_gb_inter_card_stays_gb(self) -> None:
        prior = _healthy_prior("GB", "ebay.co.uk")
        self.assertEqual(required_runtime_mode(next_market="GB", prior=prior), RUNTIME_INTER_CARD)

    def test_gb_to_ca_requires_cold_start(self) -> None:
        prior = _healthy_prior("GB", "ebay.co.uk")
        self.assertEqual(required_runtime_mode(next_market="CA", prior=prior), RUNTIME_COLD_START)
        ctx = NavigationRuntimeContext(expected_prior=prior)
        prepare_context_for_market(ctx, market="CA", currency="CAD")
        self.assertEqual(ctx.marketplace_home, "https://www.ebay.ca/")

    def test_cardscanr_regions_include_blocked(self) -> None:
        self.assertEqual(CARDSCANR_REGIONS, ("AU", "US", "GB", "CA", "JP", "EU"))

    def test_continuous_override_cannot_enable_jp_or_eu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuous_enabled_markets.json"
            with patch.object(region_registry, "CONTINUOUS_MARKETS_FLAG", path):
                region_registry.write_continuous_market_overrides(["US", "JP", "EU", "GB"])
                self.assertEqual(
                    region_registry.load_continuous_market_overrides(),
                    frozenset({"US", "GB"}),
                )
                self.assertEqual(region_registry.region_definition("US").status, "CONTINUOUS")
                self.assertTrue(region_registry.region_definition("US").worker_enable_default)
                self.assertEqual(region_registry.region_definition("JP").provider, "NONE")
                self.assertFalse(region_registry.region_definition("JP").worker_enable_default)
                self.assertEqual(region_registry.region_definition("CA").status, "READY_FOR_BROWSER_CANARY")
                us_row = region_status_row("US")
                self.assertEqual(us_row["status"], "CONTINUOUS")
                self.assertTrue(us_row["enabled"])
                jp_row = region_status_row("JP")
                self.assertEqual(jp_row["workerState"], "BLOCKED")
                self.assertFalse(jp_row["enabled"])

    def test_claim_sql_filters_by_market_not_global_fifo(self) -> None:
        sql = (
            Path(__file__).resolve().parents[1]
            / "supabase"
            / "migrations"
            / "20261005081000_claim_jobs_for_market.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("claim_market_price_refresh_jobs_for_market", sql)
        self.assertIn("lower(k.market_country) = lower(trim(p_market_country))", sql)
        self.assertNotIn("ebay.eu", sql)

    def test_claim_jobs_routes_market_filter(self) -> None:
        from cardscanr_market_engine.supabase_client import SupabaseMarketEngineClient

        client = SupabaseMarketEngineClient.__new__(SupabaseMarketEngineClient)
        calls: list[tuple[str, dict]] = []

        def _rpc(name: str, params: dict) -> list:
            calls.append((name, params))
            return []

        client._rpc = _rpc  # type: ignore[method-assign]
        client.claim_jobs(worker_id="w", max_jobs=1, market_country="US")
        self.assertEqual(calls[0][0], "claim_market_price_refresh_jobs_for_market")
        self.assertEqual(calls[0][1]["p_market_country"], "US")
        client.claim_jobs(worker_id="w", max_jobs=1)
        self.assertEqual(calls[1][0], "claim_market_price_refresh_jobs")

    def test_owned_daily_sql_fans_out_browser_markets_not_jp_eu(self) -> None:
        sql = (
            Path(__file__).resolve().parents[1]
            / "supabase"
            / "migrations"
            / "20261005080000_owned_daily_browser_market_fanout.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("('us', 'usd')", sql)
        self.assertIn("('gb', 'gbp')", sql)
        self.assertIn("('ca', 'cad')", sql)
        self.assertIn("('au', 'aud')", sql)
        self.assertNotIn("('jp'", sql.lower())
        self.assertNotIn("('eu'", sql.lower())
        self.assertIn("browser_ready_market_fanout", sql)


if __name__ == "__main__":
    unittest.main()
