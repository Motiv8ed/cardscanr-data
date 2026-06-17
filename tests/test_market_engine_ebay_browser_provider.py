from __future__ import annotations

import os
import json
import types
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.config import MarketEngineConfig  # noqa: E402
from cardscanr_market_engine.marketplaces import resolve_marketplace_config  # noqa: E402
from cardscanr_market_engine.models import MarketPriceKey, MarketPriceRefreshJob, ProviderRequest, ProviderResult, SoldComp  # noqa: E402
from cardscanr_market_engine.filters import filter_comps  # noqa: E402
from cardscanr_market_engine.providers import MockMarketCompsProvider, create_market_comps_provider  # noqa: E402
from cardscanr_market_engine.providers.ebay_browser_provider import (  # noqa: E402
    EbayBrowserProviderConfig,
    EbayBrowserSoldCompsProvider,
    appears_to_be_personal_chrome_profile,
    build_quality_summary,
    contains_block_marker,
    count_candidate_selectors,
    dedupe_sold_comps,
    is_price_range_text,
    normalize_ebay_listing_url,
    parse_candidate_dict,
    parse_price_text,
    parse_shipping_text,
    parse_sold_date_text,
)
from cardscanr_market_engine.providers.identity_guard import evaluate_english_market_identity  # noqa: E402
from cardscanr_market_engine.providers.errors import (  # noqa: E402
    ProviderDisabledError,
    ProviderIdentityUnavailableError,
    sanitize_provider_diagnostics,
)
from cardscanr_market_engine.providers.errors import ProviderUnsupportedMarketError  # noqa: E402
from cardscanr_market_engine.providers.query_builder import build_provider_search_queries, build_provider_search_query  # noqa: E402
from cardscanr_market_engine.pricing_stats import calculate_pricing_stats  # noqa: E402


LEGACY_SCRIPT_SKIP_REASON = (
    "obsolete live/debug helper script was removed; script-specific tests are "
    "kept as historical coverage until the workflow is restored"
)


def _skip_removed_legacy_script(*_args: object, **_kwargs: object) -> None:
    raise unittest.SkipTest(LEGACY_SCRIPT_SKIP_REASON)


def _legacy_script_module(name: str, attrs: dict[str, object]) -> types.ModuleType:
    module = types.ModuleType(name)
    for attr_name, value in attrs.items():
        setattr(module, attr_name, value)
    sys.modules[name] = module
    return module


try:
    from scripts.debug_ebay_browser_card_matrix import _run_single_lookup, plan_card_matrix  # noqa: E402
except ModuleNotFoundError:
    _legacy_script_module(
        "scripts.debug_ebay_browser_card_matrix",
        {
            "_run_single_lookup": _skip_removed_legacy_script,
            "plan_card_matrix": _skip_removed_legacy_script,
            "subprocess": subprocess,
            "ARTIFACT_ROOT": ROOT / "reports" / "ebay_browser_debug" / "card_matrix",
        },
    )
    _run_single_lookup = _skip_removed_legacy_script
    plan_card_matrix = _skip_removed_legacy_script

try:
    from scripts.debug_ebay_browser_market_matrix import plan_market_matrix  # noqa: E402
except ModuleNotFoundError:
    _legacy_script_module(
        "scripts.debug_ebay_browser_market_matrix",
        {"plan_market_matrix": _skip_removed_legacy_script},
    )
    plan_market_matrix = _skip_removed_legacy_script

try:
    from scripts.smoke_ebay_browser_live_worker_batch import default_plan as live_worker_default_plan  # noqa: E402
    from scripts.smoke_ebay_browser_live_worker_batch import parse_market_list as parse_worker_market_list  # noqa: E402
    from scripts.smoke_ebay_browser_live_worker_batch import run_batch as run_live_worker_batch  # noqa: E402
except ModuleNotFoundError:
    _legacy_script_module(
        "scripts.smoke_ebay_browser_live_worker_batch",
        {
            "MarketEngineConfig": MarketEngineConfig,
            "MarketPriceJobRunner": object,
            "SupabaseMarketEngineClient": object,
            "create_market_comps_provider": _skip_removed_legacy_script,
            "default_plan": _skip_removed_legacy_script,
            "parse_market_list": _skip_removed_legacy_script,
            "run_batch": _skip_removed_legacy_script,
        },
    )
    live_worker_default_plan = _skip_removed_legacy_script
    parse_worker_market_list = _skip_removed_legacy_script
    run_live_worker_batch = _skip_removed_legacy_script

try:
    from scripts.smoke_ebay_browser_live_scheduler import run_live_scheduler  # noqa: E402
except ModuleNotFoundError:
    _legacy_script_module(
        "scripts.smoke_ebay_browser_live_scheduler",
        {
            "MarketEngineConfig": MarketEngineConfig,
            "run_live_scheduler": _skip_removed_legacy_script,
        },
    )
    run_live_scheduler = _skip_removed_legacy_script

try:
    from scripts.create_market_engine_upload_bundle import create_bundle  # noqa: E402
except ModuleNotFoundError:
    _legacy_script_module(
        "scripts.create_market_engine_upload_bundle",
        {"create_bundle": _skip_removed_legacy_script},
    )
    create_bundle = _skip_removed_legacy_script

try:
    from scripts.smoke_ebay_browser_live_write import _summarize_bundle  # noqa: E402
    from scripts.smoke_ebay_browser_live_write import _validation_flags  # noqa: E402
    from scripts.smoke_ebay_browser_live_write import run_smoke as run_live_write_smoke  # noqa: E402
except ModuleNotFoundError:
    _legacy_script_module(
        "scripts.smoke_ebay_browser_live_write",
        {
            "GLOBAL_DEBUG_LATEST_DIR": ROOT / "reports" / "ebay_browser_debug" / "latest",
            "LIVE_WRITE_DEBUG_DIR": ROOT / "reports" / "ebay_browser_debug" / "live_write" / "latest",
            "MarketEngineConfig": MarketEngineConfig,
            "MarketPriceJobRunner": object,
            "SupabaseMarketEngineClient": object,
            "create_market_comps_provider": _skip_removed_legacy_script,
            "_summarize_bundle": _skip_removed_legacy_script,
            "_validation_flags": _skip_removed_legacy_script,
            "run_smoke": _skip_removed_legacy_script,
        },
    )
    _summarize_bundle = _skip_removed_legacy_script
    _validation_flags = _skip_removed_legacy_script
    run_live_write_smoke = _skip_removed_legacy_script


def sample_request(
    *,
    country: str = "AU",
    currency: str = "AUD",
    condition: str = "raw",
    variant: str = "raw",
    card_name: str = "Charizard ex",
    normalized_card_name: str | None = None,
    set_name: str = "Obsidian Flames",
    set_code: str | None = "sv03",
    collector_number: str = "125/197",
    language: str = "en",
    raw: dict | None = None,
) -> ProviderRequest:
    market = resolve_marketplace_config(market_country=country, currency=currency, marketplace="ebay")
    key = MarketPriceKey(
        id="key-1",
        game="pokemon",
        card_name=card_name,
        normalized_card_name=normalized_card_name if normalized_card_name is not None else card_name.lower(),
        set_name=set_name,
        set_code=set_code,
        collector_number=collector_number,
        language=language,
        variant=variant,
        condition=condition,
        market_country=country.lower(),
        currency=currency.lower(),
        fingerprint=(
            f"pokemon|{language}|{set_code or set_name}|{collector_number}|"
            f"{(normalized_card_name if normalized_card_name is not None else card_name.lower()).replace(' ', '-')}"
            f"|{variant}|{condition}|{country.lower()}|{currency.lower()}"
        ),
        raw=raw or {},
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


def candidate_from_html_fixture(html: str) -> dict:
    href_match = re.search(r'href="([^"]*/itm/[^"]+)"', html)
    text = re.sub(r"<[^>]+>", "\n", html)
    return {
        "source": "html_fixture",
        "href": href_match.group(1) if href_match else "",
        "text": text,
    }


class ProviderFactoryTests(unittest.TestCase):
    def test_diagnostic_sanitizer_preserves_row_ids_but_redacts_secrets(self) -> None:
        payload = sanitize_provider_diagnostics(
            {
                "key_id": "key-1",
                "price_key_id": "price-key-1",
                "priceKeyId": "price-key-1",
                "cache_row_id": "cache-1",
                "cacheRowId": "cache-1",
                "snapshot_id": "snapshot-1",
                "snapshotId": "snapshot-1",
                "api_key": "secret",
                "authorization": "Bearer secret",
            }
        )
        self.assertEqual(payload["key_id"], "key-1")
        self.assertEqual(payload["price_key_id"], "price-key-1")
        self.assertEqual(payload["priceKeyId"], "price-key-1")
        self.assertEqual(payload["cache_row_id"], "cache-1")
        self.assertEqual(payload["cacheRowId"], "cache-1")
        self.assertEqual(payload["snapshot_id"], "snapshot-1")
        self.assertEqual(payload["snapshotId"], "snapshot-1")
        self.assertEqual(payload["api_key"], "***REDACTED***")
        self.assertEqual(payload["authorization"], "***REDACTED***")

    def test_provider_factory_default_is_mock(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            provider = create_market_comps_provider()
        self.assertIsInstance(provider, MockMarketCompsProvider)

    def test_ebay_browser_disabled_without_enable_flag(self) -> None:
        with patch.dict(os.environ, {"MARKET_LOOKUP_PROVIDER": "ebay_browser"}, clear=True):
            with self.assertRaises(ProviderDisabledError):
                create_market_comps_provider()

    def test_ebay_browser_enabled_with_explicit_flag(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MARKET_LOOKUP_PROVIDER": "ebay_browser",
                "ENABLE_EBAY_REAL_LOOKUP": "true",
                "EBAY_BROWSER_COOLDOWN_SECONDS": "1",
                "EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS": "1",
            },
            clear=True,
        ):
            provider = create_market_comps_provider()
        self.assertIsInstance(provider, EbayBrowserSoldCompsProvider)
        self.assertEqual(provider.config.engine, "chrome")
        self.assertEqual(provider.config.channel, "chrome")

    def test_default_profile_name_is_cardscanr(self) -> None:
        with patch.dict(os.environ, {"LIVE_EBAY_SCHEDULER_MAX_KEYS_SCANNED_PER_RUN": "25"}, clear=True):
            config = EbayBrowserProviderConfig.from_env()
        self.assertEqual(config.profile_name, "cardscanr")

    def test_default_user_data_dir_uses_repo_cardscanr_profile(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = EbayBrowserProviderConfig.from_env()
        normalized = str(config.user_data_dir).replace("/", "\\")
        self.assertTrue(normalized.endswith(".browser_profiles\\cardscanr"))

    def test_provider_refuses_personal_chrome_profile_path(self) -> None:
        personal_path = r"C:\Users\andyg\AppData\Local\Google\Chrome\User Data"
        self.assertTrue(appears_to_be_personal_chrome_profile(personal_path))
        with patch.dict(os.environ, {"EBAY_BROWSER_USER_DATA_DIR": personal_path}, clear=True):
            with self.assertRaises(ProviderDisabledError):
                EbayBrowserProviderConfig.from_env()

    def test_provider_config_creates_dedicated_profile_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / ".browser_profiles" / "cardscanr"
            config = EbayBrowserProviderConfig(
                engine="chrome",
                channel="chrome",
                profile_name="cardscanr",
                headless=True,
                max_results=30,
                timeout_seconds=45,
                launch_timeout_seconds=45,
                cooldown_seconds=20,
                min_seconds_between_requests=20,
                user_data_dir=profile_dir,
                market_scope="marketplace",
                debug_artifact_dir=None,
            )
            self.assertFalse(profile_dir.exists())
            self.assertEqual(config.ensure_profile_dir(), profile_dir)
            self.assertTrue(profile_dir.exists())

    def test_ebay_browser_rejects_unsupported_market_before_network(self) -> None:
        provider = EbayBrowserSoldCompsProvider()
        with self.assertRaises(ProviderUnsupportedMarketError):
            provider.fetch_comps(sample_request(country="DE", currency="EUR"))


class EnglishMarketIdentityGuardTests(unittest.TestCase):
    def _empty_result(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(
            provider_name="ebay_browser",
            marketplace=request.provider_marketplace_id,
            provider_fingerprint="test:fingerprint",
            query_used="Charizard ex 125/197 Obsidian Flames Pokemon card",
            comps=[],
            raw_metadata={"marketCountry": request.market_country},
        )

    def test_english_card_name_passes_for_au(self) -> None:
        provider = EbayBrowserSoldCompsProvider()
        request = sample_request(country="AU", currency="AUD", card_name="Charizard ex")
        with patch.object(provider, "_wait_for_request_slot") as wait_for_slot:
            with patch.object(provider, "_fetch_with_playwright", return_value=self._empty_result(request)) as fetch:
                result = provider.fetch_comps(request)
        self.assertEqual(result.marketplace, "EBAY_AU")
        self.assertEqual(wait_for_slot.call_count, 2)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result.raw_metadata["queryAttemptsUsed"], 2)
        self.assertEqual(result.raw_metadata["queryStopReason"], "no_useful_candidates")

    def test_japanese_card_name_is_blocked_for_au(self) -> None:
        provider = EbayBrowserSoldCompsProvider()
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="ハスブレロ",
            normalized_card_name="ハスブレロ",
        )
        with patch.object(provider, "_wait_for_request_slot") as wait_for_slot:
            with patch.object(provider, "_fetch_with_playwright") as fetch:
                with self.assertRaises(ProviderIdentityUnavailableError) as ctx:
                    provider.fetch_comps(request)
        self.assertEqual(str(ctx.exception), "english_market_identity_unavailable")
        self.assertEqual(ctx.exception.diagnostics["blocked_reason"], "english_market_identity_unavailable")
        self.assertEqual(ctx.exception.diagnostics["market_country"], "AU")
        self.assertEqual(ctx.exception.diagnostics["provider_marketplace"], "EBAY_AU")
        self.assertTrue(ctx.exception.diagnostics["non_latin_detected"])
        self.assertLess(ctx.exception.diagnostics["latin_ratio"], 0.5)
        self.assertNotIn("ハスブレロ", str(ctx.exception.diagnostics))
        wait_for_slot.assert_not_called()
        fetch.assert_not_called()

    def test_japanese_card_name_is_blocked_for_us_gb_ca(self) -> None:
        cases = (("US", "USD"), ("GB", "GBP"), ("CA", "CAD"))
        for country, currency in cases:
            with self.subTest(country=country):
                provider = EbayBrowserSoldCompsProvider()
                request = sample_request(
                    country=country,
                    currency=currency,
                    card_name="ハスブレロ",
                    normalized_card_name="ハスブレロ",
                )
                with patch.object(provider, "_fetch_with_playwright") as fetch:
                    with self.assertRaises(ProviderIdentityUnavailableError) as ctx:
                        provider.fetch_comps(request)
                self.assertEqual(ctx.exception.diagnostics["blocked_reason"], "english_market_identity_unavailable")
                self.assertEqual(ctx.exception.diagnostics["market_country"], country)
                fetch.assert_not_called()

    def test_japanese_card_name_is_not_globally_blocked_for_future_jp_market(self) -> None:
        request = replace(
            sample_request(
                country="AU",
                currency="AUD",
                card_name="ハスブレロ",
                normalized_card_name="ハスブレロ",
            ),
            market_country="JP",
            currency="JPY",
            provider_marketplace_id="EBAY_JP",
            provider_domain="ebay.co.jp",
            search_locale="ja-JP",
            display_name="Japan",
        )
        guard = evaluate_english_market_identity(request)
        self.assertFalse(guard.blocked)
        self.assertIsNone(guard.reason)
        self.assertTrue(guard.diagnostics["non_latin_detected"])

    def test_safe_english_alias_allows_query_without_original_non_latin_name(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="ハスブレロ",
            normalized_card_name="ハスブレロ",
            raw={"english_card_name": "Lombre"},
        )
        query = build_provider_search_query(request)
        self.assertIn("Lombre", query.query_text)
        self.assertNotIn("ハスブレロ", query.query_text)
        self.assertTrue(query.diagnostics["identityGuard"]["english_alias_available"])


class QueryBuilderTests(unittest.TestCase):
    def test_query_ladder_for_pancham_battle_partners(self) -> None:
        request = sample_request(
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        queries = build_provider_search_queries(request)
        self.assertEqual(
            [query.query_source for query in queries],
            [
                "language_primary_unquoted",
                "language_variant_unquoted",
                "broad_number_unquoted",
                "set_code_language_unquoted",
                "quoted_precision_fallback",
            ],
        )
        self.assertEqual(queries[0].query_text, "Pancham 050/100 Japanese Pokemon")
        self.assertEqual(queries[1].query_text, "Pancham 050/100 Japanese non holo Pokemon")
        self.assertEqual(queries[2].query_text, "Pancham 050/100 Pokemon")
        self.assertEqual(queries[3].query_text, "Pancham SV9 050 Japanese Pokemon")
        self.assertEqual(queries[4].query_text, '"Pancham" "050/100" Japanese Pokemon')
        self.assertEqual(queries[0].diagnostics["queryStyle"], "unquoted_discovery")
        self.assertEqual(queries[4].diagnostics["queryStyle"], "quoted_precision")
        self.assertEqual(queries[0].diagnostics["queryPolicy"], "simple_discovery_filter_after")
        self.assertEqual(queries[0].diagnostics["appliedNegativeTerms"], [])
        self.assertEqual(queries[0].diagnostics["rejectionPolicy"], "post_parse_only")
        self.assertEqual(queries[0].diagnostics["primaryQueryReason"], "simple_human_search_terms")

    def test_query_ladder_for_lombre_uses_english_alias(self) -> None:
        request = sample_request(
            card_name="ハスブレロ",
            normalized_card_name="ハスブレロ",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="022/100",
            language="jp",
            variant="non_holo",
            raw={"english_card_name": "Lombre"},
        )
        queries = build_provider_search_queries(request)
        self.assertEqual(
            [query.query_source for query in queries],
            [
                "language_primary_unquoted",
                "language_variant_unquoted",
                "broad_number_unquoted",
                "set_code_language_unquoted",
                "quoted_precision_fallback",
            ],
        )
        self.assertTrue(all("Lombre" in query.query_text for query in queries))
        self.assertFalse(any("ハスブレロ" in query.query_text for query in queries))
        self.assertEqual(queries[0].query_text, "Lombre 022/100 Japanese Pokemon")
        self.assertEqual(queries[1].query_text, "Lombre 022/100 Japanese non holo Pokemon")

    def test_multi_word_card_has_unquoted_and_quoted_fallback(self) -> None:
        request = sample_request(
            card_name="Roxie's Performance",
            normalized_card_name="roxies performance",
            set_name="Chaos Rising",
            set_code="SV10",
            collector_number="081/086",
            language="en",
        )
        queries = build_provider_search_queries(request)
        self.assertEqual(queries[0].query_text, "Roxie's Performance 081/086 Pokemon")
        self.assertEqual(queries[-1].query_text, '"Roxie\'s Performance" "081/086" Pokemon')
        self.assertEqual(queries[0].diagnostics["queryStyle"], "unquoted_discovery")
        self.assertEqual(queries[-1].diagnostics["queryStyle"], "quoted_precision")

    def test_jp_local_name_is_not_used_for_english_market_queries(self) -> None:
        for country, currency in (("AU", "AUD"), ("US", "USD"), ("GB", "GBP"), ("CA", "CAD")):
            with self.subTest(country=country):
                request = sample_request(
                    country=country,
                    currency=currency,
                    card_name="ハスブレロ",
                    normalized_card_name="ハスブレロ",
                    set_name="Battle Partners",
                    set_code="SV9",
                    collector_number="022/100",
                    language="jp",
                    raw={"canonical_english_name": "Lombre"},
                )
                queries = build_provider_search_queries(request)
                self.assertTrue(all("Lombre" in query.query_text for query in queries))
                self.assertFalse(any("ハスブレロ" in query.query_text for query in queries))

    def test_query_builder_au_uses_ebay_com_au(self) -> None:
        query = build_provider_search_query(sample_request(country="AU", currency="AUD"))
        self.assertEqual(query.provider_domain, "ebay.com.au")
        self.assertIn("www.ebay.com.au", query.search_url)

    def test_query_builder_us_uses_ebay_com(self) -> None:
        query = build_provider_search_query(sample_request(country="US", currency="USD"))
        self.assertEqual(query.provider_domain, "ebay.com")
        self.assertIn("www.ebay.com/sch/i.html", query.search_url)

    def test_query_builder_gb_uses_ebay_co_uk(self) -> None:
        query = build_provider_search_query(sample_request(country="GB", currency="GBP"))
        self.assertEqual(query.provider_domain, "ebay.co.uk")
        self.assertIn("www.ebay.co.uk", query.search_url)

    def test_query_builder_ca_uses_ebay_ca(self) -> None:
        query = build_provider_search_query(sample_request(country="CA", currency="CAD"))
        self.assertEqual(query.provider_domain, "ebay.ca")
        self.assertIn("www.ebay.ca", query.search_url)

    def test_query_builder_includes_sold_completed_params(self) -> None:
        query = build_provider_search_query(sample_request())
        self.assertIn("LH_Sold=1", query.search_url)
        self.assertIn("LH_Complete=1", query.search_url)

    def test_english_query_has_no_negative_terms(self) -> None:
        query = build_provider_search_query(sample_request())
        for term in ("proxy", "custom", "digital", "code", "jumbo", "pack", "booster", "sealed", "psa", "cgc", "bgs", "graded", "lot", "bundle", "holo", "reverse"):
            self.assertNotIn(f"-{term}", query.query_text)
        self.assertEqual(query.query_text, "Charizard ex 125/197 Pokemon")
        self.assertEqual(query.diagnostics["variantQueryMode"], "broad_variant_unknown_filter_later")
        self.assertEqual(query.diagnostics["queryPolicy"], "simple_discovery_filter_after")
        self.assertEqual(query.diagnostics["appliedNegativeTerms"], [])
        self.assertEqual(query.diagnostics["rejectionPolicy"], "post_parse_only")

    def test_query_builder_handles_graded_condition(self) -> None:
        query = build_provider_search_query(sample_request(condition="psa_10", variant="graded"))
        self.assertNotIn("-psa", query.query_text)
        self.assertNotIn("-graded", query.query_text)
        self.assertNotIn("-proxy", query.query_text)

    def test_query_builder_adds_reverse_holo_for_reverse_holo_variant(self) -> None:
        queries = build_provider_search_queries(sample_request(variant="reverse_holo"))
        self.assertTrue(any("reverse holo" in query.query_text for query in queries))
        self.assertFalse(any("-holo" in query.query_text for query in queries))
        self.assertEqual(queries[0].diagnostics["variantQueryMode"], "positive_reverse_holo_filter_required")

    def test_query_builder_uses_broad_non_holo_query_and_filter_later_diagnostics(self) -> None:
        queries = build_provider_search_queries(sample_request(variant="non_holo"))
        self.assertTrue(any("non holo" in query.query_text for query in queries))
        self.assertFalse(any("-holo" in query.query_text or "-reverse" in query.query_text for query in queries))
        self.assertFalse(any("-proxy" in query.query_text or "-pack" in query.query_text for query in queries))
        self.assertEqual(queries[0].diagnostics["variantQueryMode"], "broad_non_holo_filter_later")
        self.assertEqual(queries[0].diagnostics["appliedNegativeTerms"], [])

    def test_query_builder_holo_uses_positive_holo_without_reverse_negative(self) -> None:
        queries = build_provider_search_queries(sample_request(variant="holo"))
        self.assertTrue(any(" holo " in f" {query.query_text} " for query in queries))
        self.assertFalse(any("-reverse" in query.query_text for query in queries))
        self.assertEqual(queries[0].diagnostics["variantQueryMode"], "positive_holo_filter_reverse_later")

    def test_pancham_non_holo_query_ladder_uses_broad_query(self) -> None:
        request = sample_request(
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        queries = build_provider_search_queries(request)
        self.assertEqual(queries[0].query_text, "Pancham 050/100 Japanese Pokemon")
        self.assertEqual(queries[1].query_text, "Pancham 050/100 Japanese non holo Pokemon")
        self.assertEqual(queries[2].query_text, "Pancham 050/100 Pokemon")
        self.assertNotIn("-holo", queries[0].query_text)
        self.assertNotIn("-reverse", queries[0].query_text)
        self.assertNotIn("-lot", queries[0].query_text)
        self.assertNotIn("-bundle", queries[0].query_text)
        self.assertEqual(queries[0].query_source, "language_primary_unquoted")
        self.assertEqual(queries[1].query_source, "language_variant_unquoted")
        self.assertEqual(queries[0].diagnostics["variantQueryMode"], "broad_non_holo_filter_later")


class EvidenceStrategyTests(unittest.TestCase):
    def _sold_comp(
        self,
        title: str,
        *,
        item_id: str,
        sold_price: float = 2.5,
        query_index: int = 0,
        query_source: str = "exact",
        raw_metadata: dict | None = None,
        sold_date: datetime | None = None,
    ) -> SoldComp:
        metadata = {
            "url_quality": "direct_item",
            "item_id": item_id,
            "normalized_listing_url": f"https://www.ebay.com.au/itm/{item_id}",
            "query_index": query_index,
            "query_source": query_source,
        }
        metadata.update(raw_metadata or {})
        return SoldComp(
            source_listing_id=f"ebay-{item_id}",
            title=title,
            sold_price=sold_price,
            shipping_price=0.0,
            total_price=sold_price,
            currency="AUD",
            sold_date=sold_date or datetime(2026, 5, 20, tzinfo=timezone.utc),
            listing_url=f"https://www.ebay.com.au/itm/{item_id}",
            condition_text="Raw",
            raw_metadata=metadata,
        )

    def _provider_result(self, request: ProviderRequest, comps: list[SoldComp], *, query_used: str = "test") -> ProviderResult:
        return ProviderResult(
            provider_name="ebay_browser",
            marketplace=request.provider_marketplace_id,
            provider_fingerprint=f"test:{query_used}",
            query_used=query_used,
            comps=comps,
            raw_metadata={"qualitySummary": {}, "parserErrors": []},
        )

    def test_aggregator_dedupes_same_item_across_queries(self) -> None:
        comps = [
            self._sold_comp("Pancham 050/100 Battle Partners Pokemon Card", item_id="111", query_index=0, query_source="exact"),
            self._sold_comp("Pancham 050/100 Battle Partners Pokemon Card", item_id="111", query_index=1, query_source="without_set"),
        ]
        deduped = dedupe_sold_comps(comps)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].raw_metadata["query_sources"], ["exact", "without_set"])
        self.assertEqual(deduped[0].raw_metadata["duplicate_seen_count"], 2)

    def test_exact_set_number_comps_outrank_loose_comps(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
        )
        exact = self._sold_comp("Pancham 050/100 Battle Partners Pokemon Card", item_id="222")
        fallback = self._sold_comp("Pancham 050 SV9 Pokemon Card", item_id="223", query_source="set_code_fallback")
        evaluated = filter_comps(request.price_key, [exact, fallback])
        scores = {item.comp.source_listing_id: item.match_score for item in evaluated}
        self.assertGreater(scores["ebay-222"], scores["ebay-223"])
        self.assertTrue(all(item.included_in_estimate for item in evaluated))

    def test_selector_complete_your_set_listing_is_rejected(self) -> None:
        request = sample_request(country="AU", currency="AUD")
        query = build_provider_search_query(request)
        candidate = {
            "source": "fixture",
            "href": "https://www.ebay.com.au/itm/125",
            "text": "Sold 29 May 2026\nComplete Your Set Charizard ex 125/197\nAU $9.19\nFree postage",
        }
        comp = parse_candidate_dict(candidate, request=request, search_query=query, index=0)
        self.assertIsNotNone(comp)
        assert comp is not None
        evaluated = filter_comps(request.price_key, [comp])
        self.assertFalse(evaluated[0].included_in_estimate)
        self.assertEqual(evaluated[0].rejection_reason, "price_range_or_variation_listing")

    def test_junk_terms_are_rejected_by_filtering_not_query_negatives(self) -> None:
        query = build_provider_search_query(sample_request())
        for term in ("proxy", "custom", "pack", "booster", "sealed", "psa", "cgc", "bgs", "graded"):
            self.assertNotIn(f"-{term}", query.query_text)

        request = sample_request(country="AU", currency="AUD")
        cases = [
            ("Charizard ex 125/197 Obsidian Flames proxy Pokemon Card", "proxy_or_custom"),
            ("Charizard ex 125/197 Obsidian Flames custom Pokemon Card", "proxy_or_custom"),
            ("Charizard ex 125/197 Obsidian Flames PSA 9 Pokemon Card", "graded_for_raw_request"),
            ("Charizard ex 125/197 Obsidian Flames booster pack fresh Pokemon Card", "sealed_product_for_single_card_request"),
            ("Charizard ex 125/197 Obsidian Flames code card Pokemon", "digital"),
            ("Charizard ex 125/197 Obsidian Flames jumbo Pokemon Card", "oversized_or_jumbo"),
            ("Pick Your Card Charizard ex 125/197 Obsidian Flames Pokemon", "price_range_or_variation_listing"),
        ]
        comps = [self._sold_comp(title, item_id=str(700 + index)) for index, (title, _reason) in enumerate(cases)]
        evaluated = filter_comps(request.price_key, comps)
        self.assertEqual([item.rejection_reason for item in evaluated], [reason for _title, reason in cases])

    def test_non_holo_filtering_rejects_reverse_holo_and_holo_titles(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        reverse = self._sold_comp("Pancham 050/100 Battle Partners Reverse Holo Pokemon Card", item_id="601")
        holo = self._sold_comp("Pancham 050/100 Battle Partners Holo Pokemon Card", item_id="602")
        regular = self._sold_comp("Pancham 050/100 Battle Partners Non Holo Pokemon Card", item_id="603")
        evaluated = filter_comps(request.price_key, [reverse, holo, regular])
        reasons = {item.comp.source_listing_id: item.rejection_reason for item in evaluated}
        self.assertEqual(reasons["ebay-601"], "wrong_variant_reverse_holo")
        self.assertEqual(reasons["ebay-602"], "wrong_variant_holo")
        self.assertIsNone(reasons["ebay-603"])

    def test_non_holo_does_not_reject_non_holo_text(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        comp = self._sold_comp("Pancham 050/100 Battle Partners Japanese Non Holo Pokemon Card", item_id="610")
        evaluated = filter_comps(request.price_key, [comp])
        self.assertTrue(evaluated[0].included_in_estimate)
        self.assertIsNone(evaluated[0].rejection_reason)

    def test_jp_pancham_rejects_korean_language_listing(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        comp = self._sold_comp("Pancham 050/100 Battle Partners Korean Pokemon Card", item_id="611")
        evaluated = filter_comps(request.price_key, [comp])
        self.assertFalse(evaluated[0].included_in_estimate)
        self.assertEqual(evaluated[0].rejection_reason, "wrong_language")

    def test_jp_pancham_accepts_japanese_listing(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        comp = self._sold_comp("Pancham 050/100 Battle Partners Japanese Pokemon Card", item_id="612")
        evaluated = filter_comps(request.price_key, [comp])
        self.assertTrue(evaluated[0].included_in_estimate)
        self.assertIsNone(evaluated[0].rejection_reason)

    def test_korean_request_accepts_korean_and_rejects_japanese(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="kr",
            variant="non_holo",
        )
        korean = self._sold_comp("Pancham 050/100 Battle Partners Korean Pokemon Card", item_id="613")
        japanese = self._sold_comp("Pancham 050/100 Battle Partners Japanese Pokemon Card", item_id="614")
        evaluated = filter_comps(request.price_key, [korean, japanese])
        reasons = {item.comp.source_listing_id: item.rejection_reason for item in evaluated}
        self.assertIsNone(reasons["ebay-613"])
        self.assertEqual(reasons["ebay-614"], "wrong_language")

    def test_pick_your_own_from_raw_snippet_is_rejected(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        comp = self._sold_comp(
            "Pancham 050/100 Battle Partners Japanese Pokemon Card",
            item_id="615",
            raw_metadata={"rawTextSnippet": "Battle Partners - All Pokemon - Pick Your Own - Japanese - Postage Discount"},
        )
        evaluated = filter_comps(request.price_key, [comp])
        self.assertFalse(evaluated[0].included_in_estimate)
        self.assertEqual(evaluated[0].rejection_reason, "price_range_or_variation_listing")

    def test_pick_your_card_from_title_is_rejected(self) -> None:
        request = sample_request(country="AU", currency="AUD")
        comp = self._sold_comp("Pick Your Card Charizard ex 125/197 Obsidian Flames Pokemon", item_id="616")
        evaluated = filter_comps(request.price_key, [comp])
        self.assertFalse(evaluated[0].included_in_estimate)
        self.assertEqual(evaluated[0].rejection_reason, "price_range_or_variation_listing")

    def test_holo_lot_count_is_marked_lot_and_wrong_variant_for_non_holo(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        comp = self._sold_comp("Pancham 050/100 Battle Partners Japanese Holo Lot*56 Pokemon Cards", item_id="617")
        evaluated = filter_comps(request.price_key, [comp])
        self.assertFalse(evaluated[0].included_in_estimate)
        self.assertEqual(evaluated[0].rejection_reason, "wrong_variant_holo")
        self.assertTrue(evaluated[0].comp.raw_metadata["likely_bundle_lot"])

    def test_lombre_pick_your_card_listing_remains_rejected(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="ãƒã‚¹ãƒ–ãƒ¬ãƒ­",
            normalized_card_name="ãƒã‚¹ãƒ–ãƒ¬ãƒ­",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="022/100",
            language="jp",
            variant="non_holo",
            raw={"english_card_name": "Lombre"},
        )
        comp = self._sold_comp("Lombre 022/100 Battle Partners Japanese Pick Your Card Pokemon", item_id="618")
        evaluated = filter_comps(request.price_key, [comp])
        self.assertFalse(evaluated[0].included_in_estimate)
        self.assertEqual(evaluated[0].rejection_reason, "price_range_or_variation_listing")
        stats = calculate_pricing_stats(
            evaluated,
            now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            config=MarketEngineConfig.from_env(require_supabase=False),
        )
        self.assertIsNone(stats.recommended_price)

    def test_pancham_clean_japanese_comps_remain_included(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        comps = [
            self._sold_comp("Pancham 050/100 Battle Partners Japanese Non Holo Pokemon Card", item_id="619", sold_price=2.1),
            self._sold_comp("Pancham 050/100 Battle Partners Japanese Pokemon Card", item_id="620", sold_price=2.3),
            self._sold_comp("Pancham 050/100 Battle Partners JP Pokemon Card", item_id="621", sold_price=2.2),
        ]
        evaluated = filter_comps(request.price_key, comps)
        self.assertTrue(all(item.included_in_estimate for item in evaluated))
        self.assertTrue(all(item.rejection_reason is None for item in evaluated))

    def test_pancham_clean_title_with_collector_number_is_included(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        comp = self._sold_comp("Pancham 050/100 Battle Partners NM Japanese Pokemon Card TCG", item_id="624")
        evaluated = filter_comps(request.price_key, [comp])
        self.assertTrue(evaluated[0].included_in_estimate)
        self.assertIsNone(evaluated[0].rejection_reason)

    def test_pancham_hyphenated_set_code_title_is_included(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        comp = self._sold_comp("050-100-SV9-B - Pokemon Card - Japanese - Pancham - C", item_id="625")
        evaluated = filter_comps(request.price_key, [comp])
        self.assertTrue(evaluated[0].included_in_estimate)
        self.assertIsNone(evaluated[0].rejection_reason)

    def test_raw_snippet_ui_numbers_do_not_trigger_multiple_card_numbers(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        comp = self._sold_comp(
            "050-100-SV9-B - Pokemon Card - Japanese - Pancham - C",
            item_id="626",
            raw_metadata={
                "rawTextSnippet": (
                    "Sold 14 May 2026 050-100-SV9-B - Pokemon Card - Japanese - Pancham - C "
                    "Opens in a new window or tab Pre-owned AU $1.54 Buy It Now +AU $4.63 delivery "
                    "from Japan Free returns View similar active items Sell one like this midorigame "
                    "99.6% positive (29.7K)"
                )
            },
        )
        evaluated = filter_comps(request.price_key, [comp])
        self.assertTrue(evaluated[0].included_in_estimate)
        self.assertIsNone(evaluated[0].rejection_reason)
        identity_text = evaluated[0].comp.raw_metadata["collector_number_identity_text"]
        self.assertIn("050-100-SV9-B - Pokemon Card - Japanese - Pancham - C", identity_text)
        self.assertNotIn("AU $1.54", identity_text)
        self.assertNotIn("99.6%", identity_text)
        self.assertNotIn("29.7K", identity_text)

    def test_true_multi_number_title_is_rejected(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        comp = self._sold_comp("Pancham 050/100 001/100 098/100 Battle Partners Japanese Pokemon Card", item_id="627")
        evaluated = filter_comps(request.price_key, [comp])
        self.assertFalse(evaluated[0].included_in_estimate)
        self.assertEqual(evaluated[0].rejection_reason, "multiple_card_numbers")

    def test_confidence_stays_low_for_fewer_than_three_clean_variant_comps(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        comps = [
            self._sold_comp("Pancham 050/100 Battle Partners Japanese Non Holo Pokemon Card", item_id="622", sold_price=2.1),
            self._sold_comp("Pancham 050/100 Battle Partners Japanese Pokemon Card", item_id="623", sold_price=2.3),
        ]
        evaluated = filter_comps(request.price_key, comps)
        stats = calculate_pricing_stats(
            evaluated,
            now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            config=MarketEngineConfig.from_env(require_supabase=False),
        )
        self.assertEqual(stats.included_count, 2)
        self.assertEqual(stats.confidence, "low")
        self.assertIn("insufficient_variant_specific_comps", stats.confidence_warnings)

    def test_reverse_holo_filtering_requires_reverse_holo_match(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="reverse_holo",
        )
        regular = self._sold_comp("Pancham 050/100 Battle Partners Non Holo Pokemon Card", item_id="604")
        reverse = self._sold_comp("Pancham 050/100 Battle Partners Reverse Holo Pokemon Card", item_id="605")
        evaluated = filter_comps(request.price_key, [regular, reverse])
        reasons = {item.comp.source_listing_id: item.rejection_reason for item in evaluated}
        self.assertEqual(reasons["ebay-604"], "weak_variant_match")
        self.assertIsNone(reasons["ebay-605"])

    def test_holo_filtering_rejects_reverse_holo_titles(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="holo",
        )
        reverse = self._sold_comp("Pancham 050/100 Battle Partners Reverse Holo Pokemon Card", item_id="606")
        holo = self._sold_comp("Pancham 050/100 Battle Partners Holo Pokemon Card", item_id="607")
        evaluated = filter_comps(request.price_key, [reverse, holo])
        reasons = {item.comp.source_listing_id: item.rejection_reason for item in evaluated}
        self.assertEqual(reasons["ebay-606"], "wrong_variant_reverse_holo")
        self.assertIsNone(reasons["ebay-607"])

    def test_lot_and_bundle_are_rejected_by_filtering_not_query_negatives(self) -> None:
        query = build_provider_search_query(sample_request(variant="non_holo"))
        self.assertNotIn("-lot", query.query_text)
        self.assertNotIn("-bundle", query.query_text)

        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        lot = self._sold_comp("Pancham 050/100 Battle Partners Pokemon Card lot", item_id="608")
        bundle = self._sold_comp("Pancham 050/100 Battle Partners Pokemon Card bundle", item_id="609")
        evaluated = filter_comps(request.price_key, [lot, bundle])
        self.assertEqual([item.rejection_reason for item in evaluated], ["likely_bundle_lot", "likely_bundle_lot"])

    def test_no_reliable_price_when_only_weak_comps_exist(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
        )
        weak = self._sold_comp("Pancham Pokemon Card", item_id="333")
        evaluated = filter_comps(request.price_key, [weak])
        self.assertFalse(evaluated[0].included_in_estimate)
        stats = calculate_pricing_stats(
            evaluated,
            now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            config=MarketEngineConfig.from_env(require_supabase=False),
        )
        self.assertIsNone(stats.recommended_price)
        self.assertEqual(stats.no_reliable_price_reason, "all_comps_rejected")

    def test_early_stop_when_three_clean_comps_are_found(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        provider = EbayBrowserSoldCompsProvider()
        first_result = self._provider_result(
            request,
            [
                self._sold_comp("Pancham 050/100 Battle Partners Japanese Non Holo Pokemon Card", item_id="900"),
                self._sold_comp("Pancham 050/100 Battle Partners Japanese Pokemon Card", item_id="901"),
                self._sold_comp("Pancham 050/100 Battle Partners JP Pokemon Card", item_id="902"),
            ],
        )
        with patch.object(provider, "_wait_for_request_slot"):
            with patch.object(provider, "_fetch_with_playwright", return_value=first_result) as fetch:
                result = provider.fetch_comps(request)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result.raw_metadata["queryStopReason"], "enough_clean_comps")
        self.assertTrue(result.raw_metadata["earlyStopApplied"])
        self.assertEqual(result.raw_metadata["cleanIncludedCount"], 3)
        self.assertEqual(result.raw_metadata["cumulativeIncludedAfterEachAttempt"], [3])

    def test_pancham_stops_with_sparse_clean_market_evidence_after_two_clean_comps_and_duplicate_evidence(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        clean_a = self._sold_comp("Pancham 050/100 Battle Partners Japanese Non Holo Pokemon Card", item_id="910", sold_price=2.1)
        clean_b = self._sold_comp("Pancham 050/100 Battle Partners Japanese Pokemon Card", item_id="911", sold_price=2.3)
        duplicate_a = self._sold_comp("Pancham 050/100 Battle Partners Japanese Non Holo Pokemon Card", item_id="910", sold_price=2.1)
        provider = EbayBrowserSoldCompsProvider()
        side_effect = [
            self._provider_result(request, [clean_a, clean_b], query_used="q1"),
            self._provider_result(request, [duplicate_a], query_used="q2"),
        ]
        with patch.object(provider, "_wait_for_request_slot"):
            with patch.object(provider, "_fetch_with_playwright", side_effect=side_effect) as fetch:
                result = provider.fetch_comps(request)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result.raw_metadata["queryStopReason"], "sparse_clean_market_evidence")
        self.assertEqual(result.raw_metadata["lowConfidenceSparseMarketReason"], "two_clean_comps_after_duplicate_or_noisy_evidence")
        self.assertEqual(result.raw_metadata["cleanIncludedCount"], 2)
        self.assertEqual(result.raw_metadata["cleanExactCompCount"], 2)
        self.assertEqual(result.raw_metadata["selectorRejectedCount"], 0)
        evaluated = filter_comps(request.price_key, result.comps)
        stats = calculate_pricing_stats(
            evaluated,
            now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            config=MarketEngineConfig.from_env(require_supabase=False),
        )
        self.assertEqual(stats.confidence, "low")
        self.assertIn("insufficient_variant_specific_comps", stats.confidence_warnings)

    def test_lombre_wrong_number_heavy_results_stop_with_noisy_results_no_exact_comps(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Lombre",
            normalized_card_name="lombre",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="022/100",
            language="jp",
            variant="non_holo",
        )
        provider = EbayBrowserSoldCompsProvider()
        broad_noise = [
            self._sold_comp("Lombre 45/100 EX Sandstorm Japanese Pokemon", item_id="930"),
            self._sold_comp("Lombre 37/100 Crystal Guardians Japanese Pokemon", item_id="931"),
            self._sold_comp("Lombre 045/100 Japanese Pokemon", item_id="932"),
        ]
        set_code_noise = [
            self._sold_comp("Lombre SV9 037 Japanese Pokemon", item_id="933"),
            self._sold_comp("Camerupt 022/100 Battle Partners Japanese Pokemon", item_id="934"),
        ]
        quoted_noise = [
            self._sold_comp("Lombre 022/100 Battle Partners Japanese Reverse Holo Pokemon", item_id="935"),
        ]
        side_effect = [
            self._provider_result(request, broad_noise, query_used="broad"),
            self._provider_result(request, set_code_noise, query_used="set-code"),
            self._provider_result(request, quoted_noise, query_used="quoted"),
        ]
        with patch.object(provider, "_wait_for_request_slot"):
            with patch.object(provider, "_fetch_with_playwright", side_effect=side_effect) as fetch:
                result = provider.fetch_comps(request)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual([item["query_source"] for item in result.raw_metadata["queryAttempts"]], [
            "language_primary_unquoted",
            "set_code_language_unquoted",
            "quoted_precision_fallback",
        ])
        self.assertEqual(result.raw_metadata["queryStopReason"], "noisy_results_no_exact_comps")
        self.assertEqual(result.raw_metadata["cleanExactCompCount"], 0)
        self.assertEqual(result.raw_metadata["exactIdentityResultCount"], 0)
        self.assertGreaterEqual(result.raw_metadata["wrongCollectorNumberRejectedCount"], 4)
        self.assertGreaterEqual(result.raw_metadata["wrongCardNameRejectedCount"], 1)
        self.assertGreaterEqual(result.raw_metadata["wrongVariantRejectedCount"], 1)
        self.assertGreaterEqual(result.raw_metadata["noisyResultRatio"], 0.6)
        evaluated = filter_comps(request.price_key, result.comps)
        stats = calculate_pricing_stats(
            evaluated,
            now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            config=MarketEngineConfig.from_env(require_supabase=False),
        )
        self.assertIsNone(stats.recommended_price)
        self.assertEqual(stats.no_reliable_price_reason, "no_clean_exact_comps")
        self.assertEqual(result.raw_metadata["queryAttempts"][0]["dominant_rejection_reason"], "wrong_collector_number")
        self.assertEqual(result.raw_metadata["queryAttempts"][1]["dominant_rejection_reason"], "wrong_collector_number")
        self.assertEqual(result.raw_metadata["queryAttempts"][2]["dominant_rejection_reason"], "wrong_variant_reverse_holo")

    def test_lombre_one_old_clean_comp_plus_noisy_results_stops_and_marks_stale_single_comp(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Lombre",
            normalized_card_name="lombre",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="022/100",
            language="jp",
            variant="non_holo",
        )
        provider = EbayBrowserSoldCompsProvider()
        old_clean = self._sold_comp(
            "Lombre 022/100 SV9 Battle Partners NM Japanese Pokemon TCG",
            item_id="940",
            sold_price=1.79,
            sold_date=datetime(2025, 5, 18, tzinfo=timezone.utc),
        )
        broad_noise = [
            old_clean,
            self._sold_comp("Lombre 45/100 EX Sandstorm Japanese Pokemon", item_id="941"),
            self._sold_comp("Lombre 37/100 Crystal Guardians Japanese Pokemon", item_id="942"),
            self._sold_comp("Camerupt 022/100 Battle Partners Japanese Pokemon", item_id="943"),
        ]
        set_code_noise = [
            self._sold_comp("Battle Partners sv9 Japanese Pokemon Card Singles Non-Holo - Pick Your Card", item_id="944"),
            self._sold_comp("Lombre SV9 037 Japanese Pokemon", item_id="945"),
        ]
        quoted_empty = []
        side_effect = [
            self._provider_result(request, broad_noise, query_used="broad"),
            self._provider_result(request, set_code_noise, query_used="set-code"),
            self._provider_result(request, quoted_empty, query_used="quoted"),
        ]
        with patch.object(provider, "_wait_for_request_slot"):
            with patch.object(provider, "_fetch_with_playwright", side_effect=side_effect) as fetch:
                result = provider.fetch_comps(request)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual([item["query_source"] for item in result.raw_metadata["queryAttempts"]], [
            "language_primary_unquoted",
            "set_code_language_unquoted",
            "quoted_precision_fallback",
        ])
        self.assertEqual(result.raw_metadata["queryStopReason"], "stale_single_comp_only")
        self.assertEqual(result.raw_metadata["cleanExactCompCount"], 1)
        self.assertEqual(result.raw_metadata["cleanRecentCompCount"], 0)
        self.assertEqual(result.raw_metadata["cleanStaleCompCount"], 1)
        self.assertTrue(result.raw_metadata["singleCleanCompOnly"])
        self.assertTrue(result.raw_metadata["staleEvidenceOnly"])
        evaluated = filter_comps(request.price_key, result.comps)
        stats = calculate_pricing_stats(
            evaluated,
            now=datetime(2026, 6, 5, tzinfo=timezone.utc),
            config=MarketEngineConfig.from_env(require_supabase=False),
        )
        self.assertEqual(stats.included_count, 1)
        self.assertIsNone(stats.recommended_price)
        self.assertEqual(stats.price_reliability, "stale_single_comp")
        self.assertEqual(stats.no_reliable_price_reason, "stale_single_comp_only")
        self.assertIn("single_clean_comp_only", stats.confidence_warnings)
        self.assertIn("stale_evidence_only", stats.confidence_warnings)

    def test_selector_only_results_stop_with_no_reliable_price(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Lombre",
            normalized_card_name="lombre",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="022/100",
            language="jp",
            variant="non_holo",
        )
        provider = EbayBrowserSoldCompsProvider()
        side_effect = [
            self._provider_result(
                request,
                [self._sold_comp("Lombre 022/100 Battle Partners Japanese Pick Your Card Pokemon", item_id=str(920 + index))],
                query_used=f"q{index}",
            )
            for index in range(4)
        ]
        with patch.object(provider, "_wait_for_request_slot"):
            with patch.object(provider, "_fetch_with_playwright", side_effect=side_effect) as fetch:
                result = provider.fetch_comps(request)
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(result.raw_metadata["queryStopReason"], "only_selector_results")
        self.assertTrue(result.raw_metadata["earlyStopApplied"])
        evaluated = filter_comps(request.price_key, result.comps)
        self.assertTrue(all(item.rejection_reason == "price_range_or_variation_listing" for item in evaluated))
        stats = calculate_pricing_stats(
            evaluated,
            now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            config=MarketEngineConfig.from_env(require_supabase=False),
        )
        self.assertIsNone(stats.recommended_price)
        self.assertEqual(stats.no_reliable_price_reason, "all_comps_rejected")

    def test_high_outlier_does_not_dominate_recommended_price(self) -> None:
        stats = calculate_pricing_stats(
            [
                type("Eval", (), {"included_in_estimate": True, "match_score": 0.9, "comp": self._sold_comp("Pancham 050/100 Battle Partners", item_id="401", sold_price=2.0)})(),
                type("Eval", (), {"included_in_estimate": True, "match_score": 0.9, "comp": self._sold_comp("Pancham 050/100 Battle Partners", item_id="402", sold_price=2.5)})(),
                type("Eval", (), {"included_in_estimate": True, "match_score": 0.9, "comp": self._sold_comp("Pancham 050/100 Battle Partners", item_id="403", sold_price=3.0)})(),
                type("Eval", (), {"included_in_estimate": True, "match_score": 0.9, "comp": self._sold_comp("Pancham 050/100 Battle Partners", item_id="404", sold_price=99.0)})(),
            ],
            now=datetime(2026, 5, 25, tzinfo=timezone.utc),
            config=MarketEngineConfig.from_env(require_supabase=False),
        )
        self.assertLess(stats.recommended_price or 0, 10.0)
        self.assertEqual(stats.recommended_price, 2.75)

    def test_aggregate_report_includes_query_attempts_and_rejection_reasons(self) -> None:
        request = sample_request(country="AU", currency="AUD")
        queries = build_provider_search_queries(request, max_attempts=2)
        good = self._sold_comp("Charizard ex 125/197 Obsidian Flames raw Pokemon Card", item_id="501")
        bad = self._sold_comp("Complete Your Set Charizard ex 125/197", item_id="502")
        attempts = [
            (
                queries[0],
                ProviderResult(
                    provider_name="ebay_browser",
                    marketplace="EBAY_AU",
                    provider_fingerprint="test:1",
                    query_used=queries[0].query_text,
                    comps=[good, bad],
                    raw_metadata={"qualitySummary": {"total_parsed": 2}, "parserErrors": []},
                ),
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            provider = EbayBrowserSoldCompsProvider(
                config=EbayBrowserProviderConfig(
                    engine="chrome",
                    channel="chrome",
                    profile_name="cardscanr",
                    headless=True,
                    max_results=30,
                    timeout_seconds=45,
                    launch_timeout_seconds=45,
                    cooldown_seconds=20,
                    min_seconds_between_requests=20,
                    user_data_dir=Path(tmp) / ".browser_profiles" / "cardscanr",
                    market_scope="marketplace",
                    debug_artifact_dir=Path(tmp) / "debug",
                )
            )
            result = provider._build_aggregate_result(
                request=request,
                attempts=attempts,
                comps=dedupe_sold_comps([good, bad]),
                stop_reason="test",
                query_attempt_limit=2,
                failed_attempts=[
                    {
                        "query_index": 0,
                        "query_source": "broad_number_unquoted",
                        "timed_out_stage": "wait_for_result_container",
                    }
                ],
                stage_timings={
                    "stageDurationsMs": {"run_query_attempt_1": 30000.0},
                    "timedOutStage": "wait_for_result_container",
                },
            )
            summary = json.loads((Path(tmp) / "debug" / "debug_summary.json").read_text(encoding="utf-8"))
        self.assertIn("queryAttempts", result.raw_metadata)
        self.assertEqual(result.raw_metadata["queryAttempts"][0]["search_url"], queries[0].search_url)
        self.assertEqual(result.raw_metadata["failedQueryAttempts"][0]["timed_out_stage"], "wait_for_result_container")
        self.assertIn("stageTimings", result.raw_metadata)
        self.assertIn("aggregate", result.raw_metadata["stageTimings"])
        self.assertIn("evidence_filtering", result.raw_metadata["stageTimings"]["aggregate"]["stageDurationsMs"])
        self.assertIn("report_writing", result.raw_metadata["stageTimings"]["aggregate"]["stageDurationsMs"])
        self.assertEqual(summary["query_attempts"][0]["query_source"], "broad_number_unquoted")
        self.assertIn("early_stop_applied", summary)
        self.assertEqual(summary["failed_query_attempts"][0]["timed_out_stage"], "wait_for_result_container")
        self.assertIn("stage_timings", summary)
        self.assertEqual(summary["top_rejected_comps"][0]["rejection_reason"], "price_range_or_variation_listing")


class ParserTests(unittest.TestCase):
    def test_price_parser_handles_aud_usd_gbp_cad_examples(self) -> None:
        examples = [
            ("A$12.34", "AUD", 12.34),
            ("US $56.78", "USD", 56.78),
            ("£9.99", "GBP", 9.99),
            ("C $101.50", "CAD", 101.50),
            ("\u00a39.99", "GBP", 9.99),
        ]
        for text, currency, expected in examples:
            amount, detected, _diagnostics = parse_price_text(text, expected_currency=currency)
            self.assertEqual(amount, expected)
            self.assertEqual(detected, currency)

    def test_shipping_parser_handles_free_and_paid_shipping(self) -> None:
        free, free_diag = parse_shipping_text("Free postage", expected_currency="AUD")
        paid, paid_diag = parse_shipping_text("+ A$4.99 shipping", expected_currency="AUD")
        self.assertEqual(free, 0.0)
        self.assertTrue(free_diag["freeShipping"])
        self.assertEqual(paid, 4.99)
        self.assertEqual(paid_diag["detectedCurrency"], "AUD")

    def test_candidate_parser_handles_visible_au_result_pattern(self) -> None:
        request = sample_request(country="AU", currency="AUD")
        query = build_provider_search_query(request)
        html = """
        <li class="s-item">
          <span>Sold 29 May 2026</span>
          <a class="s-item__link" href="https://www.ebay.com.au/itm/1234567890?hash=abc">
            Charizard ex 125/197 | Double Rare SV03: Obsidian Flames | Pokemon Card | NM
          </a>
          <span class="s-item__price">AU $9.19</span>
          <span class="s-item__shipping">+AU $15.04 delivery</span>
        </li>
        """
        candidate = candidate_from_html_fixture(html)
        comp = parse_candidate_dict(candidate, request=request, search_query=query, index=0)
        self.assertIsNotNone(comp)
        assert comp is not None
        self.assertIn("Charizard ex 125/197", comp.title)
        self.assertEqual(comp.sold_price, 9.19)
        self.assertEqual(comp.shipping_price, 15.04)
        self.assertEqual(comp.total_price, 24.23)
        self.assertEqual(comp.currency, "AUD")
        self.assertIn("/itm/", comp.listing_url)
        self.assertEqual(comp.listing_url, "https://www.ebay.com.au/itm/1234567890")
        self.assertEqual(comp.raw_metadata["url_quality"], "direct_item")
        self.assertEqual(comp.raw_metadata["item_id"], "1234567890")
        self.assertEqual(comp.raw_metadata["original_href"], "https://www.ebay.com.au/itm/1234567890?hash=abc")
        self.assertEqual(comp.raw_metadata["soldDateText"], "Sold 29 May 2026")

    def test_relative_item_url_becomes_absolute_provider_url(self) -> None:
        normalized = normalize_ebay_listing_url("/itm/123456?hash=abc", provider_domain="ebay.com.au")
        self.assertEqual(normalized["url_quality"], "direct_item")
        self.assertEqual(normalized["item_id"], "123456")
        self.assertEqual(normalized["normalized_listing_url"], "https://www.ebay.com.au/itm/123456")

    def test_generic_search_url_is_not_treated_as_item_url(self) -> None:
        normalized = normalize_ebay_listing_url(
            "https://www.ebay.com.au/sch/i.html?_nkw=charizard",
            provider_domain="ebay.com.au",
        )
        self.assertEqual(normalized["url_quality"], "generic_non_item")
        self.assertIsNone(normalized["normalized_listing_url"])

    def test_missing_url_is_safe(self) -> None:
        normalized = normalize_ebay_listing_url("", provider_domain="ebay.com.au")
        self.assertEqual(normalized["url_quality"], "missing")
        self.assertIsNone(normalized["normalized_listing_url"])

    def test_candidate_parser_handles_free_postage(self) -> None:
        request = sample_request(country="AU", currency="AUD")
        query = build_provider_search_query(request)
        candidate = {
            "source": "fixture",
            "href": "https://www.ebay.com.au/itm/123",
            "text": "Sold 29 May 2026\nCharizard ex 125/197 Pokemon Card\nAU $9.19\nFree postage",
        }
        comp = parse_candidate_dict(candidate, request=request, search_query=query, index=0)
        self.assertIsNotNone(comp)
        assert comp is not None
        self.assertEqual(comp.shipping_price, 0.0)

    def test_us_fallback_does_not_parse_feedback_percentage(self) -> None:
        request = sample_request(country="US", currency="USD")
        query = build_provider_search_query(request)
        candidate = {
            "source": "fixture",
            "href": "https://www.ebay.com/itm/987",
            "text": (
                "Sold May 22, 2026 Charizard ex - 125/197 SV03: Obsidian Flames - Pokemon Card - NM "
                "$7.06 or Best Offer Free delivery Located in Australia View similar active items "
                "Sell one like this seller 99.8% positive"
            ),
        }
        comp = parse_candidate_dict(candidate, request=request, search_query=query, index=0)
        self.assertIsNotNone(comp)
        assert comp is not None
        self.assertEqual(comp.sold_price, 7.06)
        self.assertEqual(comp.currency, "USD")
        self.assertNotEqual(comp.sold_price, 99.8)
        self.assertEqual(comp.raw_metadata["priceDiagnostics"]["rejectedNonPricePercent"], 1)

    def test_feedback_percentage_not_parsed_as_price(self) -> None:
        amount, currency, diagnostics = parse_price_text("99.8% positive", expected_currency="USD")
        self.assertIsNone(amount)
        self.assertIsNone(currency)
        self.assertEqual(diagnostics["reason"], "no_currency_price")
        self.assertEqual(diagnostics["rejectedNonPricePercent"], 1)

    def test_product_rating_not_parsed_as_price(self) -> None:
        amount, currency, diagnostics = parse_price_text(
            "4.5 out of 5 stars. 4 product ratings",
            expected_currency="USD",
        )
        self.assertIsNone(amount)
        self.assertIsNone(currency)
        self.assertEqual(diagnostics["reason"], "no_currency_price")

    def test_au_bid_and_delivery_prices(self) -> None:
        request = sample_request(country="AU", currency="AUD")
        query = build_provider_search_query(request)
        comp = parse_candidate_dict(
            {
                "source": "fixture",
                "href": "https://www.ebay.com.au/itm/555",
                "text": "Sold 29 May 2026\nCharizard ex 125/197 Pokemon Card\nAU $9.19 1 bid\n+AU $15.04 delivery",
            },
            request=request,
            search_query=query,
            index=0,
        )
        self.assertIsNotNone(comp)
        assert comp is not None
        self.assertEqual(comp.sold_price, 9.19)
        self.assertEqual(comp.shipping_price, 15.04)
        self.assertEqual(comp.currency, "AUD")

    def test_ca_bid_and_shipping_prices(self) -> None:
        request = sample_request(country="CA", currency="CAD")
        query = build_provider_search_query(request)
        comp = parse_candidate_dict(
            {
                "source": "fixture",
                "href": "https://www.ebay.ca/itm/555",
                "text": "Sold May 22, 2026\nCharizard ex 125/197 Pokemon Card\nC $9.09 1 bid\n+C $14.87 shipping",
            },
            request=request,
            search_query=query,
            index=0,
        )
        self.assertIsNotNone(comp)
        assert comp is not None
        self.assertEqual(comp.sold_price, 9.09)
        self.assertEqual(comp.shipping_price, 14.87)
        self.assertEqual(comp.currency, "CAD")

    def test_gb_buy_it_now_and_postage_prices(self) -> None:
        request = sample_request(country="GB", currency="GBP")
        query = build_provider_search_query(request)
        comp = parse_candidate_dict(
            {
                "source": "fixture",
                "href": "https://www.ebay.co.uk/itm/555",
                "text": "Sold 22 May 2026\nCharizard ex 125/197 Pokemon Card\n£5.30 Buy It Now\n+£8.00 postage",
            },
            request=request,
            search_query=query,
            index=0,
        )
        self.assertIsNotNone(comp)
        assert comp is not None
        self.assertEqual(comp.sold_price, 5.30)
        self.assertEqual(comp.shipping_price, 8.00)
        self.assertEqual(comp.currency, "GBP")

    def test_currency_mismatch_is_not_useful(self) -> None:
        request = sample_request(country="US", currency="USD")
        query = build_provider_search_query(request)
        comp = parse_candidate_dict(
            {
                "source": "fixture",
                "href": "https://www.ebay.com/itm/888",
                "text": "Sold May 22, 2026\nCharizard ex 125/197 Pokemon Card\nC $9.09\nFree shipping",
            },
            request=request,
            search_query=query,
            index=0,
        )
        self.assertIsNotNone(comp)
        assert comp is not None
        self.assertEqual(comp.currency, "CAD")
        evaluated = filter_comps(request.price_key, [comp])
        self.assertFalse(evaluated[0].included_in_estimate)
        self.assertEqual(evaluated[0].rejection_reason, "currency_mismatch")

    def test_price_range_detection_and_filter_rejection(self) -> None:
        request = sample_request(country="AU", currency="AUD")
        query = build_provider_search_query(request)
        candidate = {
            "source": "fixture",
            "href": "https://www.ebay.com.au/itm/123",
            "text": "Sold 29 May 2026\nChoose Your Card Charizard ex 125/197\nAU $1.99 to AU $1,386.35\nFree postage",
        }
        comp = parse_candidate_dict(candidate, request=request, search_query=query, index=0)
        self.assertIsNotNone(comp)
        assert comp is not None
        self.assertTrue(comp.raw_metadata["priceRangeListing"])
        evaluated = filter_comps(request.price_key, [comp])
        self.assertFalse(evaluated[0].included_in_estimate)
        self.assertEqual(evaluated[0].rejection_reason, "price_range_or_variation_listing")

    def test_pick_your_card_rejection(self) -> None:
        request = sample_request(country="AU", currency="AUD")
        query = build_provider_search_query(request)
        candidate = {
            "source": "fixture",
            "href": "https://www.ebay.com.au/itm/124",
            "text": "Sold 29 May 2026\nPICK YOUR CARD Charizard ex 125/197\nAU $9.19\nFree postage",
        }
        comp = parse_candidate_dict(candidate, request=request, search_query=query, index=0)
        self.assertIsNotNone(comp)
        assert comp is not None
        evaluated = filter_comps(request.price_key, [comp])
        self.assertFalse(evaluated[0].included_in_estimate)
        self.assertEqual(evaluated[0].rejection_reason, "price_range_or_variation_listing")

    def test_generic_opens_title_falls_back_to_raw_snippet_title(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        query = build_provider_search_query(request)
        comp = parse_candidate_dict(
            {
                "source": "fixture",
                "href": "https://www.ebay.com.au/itm/126",
                "title": "Opens in a new window or tab",
                "text": (
                    "Sold 17 Mar 2026 Battle Partners - All Pokemon - Pick Your Own - Japanese - "
                    "Postage Discount Opens in a new window or tab\nAU $2.20\nFree postage"
                ),
            },
            request=request,
            search_query=query,
            index=0,
        )
        self.assertIsNotNone(comp)
        assert comp is not None
        self.assertEqual(comp.title, "Battle Partners - All Pokemon - Pick Your Own - Japanese - Postage Discount")
        evaluated = filter_comps(request.price_key, [comp])
        self.assertEqual(evaluated[0].rejection_reason, "price_range_or_variation_listing")

    def test_single_line_sold_snippet_extracts_pancham_title_without_ui_noise(self) -> None:
        request = sample_request(
            country="AU",
            currency="AUD",
            card_name="Pancham",
            normalized_card_name="pancham",
            set_name="Battle Partners",
            set_code="SV9",
            collector_number="050/100",
            language="jp",
            variant="non_holo",
        )
        query = build_provider_search_query(request)
        comp = parse_candidate_dict(
            {
                "source": "fixture",
                "href": "https://www.ebay.com.au/itm/127",
                "title": "Opens in a new window or tab",
                "text": (
                    "Sold 14 May 2026 050-100-SV9-B - Pokemon Card - Japanese - Pancham - C "
                    "Opens in a new window or tab Pre-owned AU $1.54 Buy It Now +AU $4.63 delivery "
                    "from Japan Free returns View similar active items Sell one like this midorigame "
                    "99.6% positive (29.7K)"
                ),
                "priceText": "AU $1.54",
                "shippingText": "+AU $4.63 delivery",
            },
            request=request,
            search_query=query,
            index=0,
        )
        self.assertIsNotNone(comp)
        assert comp is not None
        self.assertEqual(comp.title, "050-100-SV9-B - Pokemon Card - Japanese - Pancham - C")
        evaluated = filter_comps(request.price_key, [comp])
        self.assertTrue(evaluated[0].included_in_estimate)

    def test_quality_summary_counts(self) -> None:
        request = sample_request(country="AU", currency="AUD")
        query = build_provider_search_query(request)
        good = parse_candidate_dict(
            {
                "href": "https://www.ebay.com.au/itm/1",
                "text": "Sold 29 May 2026\nCharizard ex 125/197 Pokemon Card\nAU $9.19\nFree postage",
            },
            request=request,
            search_query=query,
            index=0,
        )
        range_comp = parse_candidate_dict(
            {
                "href": "https://www.ebay.com.au/itm/2",
                "text": "Sold 29 May 2026\nChoose Your Card Charizard ex 125/197\nAU $1.99 to AU $23.09\nFree postage",
            },
            request=request,
            search_query=query,
            index=1,
        )
        assert good is not None and range_comp is not None
        summary = build_quality_summary([good, range_comp], request=request)
        self.assertEqual(summary["total_parsed"], 2)
        self.assertEqual(summary["range_price_count"], 1)
        self.assertEqual(summary["likely_pick_your_card_count"], 1)
        self.assertEqual(summary["useful_candidate_count"], 1)
        self.assertEqual(summary["fallback_price_used_count"], 2)
        self.assertEqual(summary["direct_item_url_count"], 2)
        self.assertEqual(summary["generic_url_count"], 0)
        self.assertEqual(summary["missing_url_count"], 0)

    def test_marketplace_scope_diagnostics(self) -> None:
        with patch.dict(os.environ, {"EBAY_MARKET_SCOPE": "marketplace"}, clear=True):
            config = EbayBrowserProviderConfig.from_env()
        self.assertEqual(config.market_scope, "marketplace")
        self.assertEqual(config.safe_diagnostics()["marketScope"], "marketplace")

    def test_price_range_text_detection(self) -> None:
        self.assertTrue(is_price_range_text("AU $1.99 to AU $1,386.35"))
        self.assertTrue(is_price_range_text("AU $2.14 to AU $23.09"))
        self.assertFalse(is_price_range_text("AU $9.19"))

    def test_candidate_parser_ignores_fake_cards_without_item_url(self) -> None:
        request = sample_request(country="AU", currency="AUD")
        query = build_provider_search_query(request)
        candidate = {
            "source": "fixture",
            "href": "https://www.ebay.com.au/sch/i.html",
            "text": "Shop on eBay\nAU $9.19",
        }
        self.assertIsNone(parse_candidate_dict(candidate, request=request, search_query=query, index=0))

    def test_sold_date_parser_handles_common_formats(self) -> None:
        self.assertEqual(parse_sold_date_text("Sold May 20, 2026").year, 2026)
        self.assertEqual(parse_sold_date_text("20 May 2026").month, 5)

    def test_block_detection_text_detection(self) -> None:
        self.assertTrue(contains_block_marker(title="Verify yourself", body_text="Are you a robot?"))
        self.assertTrue(contains_block_marker(title="", body_text="Access denied"))
        self.assertFalse(contains_block_marker(title="Charizard listings", body_text="Sold results"))

    def test_provider_diagnostics_redacts_secrets(self) -> None:
        clean = sanitize_provider_diagnostics(
            {
                "apiKey": "abc",
                "Authorization": "Bearer token",
                "nested": {"cookie": "session=secret", "providerDomain": "ebay.com.au"},
            }
        )
        self.assertEqual(clean["apiKey"], "***REDACTED***")
        self.assertEqual(clean["Authorization"], "***REDACTED***")
        self.assertEqual(clean["nested"]["cookie"], "***REDACTED***")
        self.assertEqual(clean["nested"]["providerDomain"], "ebay.com.au")

    def test_selector_count_helper_works(self) -> None:
        class FakeLocator:
            def __init__(self, count: int) -> None:
                self._count = count

            def count(self) -> int:
                return self._count

        class FakePage:
            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator({".s-item": 2, 'a[href*="/itm/"]': 1}.get(selector, 0))

        counts = count_candidate_selectors(FakePage())
        self.assertEqual(counts[".s-item"], 2)
        self.assertEqual(counts['a[href*="/itm/"]'], 1)

    def test_debug_artifact_summary_redacts_secrets(self) -> None:
        clean = sanitize_provider_diagnostics(
            {
                "browser_config": {"userDataDir": "D:/cardscanr-data/.browser_profiles/cardscanr"},
                "cookie": "secret-cookie",
                "Authorization": "Bearer abc",
            }
        )
        self.assertEqual(clean["cookie"], "***REDACTED***")
        self.assertEqual(clean["Authorization"], "***REDACTED***")

    def test_market_matrix_planning_without_live_network(self) -> None:
        self.assertEqual(
            plan_market_matrix("AU,US,GB,CA"),
            [
                {"market": "AU", "currency": "AUD"},
                {"market": "US", "currency": "USD"},
                {"market": "GB", "currency": "GBP"},
                {"market": "CA", "currency": "CAD"},
            ],
        )

    def test_card_matrix_default_plan_has_expected_cards(self) -> None:
        planned = plan_card_matrix()
        self.assertEqual(len(planned), 9)
        labels = [f"{item['card_name']} {item['collector_number']} {item['set_name']}" for item in planned]
        self.assertIn("Pancham 050/100 Battle Partners", labels)
        self.assertIn("Roxie's Performance 081/086 Chaos Rising", labels)

    def test_card_matrix_plan_reads_external_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "card_plan.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "card_name": "Pancham",
                            "collector_number": "050/100",
                            "set_name": "Battle Partners",
                            "language": "jp",
                            "variant": "non_holo",
                            "condition": "raw",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            planned = plan_card_matrix(str(path))
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["card_name"], "Pancham")
        self.assertEqual(planned[0]["variant"], "non_holo")

    def test_card_matrix_timeout_report_includes_stage_fields_without_live_ebay(self) -> None:
        import scripts.debug_ebay_browser_card_matrix as card_matrix

        row = {
            "card_name": "Pancham",
            "collector_number": "050/100",
            "set_name": "Battle Partners",
            "set_code": "",
            "language": "jp",
            "variant": "non_holo",
            "condition": "raw",
        }
        args = type(
            "Args",
            (),
            {
                "max_results": 30,
                "headed": True,
                "lookup_timeout_seconds": 30,
                "browser_launch_timeout_seconds": 120,
                "per_query_timeout_seconds": 120,
                "total_card_timeout_seconds": 30,
            },
        )()
        with tempfile.TemporaryDirectory() as tmp:
            artifact_root = Path(tmp) / "artifacts"
            artifact_dir = artifact_root / "pancham-050-100-battle-partners" / "au"
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "debug_summary.json").write_text(
                json.dumps(
                    {
                        "stage_timings": {
                            "timedOutStage": "wait_for_result_container",
                            "stageDurationsMs": {"wait_for_result_container": 30000.0},
                        },
                        "failed_query_attempts": [
                            {"query_index": 0, "timed_out_stage": "wait_for_result_container"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(card_matrix, "ARTIFACT_ROOT", artifact_root):
                with patch("scripts.debug_ebay_browser_card_matrix.subprocess.run") as run:
                    run.side_effect = subprocess.TimeoutExpired(cmd=["python"], timeout=30)
                    payload = _run_single_lookup(row=row, market="AU", currency="AUD", args=args)

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_type"], "TimeoutExpired")
        self.assertEqual(payload["timed_out_stage"], "wait_for_result_container")
        self.assertEqual(payload["stage_timings"]["stageDurationsMs"]["wait_for_result_container"], 30000.0)
        self.assertEqual(payload["failed_query_attempts"][0]["timed_out_stage"], "wait_for_result_container")

    def test_live_write_smoke_requires_confirmation(self) -> None:
        args = type(
            "Args",
            (),
            {
                "market": "AU",
                "currency": "AUD",
                "card_name": "Charizard ex",
                "collector_number": "125/197",
                "set_name": "Obsidian Flames",
                "set_code": "sv03",
                "condition": "raw",
                "variant": "raw",
            },
        )()
        with patch.dict(os.environ, {"MARKET_LOOKUP_PROVIDER": "ebay_browser", "ENABLE_EBAY_REAL_LOOKUP": "true"}, clear=True):
            with self.assertRaises(RuntimeError):
                run_live_write_smoke(args)

    def test_live_write_smoke_force_refresh_passes_force_to_rpc(self) -> None:
        import scripts.smoke_ebay_browser_live_write as smoke

        class FakeClient:
            last_instance: "FakeClient | None" = None

            def __init__(self, **_kwargs: object) -> None:
                self.force_refresh: bool | None = None
                FakeClient.last_instance = self

            def request_market_price_refresh(self, **kwargs: object) -> dict:
                self.force_refresh = bool(kwargs.get("force_refresh"))
                return {"action": "cache_fresh", "cache_is_fresh": True}

            def get_market_price_bundle(self, **_kwargs: object) -> dict:
                return {
                    "cache": {"current_market_price": 13.0, "recommended_price": 13.0, "median_price": 13.0},
                    "latest_snapshot": {"diagnostics_json": {"priceViews": {}}},
                    "sold_listing_evidence": [],
                }

        fake_config = type(
            "Config",
            (),
            {
                "supabase_url": "https://example.supabase.co",
                "supabase_service_role_key": "secret",
                "worker_id": "worker-test",
            },
        )()
        args = type(
            "Args",
            (),
            {
                "market": "AU",
                "currency": "AUD",
                "card_name": "Charizard ex",
                "collector_number": "125/197",
                "set_name": "Obsidian Flames",
                "set_code": "sv03",
                "condition": "raw",
                "variant": "raw",
                "force_refresh": True,
            },
        )()
        with patch.dict(
            os.environ,
            {
                "MARKET_LOOKUP_PROVIDER": "ebay_browser",
                "ENABLE_EBAY_REAL_LOOKUP": "true",
                "CONFIRM_LIVE_EBAY_WRITE": "true",
            },
            clear=True,
        ):
            with patch.object(smoke.MarketEngineConfig, "from_env", return_value=fake_config):
                with patch.object(smoke, "SupabaseMarketEngineClient", FakeClient):
                    report = run_live_write_smoke(args)

        self.assertTrue(FakeClient.last_instance.force_refresh)
        self.assertTrue(report["force_refresh_requested"])
        self.assertFalse(report["pricing_model_validated"])

    def test_live_write_smoke_dry_run_requires_no_confirmation_or_secrets(self) -> None:
        args = type(
            "Args",
            (),
            {
                "market": "AU",
                "currency": "AUD",
                "card_name": "Riolu",
                "collector_number": "050/131",
                "set_name": "Prismatic Evolutions",
                "set_code": "sv8pt5",
                "condition": "raw",
                "variant": "reverse_holo",
                "force_refresh": False,
                "dry_run": True,
            },
        )()
        with patch.dict(os.environ, {}, clear=True):
            report = run_live_write_smoke(args)

        self.assertEqual(report["status"], "dry_run")
        self.assertEqual(report["job_status"], "dry_run")
        self.assertEqual(report["request_market_price_refresh"]["action"], "dry_run_only")
        self.assertEqual(report["identity"]["fingerprint"], "pokemon|en|sv8pt5|050/131|riolu|reverse_holo|raw|au|aud")
        self.assertEqual(report["market"]["provider_marketplace_id"], "EBAY_AU")

    def test_live_write_smoke_reports_final_completed_job_status_and_run_debug_artifacts(self) -> None:
        import scripts.smoke_ebay_browser_live_write as smoke

        class FakeClient:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def request_market_price_refresh(self, **_kwargs: object) -> dict:
                return {
                    "action": "job_enqueued",
                    "price_key_id": "key-au",
                    "job_id": "11111111-1111-1111-1111-111111111111",
                    "job_status": "queued",
                }

            def get_refresh_job(self, *, job_id: str) -> MarketPriceRefreshJob:
                return MarketPriceRefreshJob(
                    id=job_id,
                    price_key_id="key-au",
                    reason="unit",
                    priority=10,
                    status="queued",
                    attempt_count=0,
                )

            def claim_specific_refresh_job(self, *, job_id: str, worker_id: str) -> MarketPriceRefreshJob:
                return MarketPriceRefreshJob(
                    id=job_id,
                    price_key_id="key-au",
                    reason=worker_id,
                    priority=10,
                    status="running",
                    attempt_count=1,
                )

            def get_market_price_bundle(self, **_kwargs: object) -> dict:
                return {
                    "state": "existing_fresh_cache",
                    "cache_state": "fresh",
                    "refresh_state": "completed",
                    "current_market_evidence_available": True,
                    "cache": {
                        "id": "cache-au",
                        "current_market_price": 1.89,
                        "recommended_price": 1.89,
                        "sample_size": 1,
                        "confidence": "medium",
                    },
                    "latest_snapshot": {
                        "id": "snapshot-au",
                        "included_count": 1,
                        "rejected_count": 0,
                        "diagnostics_json": {"priceViews": {"itemPrice": {}, "landedPrice": {}}},
                    },
                    "sold_listing_evidence": [
                        {
                            "included_in_estimate": True,
                            "sold_date": None,
                            "raw_json": {"compQuality": {"detected_variant": "reverse_holo"}},
                        }
                    ],
                }

        class FakeRunner:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def run_job(self, job: MarketPriceRefreshJob) -> dict:
                debug_dir = Path(os.environ["EBAY_BROWSER_DEBUG_ARTIFACT_DIR"])
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / "debug_summary.json").write_text('{"query_text":"Riolu 050/131 Pokemon"}\n', encoding="utf-8")
                (debug_dir / "screenshot.png").write_bytes(b"riolu-png")
                return {"status": "completed", "jobId": job.id, "cacheRowId": "cache-au", "snapshotId": "snapshot-au"}

        fake_config = type(
            "Config",
            (),
            {
                "supabase_url": "https://example.supabase.co",
                "supabase_service_role_key": "secret",
                "worker_id": "worker-test",
            },
        )()
        args = type(
            "Args",
            (),
            {
                "market": "AU",
                "currency": "AUD",
                "card_name": "Riolu",
                "collector_number": "050/131",
                "set_name": "Prismatic Evolutions",
                "set_code": "sv8pt5",
                "condition": "raw",
                "variant": "reverse_holo",
                "force_refresh": True,
                "dry_run": False,
            },
        )()
        with tempfile.TemporaryDirectory() as tmp:
            debug_dir = Path(tmp) / "reports" / "ebay_browser_debug" / "live_write" / "latest"
            global_debug_dir = Path(tmp) / "reports" / "ebay_browser_debug" / "latest"
            with patch.dict(
                os.environ,
                {
                    "MARKET_LOOKUP_PROVIDER": "ebay_browser",
                    "ENABLE_EBAY_REAL_LOOKUP": "true",
                    "CONFIRM_LIVE_EBAY_WRITE": "true",
                },
                clear=True,
            ):
                with patch.object(smoke, "LIVE_WRITE_DEBUG_DIR", debug_dir):
                    with patch.object(smoke, "GLOBAL_DEBUG_LATEST_DIR", global_debug_dir):
                        with patch.object(smoke.MarketEngineConfig, "from_env", return_value=fake_config):
                            with patch.object(smoke, "SupabaseMarketEngineClient", FakeClient):
                                with patch.object(smoke, "MarketPriceJobRunner", FakeRunner):
                                    with patch.object(smoke, "create_market_comps_provider", return_value=object()):
                                        report = run_live_write_smoke(args)

        self.assertEqual(report["request_market_price_refresh"]["job_status"], "queued")
        self.assertEqual(report["job_status"], "completed")
        self.assertEqual(report["worker_result"]["status"], "completed")
        self.assertEqual(report["bundle_refresh_state"], "completed")
        self.assertTrue(report["debug_artifacts"]["debug_summary_exists"])
        self.assertTrue(report["debug_artifacts"]["screenshot_exists"])
        self.assertTrue(report["debug_artifacts"]["mirrored_to_global_latest"])
        self.assertEqual(report["top_included_comps"][0]["sold_date"], None)

    def test_live_write_smoke_without_force_respects_cooldown(self) -> None:
        flags = _validation_flags(action="cache_fresh", worker_result=None)
        self.assertFalse(flags["live_lookup_performed"])
        self.assertTrue(flags["used_cached_result"])
        self.assertFalse(flags["pricing_model_validated"])
        self.assertIn("-ForceRefresh", flags["message"])

    def test_live_write_smoke_processed_report_marks_pricing_model_validated(self) -> None:
        flags = _validation_flags(action="job_enqueued", worker_result={"status": "completed"})
        self.assertTrue(flags["live_lookup_performed"])
        self.assertFalse(flags["used_cached_result"])
        self.assertTrue(flags["pricing_model_validated"])

    def test_live_worker_batch_refuses_without_worker_confirmation(self) -> None:
        args = type(
            "Args",
            (),
            {
                "markets": "AU",
                "max_jobs": 1,
                "pause_between_jobs_seconds": 0,
                "force_refresh": False,
                "card_name": "Charizard ex",
                "collector_number": "125/197",
                "set_name": "Obsidian Flames",
                "set_code": "sv03",
                "condition": "raw",
                "variant": "raw",
            },
        )()
        with patch.dict(
            os.environ,
            {
                "MARKET_LOOKUP_PROVIDER": "ebay_browser",
                "ENABLE_EBAY_REAL_LOOKUP": "true",
                "CONFIRM_LIVE_EBAY_WRITE": "true",
            },
            clear=True,
        ):
            with self.assertRaises(RuntimeError):
                run_live_worker_batch(args)

    def test_live_worker_batch_refuses_without_write_confirmation(self) -> None:
        args = type(
            "Args",
            (),
            {
                "markets": "AU",
                "max_jobs": 1,
                "pause_between_jobs_seconds": 0,
                "force_refresh": False,
                "card_name": "Charizard ex",
                "collector_number": "125/197",
                "set_name": "Obsidian Flames",
                "set_code": "sv03",
                "condition": "raw",
                "variant": "raw",
            },
        )()
        with patch.dict(
            os.environ,
            {
                "MARKET_LOOKUP_PROVIDER": "ebay_browser",
                "ENABLE_EBAY_REAL_LOOKUP": "true",
                "CONFIRM_LIVE_EBAY_WORKER": "true",
            },
            clear=True,
        ):
            with self.assertRaises(RuntimeError):
                run_live_worker_batch(args)

    def test_live_worker_batch_default_plan_is_au_only(self) -> None:
        args = type("Args", (), {"markets": "AU", "max_jobs": 1})()
        self.assertEqual(live_worker_default_plan(args), [{"market": "AU", "currency": "AUD"}])
        self.assertEqual(getattr(args, "max_jobs"), 1)

    def test_live_worker_batch_market_list_parsing(self) -> None:
        self.assertEqual(
            parse_worker_market_list("AU,US,GB,CA"),
            [
                {"market": "AU", "currency": "AUD"},
                {"market": "US", "currency": "USD"},
                {"market": "GB", "currency": "GBP"},
                {"market": "CA", "currency": "CAD"},
            ],
        )
        self.assertEqual(parse_worker_market_list(["AU", "US"]), [{"market": "AU", "currency": "AUD"}, {"market": "US", "currency": "USD"}])

    def test_live_worker_batch_force_refresh_and_cache_fresh_skip(self) -> None:
        import scripts.smoke_ebay_browser_live_worker_batch as batch

        class FakeClient:
            last_instance: "FakeClient | None" = None

            def __init__(self, **_kwargs: object) -> None:
                self.request_calls: list[dict] = []
                FakeClient.last_instance = self

            def request_market_price_refresh(self, **kwargs: object) -> dict:
                self.request_calls.append(kwargs)
                return {"action": "cache_fresh", "cache_is_fresh": True, "price_key_id": "pk-au"}

            def get_market_price_bundle(self, **_kwargs: object) -> dict:
                return {"cache": {}, "latest_snapshot": {"diagnostics_json": {"priceViews": {}}}, "sold_listing_evidence": []}

        fake_config = type(
            "Config",
            (),
            {"supabase_url": "https://example.supabase.co", "supabase_service_role_key": "secret", "worker_id": "worker-test"},
        )()
        args = type(
            "Args",
            (),
            {
                "markets": "AU",
                "max_jobs": 1,
                "pause_between_jobs_seconds": 0,
                "force_refresh": True,
                "card_name": "Charizard ex",
                "collector_number": "125/197",
                "set_name": "Obsidian Flames",
                "set_code": "sv03",
                "condition": "raw",
                "variant": "raw",
            },
        )()
        with patch.dict(
            os.environ,
            {
                "MARKET_LOOKUP_PROVIDER": "ebay_browser",
                "ENABLE_EBAY_REAL_LOOKUP": "true",
                "CONFIRM_LIVE_EBAY_WRITE": "true",
                "CONFIRM_LIVE_EBAY_WORKER": "true",
            },
            clear=True,
        ):
            with patch.object(batch.MarketEngineConfig, "from_env", return_value=fake_config):
                with patch.object(batch, "SupabaseMarketEngineClient", FakeClient):
                    report = run_live_worker_batch(args)

        self.assertEqual(report["cache_fresh_skipped_count"], 1)
        self.assertEqual(report["processed_job_count"], 0)
        self.assertTrue(FakeClient.last_instance.request_calls[0]["force_refresh"])
        self.assertTrue(report["markets"][0]["cache_fresh"])

    def test_live_worker_batch_processes_only_expected_job_id_and_key(self) -> None:
        import scripts.smoke_ebay_browser_live_worker_batch as batch
        from cardscanr_market_engine.models import MarketPriceRefreshJob

        class FakeClient:
            last_instance: "FakeClient | None" = None

            def __init__(self, **_kwargs: object) -> None:
                self.claimed: list[str] = []
                FakeClient.last_instance = self

            def request_market_price_refresh(self, **_kwargs: object) -> dict:
                return {"action": "job_enqueued", "job_id": "11111111-1111-1111-1111-111111111111", "price_key_id": "pk-au"}

            def get_refresh_job(self, *, job_id: str) -> MarketPriceRefreshJob:
                self.seen_job_id = job_id
                return MarketPriceRefreshJob(
                    id=job_id,
                    price_key_id="pk-au",
                    reason="live_ebay_worker_batch",
                    priority=10,
                    status="queued",
                    attempt_count=0,
                )

            def claim_specific_refresh_job(self, *, job_id: str, worker_id: str) -> MarketPriceRefreshJob:
                self.claimed.append(job_id)
                return MarketPriceRefreshJob(
                    id=job_id,
                    price_key_id="pk-au",
                    reason="live_ebay_worker_batch",
                    priority=10,
                    status="running",
                    attempt_count=1,
                )

            def get_market_price_bundle(self, **_kwargs: object) -> dict:
                return {"cache": {}, "latest_snapshot": {"diagnostics_json": {"priceViews": {}}}, "sold_listing_evidence": []}

        class FakeRunner:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def run_job(self, job: MarketPriceRefreshJob) -> dict:
                return {"status": "completed", "jobId": job.id, "snapshotId": "snapshot-1"}

        fake_config = type(
            "Config",
            (),
            {"supabase_url": "https://example.supabase.co", "supabase_service_role_key": "secret", "worker_id": "worker-test"},
        )()
        args = type(
            "Args",
            (),
            {
                "markets": "AU",
                "max_jobs": 1,
                "pause_between_jobs_seconds": 0,
                "force_refresh": True,
                "card_name": "Charizard ex",
                "collector_number": "125/197",
                "set_name": "Obsidian Flames",
                "set_code": "sv03",
                "condition": "raw",
                "variant": "raw",
            },
        )()
        with patch.dict(
            os.environ,
            {
                "MARKET_LOOKUP_PROVIDER": "ebay_browser",
                "ENABLE_EBAY_REAL_LOOKUP": "true",
                "CONFIRM_LIVE_EBAY_WRITE": "true",
                "CONFIRM_LIVE_EBAY_WORKER": "true",
            },
            clear=True,
        ):
            with patch.object(batch.MarketEngineConfig, "from_env", return_value=fake_config):
                with patch.object(batch, "SupabaseMarketEngineClient", FakeClient):
                    with patch.object(batch, "create_market_comps_provider", return_value=object()):
                        with patch.object(batch, "MarketPriceJobRunner", FakeRunner):
                            report = run_live_worker_batch(args)

        self.assertEqual(FakeClient.last_instance.claimed, ["11111111-1111-1111-1111-111111111111"])
        self.assertEqual(report["processed_job_count"], 1)
        self.assertEqual(report["jobs_processed"], ["11111111-1111-1111-1111-111111111111"])
        self.assertTrue(report["markets"][0]["processed"])

    def test_live_scheduler_refuses_real_enqueue_when_disabled_by_default(self) -> None:
        import scripts.smoke_ebay_browser_live_scheduler as live_scheduler

        fake_config = type(
            "Config",
            (),
            {"provider_name": "ebay_browser", "supabase_url": "https://example.supabase.co", "supabase_service_role_key": "secret"},
        )()
        args = type("Args", (), {"markets": "AU", "max_enqueues": 2, "dry_run": False, "real_enqueue": True})()
        with patch.dict(os.environ, {"ENABLE_EBAY_REAL_LOOKUP": "true"}, clear=True):
            with patch.object(live_scheduler.MarketEngineConfig, "from_env", return_value=fake_config):
                with self.assertRaises(RuntimeError):
                    run_live_scheduler(args, client=object())

    def test_live_scheduler_refuses_real_enqueue_without_confirmation(self) -> None:
        import scripts.smoke_ebay_browser_live_scheduler as live_scheduler

        fake_config = type(
            "Config",
            (),
            {"provider_name": "ebay_browser", "supabase_url": "https://example.supabase.co", "supabase_service_role_key": "secret"},
        )()
        args = type("Args", (), {"markets": "AU", "max_enqueues": 2, "dry_run": False, "real_enqueue": True})()
        with patch.dict(
            os.environ,
            {"ENABLE_EBAY_REAL_LOOKUP": "true", "ENABLE_LIVE_EBAY_SCHEDULER": "true"},
            clear=True,
        ):
            with patch.object(live_scheduler.MarketEngineConfig, "from_env", return_value=fake_config):
                with self.assertRaises(RuntimeError):
                    run_live_scheduler(args, client=object())

    def test_live_scheduler_dry_run_skips_and_does_not_enqueue(self) -> None:
        import scripts.smoke_ebay_browser_live_scheduler as live_scheduler

        class FakeClient:
            def __init__(self) -> None:
                self.requested: list[dict] = []

            def list_missing_cache_keys(self, **_kwargs: object) -> list[dict]:
                return [
                    {"id": "k-au-missing", "fingerprint": "f-au", "market_country": "au", "currency": "aud"},
                    {"id": "k-us-missing", "fingerprint": "f-us", "market_country": "us", "currency": "usd"},
                ]

            def list_cache_refresh_candidates(self, **_kwargs: object) -> list[dict]:
                return [
                    {"id": "k-au-fresh", "fingerprint": "f-au-fresh", "market_country": "au", "currency": "aud", "stale_after": "2099-01-01T00:00:00Z"},
                    {"id": "k-au-active", "fingerprint": "f-au-active", "market_country": "au", "currency": "aud", "stale_after": "2020-01-01T00:00:00Z"},
                ]

            def get_active_jobs_for_keys(self, *, price_key_ids: list[str]) -> dict:
                return {"k-au-active": {"id": "job-active", "status": "queued"}}

            def count_live_scheduler_jobs_today(self) -> int:
                return 0

            def get_price_key(self, price_key_id: str) -> MarketPriceKey:
                return MarketPriceKey(
                    id=price_key_id,
                    game="pokemon",
                    card_name="Charizard ex",
                    normalized_card_name="charizard ex",
                    set_name="Obsidian Flames",
                    set_code="sv03",
                    collector_number="125/197",
                    language="en",
                    variant="raw",
                    condition="raw",
                    market_country="au",
                    currency="aud",
                    fingerprint=f"fingerprint-{price_key_id}",
                )

            def request_market_price_refresh(self, **kwargs: object) -> dict:
                self.requested.append(kwargs)
                return {"action": "job_enqueued", "job_id": "job-1", "price_key_id": "k-au-missing"}

        fake_config = type(
            "Config",
            (),
            {"provider_name": "ebay_browser", "supabase_url": "https://example.supabase.co", "supabase_service_role_key": "secret"},
        )()
        args = type("Args", (), {"markets": "AU", "max_enqueues": 1, "dry_run": True, "real_enqueue": False})()
        with patch.dict(os.environ, {"LIVE_EBAY_SCHEDULER_MAX_KEYS_SCANNED_PER_RUN": "25"}, clear=True):
            with patch.object(live_scheduler.MarketEngineConfig, "from_env", return_value=fake_config):
                client = FakeClient()
                report = run_live_scheduler(args, client=client, now_func=lambda: datetime(2026, 5, 25, tzinfo=timezone.utc))
        self.assertTrue(report["dryRun"])
        self.assertEqual(report["summary"]["jobsWouldEnqueue"], 1)
        self.assertEqual(report["summary"]["jobsEnqueued"], 0)
        self.assertEqual(client.requested, [])
        reasons = {item["fingerprint"]: item["reason"] for item in report["candidateDecisions"]}
        self.assertEqual(reasons["f-us"], "market_not_allowed")
        self.assertEqual(reasons["f-au-fresh"], "not_stale")
        self.assertEqual(reasons["f-au-active"], "active_job_exists")

    def test_live_scheduler_real_enqueue_uses_request_rpc(self) -> None:
        import scripts.smoke_ebay_browser_live_scheduler as live_scheduler

        class FakeClient:
            def __init__(self) -> None:
                self.requested: list[dict] = []

            def list_missing_cache_keys(self, **_kwargs: object) -> list[dict]:
                return [{"id": "k-au", "fingerprint": "f-au", "market_country": "au", "currency": "aud"}]

            def list_cache_refresh_candidates(self, **_kwargs: object) -> list[dict]:
                return []

            def get_active_jobs_for_keys(self, *, price_key_ids: list[str]) -> dict:
                return {}

            def count_live_scheduler_jobs_today(self) -> int:
                return 0

            def get_price_key(self, price_key_id: str) -> MarketPriceKey:
                return MarketPriceKey(
                    id=price_key_id,
                    game="pokemon",
                    card_name="Charizard ex",
                    normalized_card_name="charizard ex",
                    set_name="Obsidian Flames",
                    set_code="sv03",
                    collector_number="125/197",
                    language="en",
                    variant="raw",
                    condition="raw",
                    market_country="au",
                    currency="aud",
                    fingerprint="fingerprint-k-au",
                )

            def request_market_price_refresh(self, **kwargs: object) -> dict:
                self.requested.append(kwargs)
                return {"action": "job_enqueued", "job_id": "job-1", "price_key_id": "k-au"}

        fake_config = type(
            "Config",
            (),
            {"provider_name": "ebay_browser", "supabase_url": "https://example.supabase.co", "supabase_service_role_key": "secret"},
        )()
        args = type("Args", (), {"markets": "AU", "max_enqueues": 2, "dry_run": False, "real_enqueue": True})()
        with patch.dict(
            os.environ,
            {
                "ENABLE_EBAY_REAL_LOOKUP": "true",
                "ENABLE_LIVE_EBAY_SCHEDULER": "true",
                "CONFIRM_LIVE_EBAY_SCHEDULER": "true",
                "LIVE_EBAY_SCHEDULER_DRY_RUN": "false",
                "LIVE_EBAY_SCHEDULER_DAILY_ENQUEUE_CAP": "20",
            },
            clear=True,
        ):
            with patch.object(live_scheduler.MarketEngineConfig, "from_env", return_value=fake_config):
                client = FakeClient()
                report = run_live_scheduler(args, client=client, now_func=lambda: datetime(2026, 5, 25, tzinfo=timezone.utc))
        self.assertEqual(report["summary"]["jobsEnqueued"], 1)
        self.assertEqual(client.requested[0]["reason"], "live_ebay_scheduler")
        self.assertFalse(client.requested[0]["force_refresh"])

    def test_live_scheduler_daily_cap_skips(self) -> None:
        import scripts.smoke_ebay_browser_live_scheduler as live_scheduler

        class FakeClient:
            def list_missing_cache_keys(self, **_kwargs: object) -> list[dict]:
                return [{"id": "k-au", "fingerprint": "f-au", "market_country": "au", "currency": "aud"}]

            def list_cache_refresh_candidates(self, **_kwargs: object) -> list[dict]:
                return []

            def get_active_jobs_for_keys(self, *, price_key_ids: list[str]) -> dict:
                return {}

            def count_live_scheduler_jobs_today(self) -> int:
                return 20

        fake_config = type(
            "Config",
            (),
            {"provider_name": "ebay_browser", "supabase_url": "https://example.supabase.co", "supabase_service_role_key": "secret"},
        )()
        args = type("Args", (), {"markets": "AU", "max_enqueues": 2, "dry_run": True, "real_enqueue": False})()
        with patch.dict(os.environ, {"LIVE_EBAY_SCHEDULER_DAILY_ENQUEUE_CAP": "20"}, clear=True):
            with patch.object(live_scheduler.MarketEngineConfig, "from_env", return_value=fake_config):
                report = run_live_scheduler(args, client=FakeClient(), now_func=lambda: datetime(2026, 5, 25, tzinfo=timezone.utc))
        self.assertEqual(report["summary"]["jobsWouldEnqueue"], 0)
        self.assertEqual(report["summary"]["skipped"]["daily_cap_reached"], 1)

    def test_bulk_worker_requires_live_confirmation(self) -> None:
        import workers.market_price_worker as worker

        fake_config = type(
            "Config",
            (),
            {
                "provider_name": "ebay_browser",
                "worker_concurrency": 1,
            },
        )()
        with patch.dict(os.environ, {"MARKET_LOOKUP_PROVIDER": "ebay_browser", "ENABLE_EBAY_REAL_LOOKUP": "true"}, clear=True):
            with patch.object(worker.MarketEngineConfig, "from_env", return_value=fake_config):
                with patch.object(worker, "parse_args", return_value=type("Args", (), {})()):
                    with self.assertRaises(ValueError):
                        worker.main()

    def test_upload_bundle_excludes_html_and_secret_paths_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            debug_dir = root / "reports" / "ebay_browser_debug" / "latest"
            debug_dir.mkdir(parents=True)
            (debug_dir / "debug_summary.json").write_text(
                '{"status":"success","apiKey":"secret","providerDomain":"ebay.com.au"}\n',
                encoding="utf-8",
            )
            (debug_dir / "screenshot.png").write_bytes(b"png")
            (debug_dir / "page.html").write_text("<html>listing</html>", encoding="utf-8")
            (root / "supabase_env.local.json").write_text('{"SUPABASE_SERVICE_ROLE_KEY":"secret"}', encoding="utf-8")
            (root / ".browser_profiles" / "cardscanr").mkdir(parents=True)
            (root / ".browser_profiles" / "cardscanr" / "Cookies").write_text("secret", encoding="utf-8")
            bundle = create_bundle(kind="ebay_browser_debug", root=root)
            with ZipFile(bundle) as zip_file:
                names = set(zip_file.namelist())
                self.assertIn("reports/ebay_browser_debug/latest/debug_summary.json", names)
                self.assertIn("reports/ebay_browser_debug/latest/screenshot.png", names)
                self.assertNotIn("reports/ebay_browser_debug/latest/page.html", names)
                self.assertNotIn("supabase_env.local.json", names)
                self.assertFalse(any(".browser_profiles" in name for name in names))
                summary = zip_file.read("reports/ebay_browser_debug/latest/debug_summary.json").decode("utf-8")
                self.assertIn("***REDACTED***", summary)
                self.assertNotIn("secret", summary)

    def test_upload_bundle_includes_html_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            debug_dir = root / "reports" / "ebay_browser_debug" / "latest"
            debug_dir.mkdir(parents=True)
            (debug_dir / "debug_summary.json").write_text('{"status":"success"}\n', encoding="utf-8")
            (debug_dir / "page.html").write_text("<html>listing</html>", encoding="utf-8")
            bundle = create_bundle(kind="ebay_browser_debug", root=root, include_html=True)
            with ZipFile(bundle) as zip_file:
                self.assertIn("reports/ebay_browser_debug/latest/page.html", set(zip_file.namelist()))

    def test_upload_bundle_missing_optional_files_does_not_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = create_bundle(kind="market_price_engine_smoke", root=root)
            with ZipFile(bundle) as zip_file:
                self.assertIn("bundle_manifest.json", set(zip_file.namelist()))

    def test_live_worker_batch_upload_bundle_excludes_secret_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            (reports / "ebay_browser_debug" / "live_worker_batch" / "latest" / "au").mkdir(parents=True)
            (reports / "ebay_browser_live_worker_batch_latest.json").write_text('{"apiKey":"secret","status":"success"}', encoding="utf-8")
            (reports / "ebay_browser_live_worker_batch_runs.jsonl").write_text('{"token":"secret"}\n', encoding="utf-8")
            (reports / "ebay_browser_debug" / "live_worker_batch" / "latest" / "au" / "debug_summary.json").write_text(
                '{"cookie":"secret","result_count":1}', encoding="utf-8"
            )
            (reports / "ebay_browser_debug" / "live_worker_batch" / "latest" / "au" / "screenshot.png").write_bytes(b"png")
            (root / ".browser_profiles" / "cardscanr").mkdir(parents=True)
            (root / ".browser_profiles" / "cardscanr" / "Cookies").write_text("secret", encoding="utf-8")
            bundle = create_bundle(kind="ebay_browser_live_worker_batch", root=root)
            with ZipFile(bundle) as zip_file:
                names = set(zip_file.namelist())
                self.assertIn("reports/ebay_browser_live_worker_batch_latest.json", names)
                self.assertIn("reports/ebay_browser_debug/live_worker_batch/latest/au/debug_summary.json", names)
                self.assertFalse(any(".browser_profiles" in name for name in names))
                latest = zip_file.read("reports/ebay_browser_live_worker_batch_latest.json").decode("utf-8")
                self.assertIn("***REDACTED***", latest)
                self.assertNotIn("secret", latest)

    def test_card_matrix_upload_bundle_includes_reports_and_redacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            reports.mkdir(parents=True)
            (reports / "ebay_browser_card_matrix_latest.json").write_text('{"apiKey":"secret","status":"partial"}', encoding="utf-8")
            (reports / "ebay_browser_card_matrix_runs.jsonl").write_text('{"token":"secret"}\n', encoding="utf-8")
            card_debug = reports / "ebay_browser_debug" / "card_matrix" / "latest" / "pancham-050-100-battle-partners" / "au"
            card_debug.mkdir(parents=True)
            (card_debug / "card_qa_report.json").write_text('{"Authorization":"Bearer secret","status":"success"}', encoding="utf-8")
            (card_debug / "debug_summary.json").write_text('{"cookie":"secret"}', encoding="utf-8")
            (card_debug / "screenshot.png").write_bytes(b"png")

            bundle = create_bundle(kind="ebay_browser_card_matrix", root=root)
            with ZipFile(bundle) as zip_file:
                names = set(zip_file.namelist())
                self.assertIn("reports/ebay_browser_card_matrix_latest.json", names)
                self.assertIn(
                    "reports/ebay_browser_debug/card_matrix/latest/pancham-050-100-battle-partners/au/card_qa_report.json",
                    names,
                )
                latest = zip_file.read("reports/ebay_browser_card_matrix_latest.json").decode("utf-8")
                report = zip_file.read(
                    "reports/ebay_browser_debug/card_matrix/latest/pancham-050-100-battle-partners/au/card_qa_report.json"
                ).decode("utf-8")
                self.assertIn("***REDACTED***", latest)
                self.assertIn("***REDACTED***", report)
                self.assertNotIn("secret", latest)
                self.assertNotIn("secret", report)

    def test_live_scheduler_upload_bundle_excludes_secret_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            reports.mkdir(parents=True)
            (reports / "ebay_browser_live_scheduler_latest.json").write_text('{"apiKey":"secret","status":"success"}', encoding="utf-8")
            (reports / "ebay_browser_live_scheduler_runs.jsonl").write_text('{"token":"secret"}\n', encoding="utf-8")
            (root / ".browser_profiles" / "cardscanr").mkdir(parents=True)
            (root / ".browser_profiles" / "cardscanr" / "Cookies").write_text("secret", encoding="utf-8")
            bundle = create_bundle(kind="ebay_browser_live_scheduler", root=root)
            with ZipFile(bundle) as zip_file:
                names = set(zip_file.namelist())
                self.assertIn("reports/ebay_browser_live_scheduler_latest.json", names)
                self.assertFalse(any(".browser_profiles" in name for name in names))
                latest = zip_file.read("reports/ebay_browser_live_scheduler_latest.json").decode("utf-8")
                self.assertIn("***REDACTED***", latest)
                self.assertNotIn("secret", latest)

    def test_live_write_upload_bundle_uses_live_write_debug_artifacts_not_global_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            live_debug = reports / "ebay_browser_debug" / "live_write" / "latest"
            stale_global = reports / "ebay_browser_debug" / "latest"
            live_debug.mkdir(parents=True)
            stale_global.mkdir(parents=True)
            (reports / "ebay_browser_live_write_smoke_latest.json").write_text('{"status":"success"}\n', encoding="utf-8")
            (reports / "ebay_browser_live_write_smoke_runs.jsonl").write_text('{"status":"success"}\n', encoding="utf-8")
            (live_debug / "debug_summary.json").write_text('{"query_text":"Riolu 050/131 Pokemon"}\n', encoding="utf-8")
            (live_debug / "screenshot.png").write_bytes(b"riolu-png")
            (stale_global / "debug_summary.json").write_text('{"query_text":"Lombre 022/100 Japanese Pokemon"}\n', encoding="utf-8")
            (stale_global / "screenshot.png").write_bytes(b"lombre-png")

            bundle = create_bundle(kind="ebay_browser_live_write_smoke", root=root)

            with ZipFile(bundle) as zip_file:
                names = set(zip_file.namelist())
                self.assertIn("reports/ebay_browser_debug/live_write/latest/debug_summary.json", names)
                self.assertIn("reports/ebay_browser_debug/live_write/latest/screenshot.png", names)
                self.assertNotIn("reports/ebay_browser_debug/latest/debug_summary.json", names)
                summary = zip_file.read("reports/ebay_browser_debug/live_write/latest/debug_summary.json").decode("utf-8")
                self.assertIn("Riolu", summary)
                self.assertNotIn("Lombre", summary)

    def test_live_write_report_summary_includes_item_and_landed_stats(self) -> None:
        summary = _summarize_bundle(
            {
                "cache": {
                    "current_market_price": 13.0,
                    "recommended_price": 13.0,
                    "median_price": 13.0,
                    "sample_size": 3,
                    "confidence": "medium",
                    "marketplace": "EBAY_AU",
                    "market_country": "AU",
                    "currency": "AUD",
                },
                "latest_snapshot": {
                    "included_count": 2,
                    "rejected_count": 1,
                    "diagnostics_json": {
                        "priceViews": {
                            "priceBasis": "item_price",
                            "landedPriceAvailable": True,
                            "itemPrice": {"recommended": 13.0, "median": 13.0, "low": 9.0, "high": 20.0},
                            "landedPrice": {"recommended": 24.0, "median": 24.0, "low": 13.0, "high": 40.0},
                        }
                    },
                },
                "sold_listing_evidence": [
                    {
                        "included_in_estimate": True,
                        "raw_json": {"compQuality": {"detected_variant": "non_holo"}},
                    },
                    {
                        "included_in_estimate": False,
                        "rejection_reason": "wrong_variant_reverse_holo",
                        "raw_json": {"compQuality": {"detected_variant": "reverse_holo"}},
                    },
                ],
            }
        )
        cache_summary = summary["cache_price_summary"]
        self.assertEqual(cache_summary["price_basis"], "item_price")
        self.assertTrue(cache_summary["landed_price_available"])
        self.assertEqual(cache_summary["item_recommended_price"], 13.0)
        self.assertEqual(cache_summary["landed_recommended_price"], 24.0)
        self.assertEqual(cache_summary["included_count"], 2)
        self.assertEqual(cache_summary["rejected_count"], 1)
        self.assertEqual(summary["included_variants_summary"], {"non_holo": 1})
        self.assertEqual(summary["rejected_variant_mismatch_count"], 1)


@unittest.skipUnless(
    os.getenv("ENABLE_EBAY_REAL_LOOKUP", "").lower() == "true"
    and os.getenv("RUN_LIVE_EBAY_PROVIDER_TEST", "").lower() == "true",
    "Live eBay provider test requires ENABLE_EBAY_REAL_LOOKUP=true and RUN_LIVE_EBAY_PROVIDER_TEST=true",
)
class LiveEbayProviderTests(unittest.TestCase):
    def test_live_ebay_provider_fetches_without_writing(self) -> None:
        provider = EbayBrowserSoldCompsProvider()
        result = provider.fetch_comps(sample_request(country="AU", currency="AUD"))
        self.assertEqual(result.provider_name, "ebay_browser")


if __name__ == "__main__":
    unittest.main()
