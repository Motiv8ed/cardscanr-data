"""Regression tests for JP identity resolution and scheduler starvation backoff."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.failure_policy import (
    FAILURE_CLASS_IDENTITY,
    FAILURE_CLASS_TRANSIENT,
    build_failure_policy,
    classify_pricing_failure,
)
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest
from cardscanr_market_engine.marketplaces import resolve_marketplace_config
from cardscanr_market_engine.providers.errors import (
    ProviderIdentityUnavailableError,
    ProviderTemporaryError,
)
from cardscanr_market_engine.providers.identity_guard import (
    ENGLISH_MARKET_IDENTITY_UNAVAILABLE,
    evaluate_english_market_identity,
    normalize_pokemon_language_tag,
)
from cardscanr_market_engine.providers.query_builder import build_provider_search_queries
from cardscanr_market_engine.scheduler import MarketPriceRefreshScheduler
from cardscanr_market_engine.species_names import resolve_english_species_name
from tests.test_market_engine_scheduler import FakeSchedulerClient, fixed_config, iso


NOW = datetime(2026, 8, 26, 2, 0, tzinfo=timezone.utc)

# CS-012e D2 golden keys (from D2_IDENTITY_MISS_LIST.json)
D2_GOLDEN_CASES = (
    {
        "fingerprint": "pokemon|jp|sv09|085/100|unknown|non_holo|raw|au|aud",
        "card_name": "ホップのウールー",
        "language": "jp",
        "set_code": "sv09",
        "collector_number": "085/100",
        "expect_en": "Wooloo",
        "expect_blocked": False,
    },
    {
        "fingerprint": "pokemon|jp|sv9|092|_|raw|raw|au|aud",
        "card_name": "ホップのこだわりハチマキ",
        "language": "ja",
        "set_code": "sv9",
        "collector_number": "092",
        "expect_en": None,
        "expect_blocked": True,
    },
    {
        "fingerprint": "pokemon|jp|sv9|092/100|_|non_holo|raw|au|aud",
        "card_name": "ホップのこだわりハチマキ",
        "language": "ja",
        "set_code": "sv9",
        "collector_number": "092/100",
        "expect_en": None,
        "expect_blocked": True,
    },
    {
        "fingerprint": "pokemon|jp|sv9|043|_|non_holo|raw|au|aud",
        "card_name": "リーリエのキュワワー",
        "language": "ja",
        "set_code": "sv9",
        "collector_number": "043",
        "expect_en": "Comfey",
        "expect_blocked": False,
    },
    {
        "fingerprint": "pokemon|jp|sv9|040/100|n_|non_holo|raw|au|aud",
        "card_name": "Nのシンボラー",
        "language": "ja",
        "set_code": "sv9",
        "collector_number": "040/100",
        "expect_en": "Sigilyph",
        "expect_blocked": False,
    },
    {
        "fingerprint": "pokemon|jp|sv8a|217/187|_ex|reverse_holo|raw|au|aud",
        "card_name": "ブラッキーex",
        "language": "ja",
        "set_code": "sv8a",
        "collector_number": "217/187",
        "expect_en": "Umbreon ex",
        "expect_blocked": False,
    },
    {
        "fingerprint": "pokemon|jp|sv9|085/100|_|non_holo|raw|au|aud",
        "card_name": "ホップのウールー",
        "language": "ja",
        "set_code": "sv9",
        "collector_number": "085/100",
        "expect_en": "Wooloo",
        "expect_blocked": False,
    },
)


def _jp_key(
    *,
    card_name: str,
    collector_number: str,
    normalized_card_name: str = "unknown",
    canonical_name_en: str | None = None,
    aliases: object = None,
    language: str = "jp",
    set_code: str = "sv09",
    fingerprint: str = "jp-test",
) -> MarketPriceKey:
    raw = {
        "canonical_name_en": canonical_name_en,
        "aliases": aliases if aliases is not None else [],
        "language": language,
    }
    return MarketPriceKey(
        id="jp-key",
        game="pokemon",
        card_name=card_name,
        normalized_card_name=normalized_card_name,
        set_name="",
        set_code=set_code,
        collector_number=collector_number,
        language=language,
        variant="raw",
        condition="raw",
        market_country="au",
        currency="aud",
        fingerprint=fingerprint,
        raw=raw,
    )


def _request(key: MarketPriceKey) -> ProviderRequest:
    market = resolve_marketplace_config(
        market_country=key.market_country,
        currency=key.currency,
        marketplace="ebay",
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


class SpeciesNameResolutionTests(unittest.TestCase):
    def test_kurimugan_and_hassuburero_resolve(self) -> None:
        self.assertEqual(resolve_english_species_name("クリムガン"), "Druddigon")
        self.assertEqual(resolve_english_species_name("ハスブレロ"), "Lombre")

    def test_unknown_and_empty_rejected(self) -> None:
        self.assertIsNone(resolve_english_species_name("unknown"))
        self.assertIsNone(resolve_english_species_name(""))
        self.assertIsNone(resolve_english_species_name("完全に未知の名前xyz"))

    def test_possessive_trainer_forms_resolve(self) -> None:
        self.assertEqual(resolve_english_species_name("ホップのウールー"), "Wooloo")
        self.assertEqual(resolve_english_species_name("リーリエのキュワワー"), "Comfey")
        self.assertEqual(resolve_english_species_name("Nのシンボラー"), "Sigilyph")
        self.assertEqual(resolve_english_species_name("ブラッキーex"), "Umbreon ex")

    def test_item_card_without_species_stays_unresolved(self) -> None:
        self.assertIsNone(resolve_english_species_name("ホップのこだわりハチマキ"))


class JpIdentityGuardTests(unittest.TestCase):
    def test_species_map_unblocks_english_market_search(self) -> None:
        key = _jp_key(card_name="クリムガン", collector_number="073")
        result = evaluate_english_market_identity(_request(key))
        self.assertFalse(result.blocked)
        self.assertEqual(result.search_card_name, "Druddigon")
        self.assertEqual(result.search_name_source, "species_names_ja_en")

        queries = build_provider_search_queries(_request(key), max_attempts=3)
        self.assertTrue(queries)
        self.assertIn("Druddigon", queries[0].query_text)
        self.assertNotIn("クリムガン", queries[0].query_text)
        self.assertNotIn("unknown", queries[0].query_text.lower())

    def test_canonical_name_en_preferred(self) -> None:
        key = _jp_key(
            card_name="ハスブレロ",
            collector_number="022",
            canonical_name_en="Lombre",
        )
        result = evaluate_english_market_identity(_request(key))
        self.assertFalse(result.blocked)
        self.assertEqual(result.search_card_name, "Lombre")
        self.assertEqual(result.search_name_source, "raw.canonical_name_en")

    def test_generic_unknown_alias_ignored(self) -> None:
        key = _jp_key(
            card_name="未知カード",
            collector_number="999",
            aliases=["unknown"],
        )
        # Force no species map hit by using a nonsense JP name.
        result = evaluate_english_market_identity(_request(key))
        self.assertTrue(result.blocked)
        self.assertEqual(result.reason, ENGLISH_MARKET_IDENTITY_UNAVAILABLE)

    def test_safe_alias_used(self) -> None:
        key = _jp_key(
            card_name="ピカチュウ",
            collector_number="025",
            aliases={"en": "Pikachu"},
        )
        result = evaluate_english_market_identity(_request(key))
        self.assertFalse(result.blocked)
        self.assertEqual(result.search_card_name, "Pikachu")

    def test_jp_and_ja_language_tags_normalize_identically(self) -> None:
        self.assertEqual(normalize_pokemon_language_tag("jp"), "ja")
        self.assertEqual(normalize_pokemon_language_tag("ja"), "ja")
        self.assertEqual(normalize_pokemon_language_tag("JP"), "ja")
        jp_key = _jp_key(card_name="ホップのウールー", collector_number="085/100", language="jp")
        ja_key = _jp_key(card_name="ホップのウールー", collector_number="085/100", language="ja")
        jp_result = evaluate_english_market_identity(_request(jp_key))
        ja_result = evaluate_english_market_identity(_request(ja_key))
        self.assertFalse(jp_result.blocked)
        self.assertFalse(ja_result.blocked)
        self.assertEqual(jp_result.search_card_name, ja_result.search_card_name)
        self.assertEqual(jp_result.diagnostics["languageNormalized"], "ja")
        self.assertEqual(ja_result.diagnostics["languageNormalized"], "ja")


class D2IdentityGoldenPathTests(unittest.TestCase):
    """Golden coverage for the 7 CS-012e D2 english_market_identity_unavailable keys."""

    def test_all_seven_d2_fingerprints(self) -> None:
        self.assertEqual(len(D2_GOLDEN_CASES), 7)
        for case in D2_GOLDEN_CASES:
            with self.subTest(fingerprint=case["fingerprint"]):
                key = _jp_key(
                    card_name=case["card_name"],
                    collector_number=case["collector_number"],
                    language=case["language"],
                    set_code=case["set_code"],
                    fingerprint=case["fingerprint"],
                )
                result = evaluate_english_market_identity(_request(key))
                self.assertEqual(result.diagnostics["languageNormalized"], "ja")
                if case["expect_blocked"]:
                    self.assertTrue(result.blocked)
                    self.assertEqual(result.reason, ENGLISH_MARKET_IDENTITY_UNAVAILABLE)
                    self.assertIsNone(resolve_english_species_name(case["card_name"]))
                else:
                    self.assertFalse(result.blocked)
                    self.assertIsNone(result.reason)
                    self.assertEqual(result.search_card_name, case["expect_en"])
                    self.assertEqual(resolve_english_species_name(case["card_name"]), case["expect_en"])


class FailureBackoffTests(unittest.TestCase):
    def test_identity_failure_classification_and_backoff(self) -> None:
        exc = ProviderIdentityUnavailableError(ENGLISH_MARKET_IDENTITY_UNAVAILABLE)
        self.assertEqual(classify_pricing_failure(exc), FAILURE_CLASS_IDENTITY)
        first = build_failure_policy(exc, now=NOW, consecutive_same_failures=1)
        second = build_failure_policy(exc, now=NOW, consecutive_same_failures=2)
        third = build_failure_policy(exc, now=NOW, consecutive_same_failures=3)
        self.assertFalse(first.retryable)
        self.assertEqual(first.backoff, timedelta(hours=6))
        self.assertEqual(second.backoff, timedelta(hours=24))
        self.assertEqual(third.backoff, timedelta(days=3))
        self.assertEqual(first.next_refresh_due_at, NOW + timedelta(hours=6))

    def test_transient_failure_shorter_backoff(self) -> None:
        exc = ProviderTemporaryError("timeout talking to ebay")
        self.assertEqual(classify_pricing_failure(exc), FAILURE_CLASS_TRANSIENT)
        policy = build_failure_policy(exc, now=NOW, consecutive_same_failures=1)
        self.assertTrue(policy.retryable)
        self.assertEqual(policy.backoff, timedelta(minutes=30))


class SchedulerStarvationBackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        # Isolate from any live marketplace cooldown artifacts on disk/env.
        self._cooldown_patch = patch(
            "cardscanr_market_engine.scheduler.get_active_cooldown",
            return_value=None,
        )
        self._cooldown_patch.start()
        self.addCleanup(self._cooldown_patch.stop)

    def test_identity_failure_backoff_excludes_key(self) -> None:
        due_future = iso(NOW + timedelta(hours=6))
        client = FakeSchedulerClient(
            stale_rows=[
                {
                    "id": "jp-failed",
                    "fingerprint": "fp-jp",
                    "market_country": "AU",
                    "currency": "AUD",
                    "current_market_price": None,
                    "recommended_price": None,
                    "next_refresh_due_at": due_future,
                    "stale_after": due_future,
                    "refresh_status": "failed",
                    "last_error_message": ENGLISH_MARKET_IDENTITY_UNAVAILABLE,
                    "last_updated_at": iso(NOW - timedelta(minutes=5)),
                    "popularity_score": 0,
                    "inventory_count": 0,
                    "last_seen_at": iso(NOW - timedelta(days=80)),
                },
                {
                    "id": "overdue-en",
                    "fingerprint": "fp-en",
                    "market_country": "AU",
                    "currency": "AUD",
                    "current_market_price": 12.5,
                    "recommended_price": 12.5,
                    "next_refresh_due_at": iso(NOW - timedelta(days=10)),
                    "stale_after": iso(NOW - timedelta(days=10)),
                    "refresh_status": "completed",
                    "last_error_message": None,
                    "last_updated_at": iso(NOW - timedelta(days=11)),
                    "popularity_score": 20,
                    "inventory_count": 4,
                    "last_seen_at": iso(NOW - timedelta(days=3)),
                },
            ]
        )
        scheduler = MarketPriceRefreshScheduler(
            client=client,
            config=fixed_config(max_enqueues=2),
            now_func=lambda: NOW,
        )
        report = scheduler.run_once()
        enqueued_ids = [job["price_key_id"] for job in report["enqueuedJobs"]]
        self.assertNotIn("jp-failed", enqueued_ids)
        self.assertIn("overdue-en", enqueued_ids)
        reasons = {item["decision"]["reason"] for item in report["candidateDecisions"]}
        self.assertIn("failure_backoff", reasons)

    def test_starvation_regression_many_overdue_progresses(self) -> None:
        rows = []
        # Two deterministic identity failures under backoff.
        for idx, key_id in enumerate(("jp-a", "jp-b")):
            rows.append(
                {
                    "id": key_id,
                    "fingerprint": f"fp-{key_id}",
                    "market_country": "AU",
                    "currency": "AUD",
                    "current_market_price": None,
                    "recommended_price": None,
                    "next_refresh_due_at": iso(NOW + timedelta(hours=6)),
                    "stale_after": iso(NOW + timedelta(hours=6)),
                    "refresh_status": "failed",
                    "last_error_message": ENGLISH_MARKET_IDENTITY_UNAVAILABLE,
                    "last_updated_at": iso(NOW - timedelta(minutes=1 + idx)),
                    "popularity_score": 0,
                    "inventory_count": 0,
                    "last_seen_at": iso(NOW - timedelta(days=90)),
                }
            )
        # Large overdue priced backlog.
        for idx in range(20):
            rows.append(
                {
                    "id": f"stale-{idx}",
                    "fingerprint": f"fp-stale-{idx}",
                    "market_country": "AU",
                    "currency": "AUD",
                    "current_market_price": 5.0 + idx,
                    "recommended_price": 5.0 + idx,
                    "next_refresh_due_at": iso(NOW - timedelta(days=20 + idx)),
                    "stale_after": iso(NOW - timedelta(days=20 + idx)),
                    "refresh_status": "completed",
                    "last_error_message": None,
                    "last_updated_at": iso(NOW - timedelta(days=21 + idx)),
                    "popularity_score": 10,
                    "inventory_count": 2,
                    "last_seen_at": iso(NOW - timedelta(days=5)),
                }
            )
        client = FakeSchedulerClient(stale_rows=rows)
        scheduler = MarketPriceRefreshScheduler(
            client=client,
            config=fixed_config(max_enqueues=5),
            now_func=lambda: NOW,
        )
        seen: set[str] = set()
        for _ in range(3):
            report = scheduler.run_once()
            batch = {job["price_key_id"] for job in report["enqueuedJobs"]}
            self.assertTrue(batch.isdisjoint({"jp-a", "jp-b"}))
            seen |= batch
            # Simulate claimed/completed so next cycle can pick other keys.
            for key_id in batch:
                client.active_jobs.pop(key_id, None)
        self.assertGreaterEqual(len(seen), 5)
        self.assertTrue(all(item.startswith("stale-") for item in seen))


if __name__ == "__main__":
    unittest.main()
