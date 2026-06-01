from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.filters import filter_comps
from cardscanr_market_engine.models import MarketPriceKey, SoldComp


def sample_price_key(
    *,
    card_name: str = "Charizard",
    normalized_card_name: str = "charizard",
    set_name: str = "Base Set",
    set_code: str = "base1",
    collector_number: str = "4",
    variant: str = "raw",
) -> MarketPriceKey:
    return MarketPriceKey(
        id="key-1",
        game="pokemon",
        card_name=card_name,
        normalized_card_name=normalized_card_name,
        set_name=set_name,
        set_code=set_code,
        collector_number=collector_number,
        language="en",
        variant=variant,
        condition="near_mint",
        market_country="us",
        currency="usd",
        fingerprint=f"pokemon|en|{set_code}|{collector_number}|{normalized_card_name}|{variant}|near_mint|us|usd",
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

    def test_wrong_collector_number_is_rejected(self) -> None:
        evaluated = filter_comps(sample_price_key(), [sold_comp("Charizard Base Set 99 raw", 20.0)])
        self.assertEqual(evaluated[0].rejection_reason, "wrong_collector_number")

    def test_wrong_card_name_is_rejected(self) -> None:
        evaluated = filter_comps(sample_price_key(), [sold_comp("Blastoise Base Set 4 raw", 20.0)])
        self.assertEqual(evaluated[0].rejection_reason, "wrong_card_name")

    def test_wrong_explicit_set_code_is_rejected(self) -> None:
        key = sample_price_key()
        keyed = MarketPriceKey(**{**key.__dict__, "set_code": "sv03"})
        evaluated = filter_comps(keyed, [sold_comp("Charizard sv04 4 raw", 20.0)])
        self.assertEqual(evaluated[0].rejection_reason, "wrong_set")

    def test_jumbo_listing_is_rejected(self) -> None:
        evaluated = filter_comps(sample_price_key(), [sold_comp("Charizard Base Set 4 jumbo card", 20.0)])
        self.assertEqual(evaluated[0].rejection_reason, "oversized_or_jumbo")

    def test_exact_raw_card_with_free_delivery_is_included(self) -> None:
        evaluated = filter_comps(
            sample_price_key(),
            [sold_comp_prices("Charizard Base Set 4 raw", 12.99, 0.0)],
        )
        self.assertTrue(evaluated[0].included_in_estimate)

    def test_non_holo_espurr_rejects_reverse_holo(self) -> None:
        key = sample_price_key(
            card_name="Espurr",
            normalized_card_name="espurr",
            set_name="Chaos Rising",
            set_code="me02",
            collector_number="036/086",
            variant="non_holo",
        )
        evaluated = filter_comps(
            key,
            [sold_comp_prices("Espurr - 036/086 - Reverse Holo - Chaos Rising - NM/M - Pokemon Card", 2.96, 0)],
        )
        self.assertEqual(evaluated[0].rejection_reason, "wrong_variant_reverse_holo")
        self.assertEqual(evaluated[0].comp.raw_metadata["requested_variant"], "non_holo")
        self.assertEqual(evaluated[0].comp.raw_metadata["detected_variant"], "reverse_holo")
        self.assertFalse(evaluated[0].comp.raw_metadata["variant_match"])

    def test_non_holo_espurr_includes_plain_common(self) -> None:
        key = sample_price_key(
            card_name="Espurr",
            normalized_card_name="espurr",
            set_name="Chaos Rising",
            set_code="me02",
            collector_number="036/086",
            variant="non_holo",
        )
        evaluated = filter_comps(key, [sold_comp_prices("Espurr 036/086 Chaos Rising Common", 2.0, 0)])
        self.assertTrue(evaluated[0].included_in_estimate)
        self.assertEqual(evaluated[0].comp.raw_metadata["detected_variant"], "non_holo")

    def test_reverse_holo_requires_reverse_holo_title(self) -> None:
        key = sample_price_key(variant="reverse_holo")
        evaluated = filter_comps(
            key,
            [
                sold_comp_prices("Charizard Base Set 4 Reverse Holo", 20, 0, source_listing_id="reverse"),
                sold_comp_prices("Charizard Base Set 4 Common", 10, 0, source_listing_id="plain"),
            ],
        )
        reasons = {item.comp.source_listing_id: item.rejection_reason for item in evaluated}
        self.assertIsNone(reasons["reverse"])
        self.assertEqual(reasons["plain"], "weak_variant_match")

    def test_holo_rejects_reverse_holo(self) -> None:
        evaluated = filter_comps(
            sample_price_key(variant="holo"),
            [sold_comp_prices("Charizard Base Set 4 Reverse Holo", 20, 0)],
        )
        self.assertEqual(evaluated[0].rejection_reason, "wrong_variant_reverse_holo")

    def test_raw_keeps_generic_variant_behavior(self) -> None:
        evaluated = filter_comps(
            sample_price_key(variant="raw"),
            [sold_comp_prices("Charizard Base Set 4 Reverse Holo", 20, 0)],
        )
        self.assertTrue(evaluated[0].included_in_estimate)
        self.assertEqual(evaluated[0].comp.raw_metadata["detected_variant"], "reverse_holo")


if __name__ == "__main__":
    unittest.main()
