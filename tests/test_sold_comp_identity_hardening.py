"""Hardened sold-comp identity filter regression tests."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.filters import filter_comps
from cardscanr_market_engine.models import MarketPriceKey, SoldComp
from cardscanr_market_engine.price_movement_guard import evaluate_price_movement
from cardscanr_market_engine.pricing_stats import determine_confidence


def _key(**overrides) -> MarketPriceKey:
    base = dict(
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
        market_country="au",
        currency="aud",
        fingerprint="pokemon|en|base1|4|charizard|raw|near_mint|au|aud",
    )
    base.update(overrides)
    return MarketPriceKey(**base)


def _comp(title: str, price: float = 20.0, **meta) -> SoldComp:
    return SoldComp(
        source_listing_id=meta.pop("source_listing_id", "listing-1"),
        title=title,
        sold_price=price,
        shipping_price=0.0,
        total_price=price,
        currency=meta.pop("currency", "AUD"),
        sold_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        listing_url="https://www.ebay.com.au/itm/1",
        condition_text=meta.pop("condition_text", "Raw"),
        raw_metadata=meta.pop("raw_metadata", {}),
    )


class CollectorNumberHardeningTests(unittest.TestCase):
    def test_substring_collision_4_not_40(self) -> None:
        evaluated = filter_comps(_key(), [_comp("Charizard Base Set 40 raw")])
        self.assertEqual(evaluated[0].rejection_reason, "wrong_collector_number")

    def test_substring_collision_4_not_104(self) -> None:
        evaluated = filter_comps(_key(), [_comp("Charizard Base Set 104/102 raw")])
        self.assertEqual(evaluated[0].rejection_reason, "wrong_collector_number")

    def test_hash_330_does_not_match_requested_1(self) -> None:
        key = _key(card_name="Pikachu", normalized_card_name="pikachu", set_name="30th Celebration", set_code="me55", collector_number="1")
        evaluated = filter_comps(key, [_comp("Pikachu #330 Ice Holo Pokemon Vintage Vending Sticker")])
        self.assertIn(evaluated[0].rejection_reason, {"wrong_collector_number", "non_comparable_product", "wrong_set", "weak_set_identity"})

    def test_full_number_accepted(self) -> None:
        evaluated = filter_comps(_key(collector_number="4/102"), [_comp("Charizard 4/102 Base Set Pokemon raw")])
        self.assertTrue(evaluated[0].included_in_estimate)


class SetLanguageGradedLotTests(unittest.TestCase):
    def test_wrong_set_celebrations_for_base(self) -> None:
        evaluated = filter_comps(
            _key(),
            [_comp("Pokemon TCG Charizard 4/102 - 25th Anniversary Celebrations Metal Gold Card - NM")],
        )
        self.assertEqual(evaluated[0].rejection_reason, "wrong_set")

    def test_french_rejected_for_english(self) -> None:
        evaluated = filter_comps(
            _key(collector_number="58", card_name="Pikachu", normalized_card_name="pikachu"),
            [_comp("Pikachu 58/102 Pokemon TCG Base Set 1st Edition FRENCH LP")],
        )
        self.assertEqual(evaluated[0].rejection_reason, "wrong_language")

    def test_nm_raw_not_rejected_as_graded(self) -> None:
        evaluated = filter_comps(_key(), [_comp("Charizard Base Set 4/102 NM raw")])
        self.assertTrue(evaluated[0].included_in_estimate)

    def test_psa_graded_rejected(self) -> None:
        evaluated = filter_comps(_key(), [_comp("Charizard Base Set 4 PSA 10 Gem Mint")])
        self.assertEqual(evaluated[0].rejection_reason, "graded_for_raw_request")

    def test_lot_rejected(self) -> None:
        evaluated = filter_comps(_key(), [_comp("Charizard Base Set 4 lot of 3 cards")])
        self.assertEqual(evaluated[0].rejection_reason, "likely_bundle_lot")

    def test_buy2_get1_rejected(self) -> None:
        evaluated = filter_comps(
            _key(card_name="Pikachu", normalized_card_name="pikachu", collector_number="58"),
            [_comp("Pikachu 58/102 Base Set BUY 2 CARDS GET 1 FREE")],
        )
        self.assertEqual(evaluated[0].rejection_reason, "likely_bundle_lot")

    def test_sealed_rejected(self) -> None:
        evaluated = filter_comps(_key(), [_comp("Charizard Base Set 4 booster pack sealed")])
        self.assertEqual(evaluated[0].rejection_reason, "sealed_product_for_single_card_request")

    def test_ambiguous_preowned_title_rejected(self) -> None:
        evaluated = filter_comps(_key(), [_comp("Pre-owned")])
        self.assertIn(evaluated[0].rejection_reason, {"ambiguous_title", "wrong_collector_number"})
        self.assertFalse(evaluated[0].included_in_estimate)

    def test_best_offer_chrome_title_rejected(self) -> None:
        evaluated = filter_comps(_key(), [_comp("Best offer accepted")])
        self.assertFalse(evaluated[0].included_in_estimate)
        self.assertIn(
            evaluated[0].rejection_reason,
            {"sold_price_obscured", "ambiguous_title", "wrong_collector_number"},
        )

    def test_sold_price_obscured_rejected(self) -> None:
        evaluated = filter_comps(
            _key(),
            [_comp("Charizard Base Set 4/102", raw_metadata={"soldPriceObscured": True})],
        )
        self.assertEqual(evaluated[0].rejection_reason, "sold_price_obscured")

    def test_30th_celebration_rejected_for_base_set(self) -> None:
        evaluated = filter_comps(
            _key(card_name="Pikachu", normalized_card_name="pikachu", collector_number="58/102"),
            [_comp("Pokemon PIKACHU 58/102 - 30th Celebration - HOLO - MINT")],
        )
        self.assertEqual(evaluated[0].rejection_reason, "wrong_set")

    def test_ja_does_not_accept_english_title(self) -> None:
        key = _key(language="ja", card_name="ピカチュウ", normalized_card_name="pikachu", set_code="xy1", set_name="Collection X", collector_number="26")
        evaluated = filter_comps(key, [_comp("Pikachu 26/146 XY English Base")])
        self.assertEqual(evaluated[0].rejection_reason, "wrong_language")


class ConfidenceAndCacheSafetyTests(unittest.TestCase):
    def test_strict_confidence_requires_quality(self) -> None:
        self.assertEqual(determine_confidence(included_count=5, average_match_score=0.5, strong_exact_count=5), "low")
        self.assertEqual(determine_confidence(included_count=5, average_match_score=0.8, strong_exact_count=5), "medium")
        self.assertEqual(determine_confidence(included_count=8, average_match_score=0.9, strong_exact_count=8), "high")

    def test_prior_strong_estimate_retained_on_weak_refresh(self) -> None:
        decision = evaluate_price_movement(
            old_price=74.0,
            new_price=6.0,
            included_count=1,
            confidence="low",
            prior_confidence="medium",
            prior_included_count=5,
        )
        self.assertEqual(decision.action, "reject_weak")
        self.assertEqual(decision.reason, "preserve_prior_stronger_than_weak_refresh")


if __name__ == "__main__":
    unittest.main()
