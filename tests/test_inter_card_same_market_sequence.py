"""Offline regression: same-market card1=COLD_START, card2-5=INTER_CARD (US/GB/CA).

No eBay contact. Proves the production handoff used by --once workers:
healthy result → persist prior → next load → required_runtime_mode.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cardscanr_market_engine.browser_lifecycle_policy import (
    RUNTIME_COLD_START,
    RUNTIME_INTER_CARD,
    PriorCardContext,
    prior_from_healthy_job_result,
    required_runtime_mode,
)
from cardscanr_market_engine.navigation_runtime_context import (
    DEFAULT_CONTEXT_PATH,
    NavigationRuntimeContext,
    clear_context_from_environ,
    load_navigation_runtime_context,
    persist_inter_card_from_healthy_result,
    prepare_context_for_market,
)


def _healthy_sold_result(*, market: str, currency: str, host: str, card_n: int) -> dict:
    target = f"TARGET_{market}_{card_n}"
    query = f"Card{card_n} 1 set Pokemon"
    return {
        "jobId": f"job-{market.lower()}-{card_n}",
        "priceKeyId": f"key-{market.lower()}-{card_n}",
        "ownedDailyOutcome": "UPDATED_FROM_EBAY",
        "status": "completed",
        "marketCountry": market,
        "currency": currency,
        "x11SoldStateVerified": True,
        "fingerprint": f"pokemon|en|set|{card_n}|card{card_n}|raw|raw|{market.lower()}|{currency.lower()}",
        "currentJobCapture": {
            "jobId": f"job-{market.lower()}-{card_n}",
            "priceKeyId": f"key-{market.lower()}-{card_n}",
            "targetId": target,
            "fingerprint": f"pokemon|en|set|{card_n}|card{card_n}|raw|raw|{market.lower()}|{currency.lower()}",
        },
        "desktopNav": {
            "SOLD_STATE_VERIFIED": True,
            "url": f"https://www.{host}/sch/i.html?_nkw=Card{card_n}+1+set+Pokemon&LH_Sold=1",
            "queryExpected": query,
            "targetId": target,
        },
    }


class SameMarketSequenceTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_context_from_environ()

    def _simulate_five_card_modes(self, market: str, currency: str, host: str) -> list[str]:
        modes: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            ctx_path = Path(tmp) / "nav_runtime_context.json"
            with mock.patch(
                "cardscanr_market_engine.navigation_runtime_context.DEFAULT_CONTEXT_PATH",
                ctx_path,
            ):
                clear_context_from_environ()
                if ctx_path.exists():
                    ctx_path.unlink()
                for card_n in range(1, 6):
                    loaded = load_navigation_runtime_context()
                    prepare_context_for_market(loaded, market=market, currency=currency)
                    modes.append(loaded.runtime_mode)
                    result = _healthy_sold_result(
                        market=market, currency=currency, host=host, card_n=card_n
                    )
                    persist_inter_card_from_healthy_result(result)
                    clear_context_from_environ()  # emulate separate --once worker
        return modes

    def test_us_card1_cold_start_card2_to_5_inter_card(self) -> None:
        modes = self._simulate_five_card_modes("US", "USD", "ebay.com")
        self.assertEqual(modes[0], RUNTIME_COLD_START)
        self.assertEqual(modes[1:], [RUNTIME_INTER_CARD] * 4)

    def test_gb_equivalent_sequence(self) -> None:
        modes = self._simulate_five_card_modes("GB", "GBP", "ebay.co.uk")
        self.assertEqual(modes[0], RUNTIME_COLD_START)
        self.assertEqual(modes[1:], [RUNTIME_INTER_CARD] * 4)

    def test_ca_equivalent_sequence(self) -> None:
        modes = self._simulate_five_card_modes("CA", "CAD", "ebay.ca")
        self.assertEqual(modes[0], RUNTIME_COLD_START)
        self.assertEqual(modes[1:], [RUNTIME_INTER_CARD] * 4)

    def test_market_switch_forces_cold_start(self) -> None:
        us = _healthy_sold_result(market="US", currency="USD", host="ebay.com", card_n=1)
        persist_inter_card_from_healthy_result(us)
        clear_context_from_environ()
        loaded = load_navigation_runtime_context()
        self.assertEqual(required_runtime_mode(next_market="US", prior=loaded.expected_prior), RUNTIME_INTER_CARD)
        self.assertEqual(required_runtime_mode(next_market="GB", prior=loaded.expected_prior), RUNTIME_COLD_START)
        self.assertEqual(required_runtime_mode(next_market="CA", prior=loaded.expected_prior), RUNTIME_COLD_START)

    def test_unhealthy_prior_forces_cold_start(self) -> None:
        prior = PriorCardContext(
            job_id="bad",
            target_id="T1",
            final_url="https://www.ebay.com/sch/i.html?_nkw=x&LH_Sold=1",
            query="x",
            x11_sold_state_verified=False,
            capture_correlated=True,
            card_verdict="FAIL_CAPTURE",
            market="US",
            currency="USD",
        )
        self.assertEqual(required_runtime_mode(next_market="US", prior=prior), RUNTIME_COLD_START)

    def test_prior_from_healthy_job_result_roundtrip(self) -> None:
        prior = prior_from_healthy_job_result(
            _healthy_sold_result(market="US", currency="USD", host="ebay.com", card_n=1)
        )
        self.assertIsNotNone(prior)
        assert prior is not None
        self.assertEqual(prior.market, "US")
        self.assertTrue(prior.x11_sold_state_verified)
        self.assertEqual(required_runtime_mode(next_market="US", prior=prior), RUNTIME_INTER_CARD)


if __name__ == "__main__":
    unittest.main()
