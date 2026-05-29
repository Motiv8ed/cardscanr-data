from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.filters import filter_comps
from cardscanr_market_engine.models import MarketPriceKey, SoldComp


def sample_price_key() -> MarketPriceKey:
    return MarketPriceKey(
        id="key-1",
        game="pokemon",
        card_name="Charizard",
        normalized_card_name="charizard",
        set_name="Base Set",
        set_code="base1",
        collector_number="4",
        language="en",
        variant="raw",
        condition="near_mint",
        market_country="us",
        currency="usd",
        fingerprint="pokemon|en|base1|4|charizard|raw|near_mint|us|usd",
    )


def sold_comp(title: str, total_price: float, *, source_listing_id: str = "listing-1") -> SoldComp:
    return SoldComp(
        source_listing_id=source_listing_id,
        title=title,
        sold_price=round(total_price - 1, 2),
        shipping_price=1.0,
        total_price=round(total_price, 2),
        currency="USD",
        sold_date=datetime(2026, 5, 20, tzinfo=timezone.utc),
        listing_url=f"https://example.test/{source_listing_id}",
        condition_text="Raw",
    )


def sold_comp_prices(
    title: str,
    sold_price: float,
    shipping_price: float,
    *,
    source_listing_id: str = "listing-1",
    raw_metadata: dict | None = None,
) -> SoldComp:
    return SoldComp(
        source_listing_id=source_listing_id,
        title=title,
        sold_price=sold_price,
        shipping_price=shipping_price,
        total_price=round(sold_price + shipping_price, 2),
        currency="USD",
        sold_date=datetime(2026, 5, 20, tzinfo=timezone.utc),
        listing_url=f"https://example.test/{source_listing_id}",
        condition_text="Raw",
        raw_metadata=raw_metadata or {},
    )


class FilterTests(unittest.TestCase):
    def test_rejects_graded_listing_for_raw_request(self) -> None:
        evaluated = filter_comps(sample_price_key(), [sold_comp("Charizard Base Set 4 PSA 10 graded", 50.0)])
        self.assertFalse(evaluated[0].included_in_estimate)
        self.assertEqual(evaluated[0].rejection_reason, "graded_for_raw_request")

    def test_rejects_obvious_outlier(self) -> None:
        comps = [
            sold_comp("Charizard Base Set 4 raw sold comp 1", 20.0, source_listing_id="a"),
            sold_comp("Charizard Base Set 4 raw sold comp 2", 21.0, source_listing_id="b"),
            sold_comp("Charizard Base Set 4 raw sold comp 3", 19.5, source_listing_id="c"),
            sold_comp("Charizard Base Set 4 raw sold comp 4", 20.5, source_listing_id="d"),
            sold_comp("Charizard Base Set 4 raw premium", 65.0, source_listing_id="e"),
        ]
        evaluated = filter_comps(sample_price_key(), comps)
        reasons = {item.comp.source_listing_id: item.rejection_reason for item in evaluated}
        self.assertEqual(reasons["e"], "obvious_outlier")
        self.assertTrue(all(reasons[item] is None for item in ("a", "b", "c", "d")))

    def test_exact_low_free_shipping_match_is_not_rejected_as_outlier(self) -> None:
        comps = [
            sold_comp_prices("Charizard Base Set 4 raw comp 1", 24.0, 15.0, source_listing_id="a"),
            sold_comp_prices("Charizard Base Set 4 raw comp 2", 25.0, 15.0, source_listing_id="b"),
            sold_comp_prices("Charizard Base Set 4 raw comp 3", 26.0, 15.0, source_listing_id="c"),
            sold_comp_prices("Charizard Base Set 4 raw comp 4", 25.0, 14.0, source_listing_id="d"),
            sold_comp_prices("Charizard Base Set 4 raw NM", 12.99, 0.0, source_listing_id="e"),
        ]
        evaluated = filter_comps(sample_price_key(), comps)
        reasons = {item.comp.source_listing_id: item.rejection_reason for item in evaluated}
        self.assertIsNone(reasons["e"])

    def test_landed_outlier_does_not_reject_item_price_valid_comp(self) -> None:
        comps = [
            sold_comp_prices("Charizard Base Set 4 raw comp 1", 20.0, 0.0, source_listing_id="a"),
            sold_comp_prices("Charizard Base Set 4 raw comp 2", 21.0, 0.0, source_listing_id="b"),
            sold_comp_prices("Charizard Base Set 4 raw comp 3", 19.5, 0.0, source_listing_id="c"),
            sold_comp_prices("Charizard Base Set 4 raw comp 4", 20.5, 0.0, source_listing_id="d"),
            sold_comp_prices("Charizard Base Set 4 raw with expensive shipping", 20.0, 40.0, source_listing_id="e"),
        ]
        evaluated = filter_comps(sample_price_key(), comps)
        reasons = {item.comp.source_listing_id: item.rejection_reason for item in evaluated}
        self.assertIsNone(reasons["e"])

    def test_price_range_and_pick_your_card_still_rejected(self) -> None:
        comps = [
            sold_comp_prices(
                "Charizard Base Set 4 Choose Your Card",
                10.0,
                0.0,
                source_listing_id="range",
                raw_metadata={"priceRangeListing": True},
            ),
            sold_comp_prices("PICK YOUR CARD Charizard Base Set 4", 10.0, 0.0, source_listing_id="pick"),
        ]
        evaluated = filter_comps(sample_price_key(), comps)
        reasons = {item.comp.source_listing_id: item.rejection_reason for item in evaluated}
        self.assertEqual(reasons["range"], "price_range_or_variation_listing")
        self.assertEqual(reasons["pick"], "price_range_or_variation_listing")


if __name__ == "__main__":
    unittest.main()
