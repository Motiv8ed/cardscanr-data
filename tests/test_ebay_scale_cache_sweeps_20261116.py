"""eBay AU scale: cross-user cache, sold URL, row parser, set sweeps (fixture-only)."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.ebay_scale.config import EbayScaleFlags
from cardscanr_market_engine.ebay_scale.cross_user_cache import (
    cross_user_cache_key,
    evaluate_cross_user_cache_for_job,
    freshness_window_days,
    is_price_fresh_for_value,
    normalize_condition_band,
)
from cardscanr_market_engine.ebay_scale.offline_replay import offline_five_set_throughput_estimate
from cardscanr_market_engine.ebay_scale.queue_priority import (
    cross_user_dedupe_work_items,
    score_queue_priority,
)
from cardscanr_market_engine.ebay_scale.set_sweep import median_prices_by_card, run_set_sweep
from cardscanr_market_engine.ebay_scale.set_sweep_matcher import (
    SetSweepCatalogueCard,
    match_sold_row_to_catalogue,
)
from cardscanr_market_engine.ebay_scale.sold_results_url import build_au_sold_results_url
from cardscanr_market_engine.ebay_scale.sold_row_html import SoldResultsRow, parse_sold_results_html
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest
from cardscanr_market_engine.providers.query_builder import build_provider_search_queries

FIXTURES = ROOT / "tests" / "fixtures" / "ebay_scale"
BLOODMOON_FIXTURE = ROOT / "tests" / "fixtures" / "ebay_sold" / "bloodmoon_ursaluna_54_sold_fixture.html"


def _buggy_sweep_match_name_set_only(
    row: SoldResultsRow,
    *,
    catalogue: list[SetSweepCatalogueCard],
    index: int = 0,
) -> SetSweepMatch | None:
    """Pre-number_gate matcher: name + set only (false-positives on wrong/missing numbers)."""
    from cardscanr_market_engine.ebay_scale.set_sweep_matcher import (
        SetSweepMatch,
        _price_key_for_card,
        _set_identity_match_info,
        _sold_comp_from_row,
    )
    from cardscanr_market_engine.filters import _reject_reason
    from cardscanr_market_engine.fingerprints import normalize_name, normalize_text

    normalized_title = normalize_text(row.title)
    candidates: list[tuple[SetSweepCatalogueCard, str]] = []
    for card in catalogue:
        price_key = _price_key_for_card(card)
        reject = _reject_reason(price_key, _sold_comp_from_row(row, index=index))
        if reject:
            continue
        name_ok = normalize_name(card.normalized_card_name).replace("_", " ") in normalized_title or (
            normalize_name(card.card_name).replace("_", " ") in normalized_title
        )
        set_info = _set_identity_match_info(price_key, normalized_title)
        set_ok = bool(set_info.get("matches"))
        if name_ok and set_ok:
            candidates.append((card, "high"))
        elif name_ok:
            candidates.append((card, "ambiguous"))

    if not candidates:
        return None
    if len({c[0].catalogue_card_id for c in candidates}) > 1:
        return None
    card, confidence = candidates[0]
    return SetSweepMatch(
        catalogue_card_id=card.catalogue_card_id,
        card_name=card.card_name,
        collector_number=card.collector_number,
        confidence=confidence if confidence == "high" else "ambiguous",
    )


class SoldResultsUrlTests(unittest.TestCase):
    def test_direct_sold_url_shape(self) -> None:
        url = build_au_sold_results_url(query_text="Pidgey 162 obsidian flames Pokemon")
        self.assertTrue(url.startswith("https://www.ebay.com.au/"))
        self.assertIn("LH_Sold=1", url)
        self.assertIn("LH_Complete=1", url)
        self.assertIn("_ipg=240", url)
        self.assertIn("_sacat=0", url)
        self.assertNotIn("http://", url)

    def test_non_au_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_au_sold_results_url(query_text="x", market_country="US")


class CrossUserCacheTests(unittest.TestCase):
    def test_freshness_windows(self) -> None:
        self.assertEqual(freshness_window_days(aud_value=60), 3)
        self.assertEqual(freshness_window_days(aud_value=12), 7)
        self.assertEqual(freshness_window_days(aud_value=2), 30)

    def test_cache_key_dedupes_users(self) -> None:
        key_a = cross_user_cache_key(
            catalogue_card_id="card-1",
            fingerprint="fp-a",
            language="en",
            condition_band=normalize_condition_band("raw"),
        )
        key_b = cross_user_cache_key(
            catalogue_card_id="card-1",
            fingerprint="fp-b",
            language="en",
            condition_band=normalize_condition_band("raw"),
        )
        self.assertEqual(key_a, key_b)

    def test_fresh_skip_when_flag_on(self) -> None:
        now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        with mock.patch.dict(
            os.environ,
            {"EBAY_SCALE_ENABLED": "true", "EBAY_SCALE_CROSS_USER_CACHE": "true"},
            clear=False,
        ):
            decision = evaluate_cross_user_cache_for_job(
                target={
                    "catalogue_card_id": "c1",
                    "fingerprint": "f1",
                    "language": "en",
                    "condition": "raw",
                    "market_country": "AU",
                },
                prior_cache={
                    "current_market_price": 55,
                    "last_updated_at": (now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                },
                now=now,
            )
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertTrue(decision.skip_job)

    def test_flag_off_no_skip(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("EBAY_SCALE_ENABLED", None)
            decision = evaluate_cross_user_cache_for_job(
                target={"fingerprint": "f1", "market_country": "AU"},
                prior_cache={"current_market_price": 99, "last_updated_at": "2026-11-15T00:00:00Z"},
                now=datetime(2026, 11, 16, tzinfo=timezone.utc),
            )
        self.assertIsNone(decision)


class QueuePriorityTests(unittest.TestCase):
    def test_sell_trade_beats_bulk(self) -> None:
        now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        sell = score_queue_priority(
            target={"fingerprint": "a", "listed_for_sale": True, "market_country": "AU"},
            now=now,
        )
        bulk = score_queue_priority(
            target={"fingerprint": "b", "market_country": "AU"},
            now=now,
        )
        self.assertLess(sell.priority_score, bulk.priority_score)

    def test_cross_user_dedupe_keeps_one_per_card(self) -> None:
        now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        items = [
            {
                "price_key_id": "k1",
                "target": {
                    "fingerprint": "f1",
                    "catalogue_card_id": "same",
                    "language": "en",
                    "condition": "raw",
                    "market_country": "AU",
                },
            },
            {
                "price_key_id": "k2",
                "target": {
                    "fingerprint": "f2",
                    "catalogue_card_id": "same",
                    "language": "en",
                    "condition": "raw",
                    "market_country": "AU",
                },
            },
        ]
        with mock.patch.dict(
            os.environ,
            {"EBAY_SCALE_ENABLED": "true", "EBAY_SCALE_VALUE_FIRST_QUEUE": "true"},
            clear=False,
        ):
            deduped = cross_user_dedupe_work_items(items, now=now)
        self.assertEqual(len(deduped), 1)


class SoldRowParserTests(unittest.TestCase):
    def test_parses_every_bloodmoon_row(self) -> None:
        html = BLOODMOON_FIXTURE.read_text(encoding="utf-8")
        rows = parse_sold_results_html(html)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0].title, "Bloodmoon Ursaluna 054/131 Prismatic Evolutions Pokemon Card")
        self.assertEqual(rows[0].sold_price, 2.80)
        self.assertEqual(rows[0].postage, 12.0)
        self.assertIn("28 Sep 2026", rows[0].sold_date)

    def test_set_sweep_fixture_rows(self) -> None:
        html = (FIXTURES / "sv3_set_sweep_fixture.html").read_text(encoding="utf-8")
        rows = parse_sold_results_html(html)
        self.assertEqual(len(rows), 6)
        self.assertTrue(rows[0].best_offer)


class SetSweepMatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalogue = [
            SetSweepCatalogueCard(
                catalogue_card_id="charizard-ex",
                card_name="Charizard ex",
                normalized_card_name="charizard ex",
                set_name="Obsidian Flames",
                set_code="sv3",
                collector_number="199/197",
            ),
            SetSweepCatalogueCard(
                catalogue_card_id="pidgey",
                card_name="Pidgey",
                normalized_card_name="pidgey",
                set_name="Obsidian Flames",
                set_code="sv3",
                collector_number="162/197",
            ),
        ]

    def test_matches_high_value_card(self) -> None:
        html = (FIXTURES / "sv3_set_sweep_fixture.html").read_text(encoding="utf-8")
        rows = parse_sold_results_html(html)
        match = match_sold_row_to_catalogue(rows[0], catalogue=self.catalogue)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.catalogue_card_id, "charizard-ex")
        self.assertEqual(match.confidence, "high")

    def test_rejects_lot_and_choose_your_card(self) -> None:
        html = (FIXTURES / "sv3_set_sweep_fixture.html").read_text(encoding="utf-8")
        rows = parse_sold_results_html(html)
        self.assertIsNone(match_sold_row_to_catalogue(rows[2], catalogue=self.catalogue))
        self.assertIsNone(match_sold_row_to_catalogue(rows[4], catalogue=self.catalogue))

    def test_revert_proof_number_required(self) -> None:
        """Revert: without collector-number matching, name+set rows false-positive."""
        html = (FIXTURES / "sv3_set_sweep_fixture.html").read_text(encoding="utf-8")
        rows = parse_sold_results_html(html)
        row = rows[5]  # single card: name+set match, title lacks full N/M
        self.assertEqual(row.title, "Pidgey 162 Obsidian Flames Pokemon")
        self.assertIsNone(match_sold_row_to_catalogue(row, catalogue=self.catalogue))
        self.assertIsNotNone(_buggy_sweep_match_name_set_only(row, catalogue=self.catalogue))


class SetSweepRunTests(unittest.TestCase):
    def test_sweep_stats(self) -> None:
        catalogue = [
            SetSweepCatalogueCard(
                catalogue_card_id="charizard-ex",
                card_name="Charizard ex",
                normalized_card_name="charizard ex",
                set_name="Obsidian Flames",
                set_code="sv3",
                collector_number="199/197",
            ),
            SetSweepCatalogueCard(
                catalogue_card_id="pidgey",
                card_name="Pidgey",
                normalized_card_name="pidgey",
                set_name="Obsidian Flames",
                set_code="sv3",
                collector_number="162/197",
            ),
        ]
        html = (FIXTURES / "sv3_set_sweep_fixture.html").read_text(encoding="utf-8")
        stats, _ = run_set_sweep(html=html, catalogue=catalogue)
        self.assertEqual(stats.rows_parsed, 6)
        self.assertGreaterEqual(stats.rows_matched, 2)
        lot_reasons = {"lot_or_bundle", "likely_bundle_lot"}
        self.assertTrue(lot_reasons & set(stats.rows_rejected))
        medians = median_prices_by_card(stats)
        self.assertIn("charizard-ex", medians)
        self.assertEqual(stats.card_price_labels["charizard-ex"], "exact_per_card_search_required")


class QueryBuilderDirectUrlTests(unittest.TestCase):
    def _request(self) -> ProviderRequest:
        key = MarketPriceKey(
            id="k1",
            game="pokemon",
            card_name="Pidgey",
            normalized_card_name="pidgey",
            set_name="Obsidian Flames",
            set_code="sv3",
            collector_number="162/197",
            language="en",
            variant="raw",
            condition="raw",
            market_country="AU",
            currency="AUD",
            fingerprint="fp",
        )
        config = resolve_marketplace_config(market_country="AU", currency="AUD", marketplace="ebay")
        return ProviderRequest(
            price_key=key,
            market_country=config.market_country,
            currency=config.currency,
            marketplace=config.marketplace,
            provider_marketplace_id=config.provider_marketplace_id,
            provider_domain=config.provider_domain,
            search_locale=config.search_locale,
            display_name=config.display_name,
            market_config=config,
        )

    def test_direct_url_only_when_flag_on(self) -> None:
        req = self._request()
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("EBAY_SCALE_ENABLED", None)
            queries = build_provider_search_queries(req, max_attempts=1)
            self.assertNotIn("LH_Sold=1", queries[0].search_url)
        with mock.patch.dict(
            os.environ,
            {"EBAY_SCALE_ENABLED": "true", "EBAY_SCALE_DIRECT_SOLD_URL": "true"},
            clear=False,
        ):
            queries = build_provider_search_queries(req, max_attempts=1)
            self.assertIn("LH_Sold=1", queries[0].search_url)
            self.assertIn("_ipg=240", queries[0].search_url)


class OfflineReplayTests(unittest.TestCase):
    def test_five_set_estimate_unverified(self) -> None:
        payload = offline_five_set_throughput_estimate()
        self.assertEqual(payload["label"], "UNVERIFIED")
        self.assertEqual(len(payload["sets"]), 5)


class FlagsDefaultOffTests(unittest.TestCase):
    def test_defaults(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            for name in (
                "EBAY_SCALE_ENABLED",
                "EBAY_SCALE_CROSS_USER_CACHE",
                "EBAY_SCALE_VALUE_FIRST_QUEUE",
                "EBAY_SCALE_DIRECT_SOLD_URL",
                "EBAY_SCALE_SET_SWEEP",
            ):
                os.environ.pop(name, None)
            flags = EbayScaleFlags.from_env()
        self.assertFalse(flags.enabled)
        self.assertFalse(flags.cross_user_cache)
        self.assertFalse(flags.direct_sold_results_url)


if __name__ == "__main__":
    unittest.main()