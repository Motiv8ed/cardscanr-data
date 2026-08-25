from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.catalogue_identity import (
    compact_set_search_name,
    is_internal_catalogue_collector_number,
    is_internal_set_code,
    searchable_collector_number,
    searchable_set_label,
)
from cardscanr_market_engine.filters import filter_comps
from cardscanr_market_engine.models import MarketPriceKey, SoldComp
from cardscanr_market_engine.providers.query_builder import build_provider_search_queries
from cardscanr_market_engine.models import ProviderRequest
from cardscanr_market_engine.marketplaces import resolve_marketplace_config


def _sold(title: str, *, listing_id: str = "1", currency: str = "USD") -> SoldComp:
    return SoldComp(
        source_listing_id=listing_id,
        title=title,
        sold_price=4.0,
        shipping_price=1.0,
        total_price=5.0,
        currency=currency,
        sold_date=datetime(2026, 5, 20, tzinfo=timezone.utc),
        listing_url=f"https://example.test/{listing_id}",
        condition_text="Raw",
    )


def _asia_key(
    *,
    card_name: str = "Bulbasaur",
    normalized_card_name: str = "bulbasaur",
    set_name: str = "Mega Evolution Mega Evolution Release Date 09-26-2025",
    set_code: str = "pokemon-asia-my-official:set:me01",
    collector_number: str = "21593",
) -> MarketPriceKey:
    return MarketPriceKey(
        id="key-asia",
        game="pokemon",
        card_name=card_name,
        normalized_card_name=normalized_card_name,
        set_name=set_name,
        set_code=set_code,
        collector_number=collector_number,
        language="en",
        variant="raw",
        condition="raw",
        market_country="au",
        currency="aud",
        fingerprint="asia-test",
    )


class CatalogueIdentityTests(unittest.TestCase):
    def test_internal_set_and_collector_detection(self) -> None:
        self.assertTrue(is_internal_set_code("pokemon-asia-my-official:set:me01"))
        self.assertTrue(is_internal_set_code("24173"))
        self.assertFalse(is_internal_set_code("sv09"))
        self.assertTrue(is_internal_catalogue_collector_number("21593", set_code="pokemon-asia-my-official:set:me01"))
        self.assertFalse(is_internal_catalogue_collector_number("043/100", set_code="sv09"))

    def test_searchable_labels_skip_internal_ids(self) -> None:
        self.assertEqual(searchable_collector_number("21593", set_code="pokemon-asia-my-official:set:me01"), "")
        self.assertIn("mega evolution", searchable_set_label(
            "Mega Evolution Mega Evolution Release Date 09-26-2025",
            "pokemon-asia-my-official:set:me01",
        ).lower())
        self.assertEqual(compact_set_search_name("Battle Partners Release Date 01-01-2025"), "battle partners")


class PricingEvidenceRepairTests(unittest.TestCase):
    def test_asia_catalogue_card_accepts_name_and_set_match_without_internal_number(self) -> None:
        evaluated = filter_comps(
            _asia_key(),
            [
                _sold(
                    "Bulbasaur 001/132 Mega Evolution Pokemon Card English",
                    listing_id="ok",
                    currency="AUD",
                )
            ],
        )
        self.assertTrue(evaluated[0].included_in_estimate, evaluated[0].rejection_reason)

    def test_asia_card_allows_japanese_listing_language(self) -> None:
        evaluated = filter_comps(
            _asia_key(card_name="Pikachu", normalized_card_name="pikachu"),
            [_sold("Pikachu 025 Pokemon Japanese Celebrations Holo", listing_id="jp", currency="AUD")],
        )
        self.assertNotEqual(evaluated[0].rejection_reason, "wrong_language")

    def test_charizard_gx_hyphen_name_matches_space_form(self) -> None:
        key = MarketPriceKey(
            id="gx",
            game="pokemon",
            card_name="Charizard-GX",
            normalized_card_name="charizard_gx",
            set_name="Hidden Fates",
            set_code="sm115",
            collector_number="9",
            language="en",
            variant="non_holo",
            condition="raw",
            market_country="us",
            currency="usd",
            fingerprint="gx-test",
        )
        evaluated = filter_comps(
            key,
            [_sold("Charizard GX SM115 9/68 Holo Pokemon Card", listing_id="gx")],
        )
        self.assertTrue(evaluated[0].included_in_estimate, evaluated[0].rejection_reason)

    def test_zero_padded_collector_number_matches(self) -> None:
        key = MarketPriceKey(
            id="pad",
            game="pokemon",
            card_name="Lillie's Comfey",
            normalized_card_name="lillie_s_comfey",
            set_name="Battle Partners",
            set_code="24173",
            collector_number="043/100",
            language="jp",
            variant="non_holo",
            condition="raw",
            market_country="au",
            currency="aud",
            fingerprint="pad-test",
        )
        evaluated = filter_comps(
            key,
            [_sold("Lillie's Comfey 43/100 Battle Partners Japanese Pokemon Card", listing_id="jp", currency="AUD")],
        )
        self.assertTrue(evaluated[0].included_in_estimate, evaluated[0].rejection_reason)

    def test_wrong_collector_number_still_rejected_for_real_numbers(self) -> None:
        key = MarketPriceKey(
            id="real",
            game="pokemon",
            card_name="Golbat",
            normalized_card_name="golbat",
            set_name="Chaos Rising",
            set_code="me4",
            collector_number="050/086",
            language="en",
            variant="non_holo",
            condition="raw",
            market_country="au",
            currency="aud",
            fingerprint="real-test",
        )
        evaluated = filter_comps(key, [_sold("Golbat 051/086 Chaos Rising Pokemon", listing_id="bad", currency="AUD")])
        self.assertEqual(evaluated[0].rejection_reason, "wrong_collector_number")

    def test_sealed_product_rejected(self) -> None:
        evaluated = filter_comps(_asia_key(), [_sold("Bulbasaur Mega Evolution booster pack sealed", currency="AUD")])
        self.assertEqual(evaluated[0].rejection_reason, "sealed_product_for_single_card_request")

    def test_query_builder_omits_internal_catalogue_number(self) -> None:
        key = _asia_key()
        market = resolve_marketplace_config(
            market_country=key.market_country,
            currency=key.currency,
            marketplace="ebay",
        )
        request = ProviderRequest(
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
        queries = build_provider_search_queries(request)
        self.assertTrue(queries)
        joined = " || ".join(query.query_text for query in queries)
        self.assertNotIn("21593", joined)
        self.assertNotIn("POKEMON-ASIA", joined.upper())
        self.assertIn("mega evolution", joined.lower())


if __name__ == "__main__":
    unittest.main()
