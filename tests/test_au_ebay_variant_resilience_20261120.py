#!/usr/bin/env python3
"""Revert-proof tests: AU eBay generic Error Page + variant pacing (offline)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.live_ebay_navigation_accounting import (
    LIVE_SUBMISSION_RESERVED,
    row_needs_supplementary_cap_slots,
    supplementary_submission_needs_cap,
)
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest, ProviderResult, SoldComp
from cardscanr_market_engine.providers.au_ebay_variant_resilience import (
    LIVE_EBAY_NAVIGATION_COUNT,
    attempt_has_healthy_verified_sold_capture,
    generic_error_page_retry_delay_seconds,
    is_generic_error_page_failure,
    is_generic_error_page_nav,
    should_stop_query_variants_after_healthy_capture,
    sold_state_verification_blocked,
    variant_inter_attempt_delay_seconds,
)
from cardscanr_market_engine.providers.ebay_browser_provider import verify_sold_result_state
from cardscanr_market_engine.providers.errors import ProviderTemporaryError
from cardscanr_market_engine.providers.sold_page_health import (
    evaluate_sold_verification,
    is_tiny_ebay_generic_error_body,
)

SOLD_URL = "https://www.ebay.com.au/sch/i.html?_nkw=Nidoking&LH_Sold=1"
HEALTHY_TITLE = "Nidoking 11 base Pokemon for sale | eBay"
ERROR_TITLE = "Error Page | eBay"
HEALTHY_BODY = (
    "Filters\nSold items\n"
    + ("Sold 12 Nov 2026\nAU $4.20\nNidoking 11 base\n" * 30)
)
GENERIC_ERROR_BODY = (
    "Error Page | eBay\nSomething went wrong on our end.\n"
    "Please check back later.\n"
)


class FixtureContractTests(unittest.TestCase):
    def test_tiny_generic_error_body_fixture(self) -> None:
        self.assertTrue(is_tiny_ebay_generic_error_body(GENERIC_ERROR_BODY))
        self.assertLess(len(GENERIC_ERROR_BODY), 220)

    def test_healthy_sold_page_fixture_verified(self) -> None:
        ev = evaluate_sold_verification(url=SOLD_URL, title=HEALTHY_TITLE, body=HEALTHY_BODY)
        self.assertTrue(ev["x11SoldStateVerified"])

    def test_error_page_title_lh_sold_not_verified(self) -> None:
        ev = evaluate_sold_verification(url=SOLD_URL, title=ERROR_TITLE, body=GENERIC_ERROR_BODY)
        self.assertFalse(ev["x11SoldStateVerified"])
        self.assertFalse(ev["verified"])

    def test_verify_sold_result_state_rejects_error_page_with_lh_sold(self) -> None:
        state = verify_sold_result_state(url=SOLD_URL, title=ERROR_TITLE, body_text=GENERIC_ERROR_BODY)
        self.assertFalse(state["SOLD_STATE_VERIFIED"])

    def test_sold_state_verification_blocked_for_error_title(self) -> None:
        self.assertTrue(
            sold_state_verification_blocked(title=ERROR_TITLE, url=SOLD_URL, body=GENERIC_ERROR_BODY)
        )


class GenericErrorPageClassificationTests(unittest.TestCase):
    def test_generic_error_nav_without_sorry_markers(self) -> None:
        self.assertTrue(
            is_generic_error_page_nav(
                title=ERROR_TITLE,
                url=SOLD_URL,
                body=GENERIC_ERROR_BODY,
                sorry=False,
                challenge=False,
            )
        )

    def test_sorry_page_not_generic_error_only(self) -> None:
        self.assertFalse(
            is_generic_error_page_nav(
                title="Something went wrong | eBay",
                url=SOLD_URL,
                body="SORRY\nSomething went wrong on our end\n",
                sorry=True,
                challenge=False,
            )
        )

    def test_challenge_not_generic_error_retry(self) -> None:
        self.assertFalse(
            is_generic_error_page_nav(
                title="Please verify yourself",
                url=SOLD_URL,
                body="captcha",
                sorry=False,
                challenge=True,
            )
        )

    def test_generic_error_failure_detector(self) -> None:
        exc = ProviderTemporaryError(
            "TEMPORARY_EBAY_SERVER_FAILURE",
            diagnostics={
                "reason": "ebay_error_page",
                "desktopNav": {"title": ERROR_TITLE, "url": SOLD_URL, "sorry": True},
            },
        )
        self.assertTrue(is_generic_error_page_failure(exc))


class VariantPacingTests(unittest.TestCase):
    def _request(self) -> ProviderRequest:
        market = resolve_marketplace_config(market_country="AU", currency="AUD", marketplace="ebay")
        key = MarketPriceKey(
            id="pk1",
            game="pokemon",
            card_name="Nidoking",
            normalized_card_name="nidoking",
            set_name="Base",
            set_code="BASE1",
            collector_number="11",
            language="en",
            variant="normal",
            condition="near_mint",
            market_country="au",
            currency="aud",
            fingerprint="test",
            raw={},
        )
        return ProviderRequest(
            price_key=key,
            market_country=market.market_country,
            currency=market.currency,
            marketplace=market.marketplace,
            provider_marketplace_id=market.provider_marketplace_id,
            provider_domain=market.provider_domain,
            search_locale=market.search_locale,
            display_name=market.display_name,
            market_config=market,
        )

    def _healthy_result(self, *, comps: int = 5) -> ProviderResult:
        return ProviderResult(
            provider_name="ebay_browser",
            marketplace="EBAY_AU",
            provider_fingerprint="fp",
            query_used="q1",
            comps=[
                SoldComp(
                    source_listing_id=f"id{i}",
                    title=f"card {i}",
                    sold_price=1.0 + i,
                    shipping_price=0.0,
                    total_price=1.0 + i,
                    currency="AUD",
                    sold_date=None,
                    listing_url=f"https://www.ebay.com.au/itm/{i}",
                    condition_text="Used",
                    raw_metadata={"variant_match": True},
                )
                for i in range(comps)
            ],
            raw_metadata={
                "x11SoldStateVerified": True,
                "postSoldCapturePhase": "POST_SOLD_CAPTURE_READY",
                "desktopNav": {"SOLD_STATE_VERIFIED": True, "title": HEALTHY_TITLE, "url": SOLD_URL},
            },
        )

    def test_stop_variants_after_healthy_capture_meets_min(self) -> None:
        req = self._request()
        pair = (SimpleNamespace(query_text="q1", query_index=0, query_source="a"), self._healthy_result())
        self.assertTrue(
            should_stop_query_variants_after_healthy_capture(last_healthy=pair, request=req)
        )
        self.assertTrue(attempt_has_healthy_verified_sold_capture(pair[1]))

    @mock.patch.dict("os.environ", {"AU_EBAY_MIN_COMPARABLE_COUNT": "10"})
    def test_continue_variants_when_below_min_comparable(self) -> None:
        req = self._request()
        pair = (SimpleNamespace(), self._healthy_result(comps=1))
        self.assertFalse(
            should_stop_query_variants_after_healthy_capture(last_healthy=pair, request=req)
        )


class CapAccountingTests(unittest.TestCase):
    def test_supplementary_includes_extra_navigations_on_reserved_job(self) -> None:
        row = {
            LIVE_SUBMISSION_RESERVED: True,
            "marketCountry": "AU",
            "liveEbayNavigationCount": 3,
        }
        self.assertFalse(supplementary_submission_needs_cap(row))
        self.assertEqual(row_needs_supplementary_cap_slots(row), 2)

    def test_unreserved_job_counts_primary_plus_extras(self) -> None:
        row = {
            "marketCountry": "AU",
            "searchSubmissionStarted": True,
            "liveEbayNavigationCount": 2,
        }
        self.assertTrue(supplementary_submission_needs_cap(row))
        self.assertEqual(row_needs_supplementary_cap_slots(row), 2)


class JitterBoundsTests(unittest.TestCase):
    def test_variant_gap_within_bounds(self) -> None:
        for _ in range(20):
            delay = variant_inter_attempt_delay_seconds()
            self.assertGreaterEqual(delay, 45.0)
            self.assertLessEqual(delay, 90.0)

    def test_generic_error_retry_within_bounds(self) -> None:
        for _ in range(20):
            delay = generic_error_page_retry_delay_seconds()
            self.assertGreaterEqual(delay, 30.0)
            self.assertLessEqual(delay, 60.0)


class NavigationCountMetadataTests(unittest.TestCase):
    def test_navigation_count_constant_exported(self) -> None:
        self.assertEqual(LIVE_EBAY_NAVIGATION_COUNT, "liveEbayNavigationCount")


if __name__ == "__main__":
    unittest.main()