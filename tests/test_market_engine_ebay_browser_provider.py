from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.marketplaces import resolve_marketplace_config  # noqa: E402
from cardscanr_market_engine.models import MarketPriceKey, ProviderRequest  # noqa: E402
from cardscanr_market_engine.filters import filter_comps  # noqa: E402
from cardscanr_market_engine.providers import MockMarketCompsProvider, create_market_comps_provider  # noqa: E402
from cardscanr_market_engine.providers.ebay_browser_provider import (  # noqa: E402
    EbayBrowserProviderConfig,
    EbayBrowserSoldCompsProvider,
    appears_to_be_personal_chrome_profile,
    build_quality_summary,
    contains_block_marker,
    count_candidate_selectors,
    is_price_range_text,
    parse_candidate_dict,
    parse_price_text,
    parse_shipping_text,
    parse_sold_date_text,
)
from cardscanr_market_engine.providers.errors import ProviderDisabledError, sanitize_provider_diagnostics  # noqa: E402
from cardscanr_market_engine.providers.errors import ProviderUnsupportedMarketError  # noqa: E402
from cardscanr_market_engine.providers.query_builder import build_provider_search_query  # noqa: E402
from scripts.debug_ebay_browser_market_matrix import plan_market_matrix  # noqa: E402
from scripts.smoke_ebay_browser_live_worker_batch import default_plan as live_worker_default_plan  # noqa: E402
from scripts.smoke_ebay_browser_live_worker_batch import parse_market_list as parse_worker_market_list  # noqa: E402
from scripts.smoke_ebay_browser_live_worker_batch import run_batch as run_live_worker_batch  # noqa: E402
from scripts.create_market_engine_upload_bundle import create_bundle  # noqa: E402
from scripts.smoke_ebay_browser_live_write import _summarize_bundle  # noqa: E402
from scripts.smoke_ebay_browser_live_write import _validation_flags  # noqa: E402
from scripts.smoke_ebay_browser_live_write import run_smoke as run_live_write_smoke  # noqa: E402


def sample_request(
    *,
    country: str = "AU",
    currency: str = "AUD",
    condition: str = "raw",
    variant: str = "raw",
) -> ProviderRequest:
    market = resolve_marketplace_config(market_country=country, currency=currency, marketplace="ebay")
    key = MarketPriceKey(
        id="key-1",
        game="pokemon",
        card_name="Charizard ex",
        normalized_card_name="charizard ex",
        set_name="Obsidian Flames",
        set_code="sv03",
        collector_number="125/197",
        language="en",
        variant=variant,
        condition=condition,
        market_country=country.lower(),
        currency=currency.lower(),
        fingerprint=f"pokemon|en|sv03|125-197|charizard-ex|{variant}|{condition}|{country.lower()}|{currency.lower()}",
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
        with patch.dict(os.environ, {}, clear=True):
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


class QueryBuilderTests(unittest.TestCase):
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

    def test_query_builder_excludes_raw_bad_terms(self) -> None:
        query = build_provider_search_query(sample_request())
        for term in ("proxy", "custom", "digital", "code", "jumbo", "lot", "bundle", "pack", "booster", "sealed", "psa", "cgc", "bgs", "graded"):
            self.assertIn(f"-{term}", query.query_text)

    def test_query_builder_handles_graded_condition(self) -> None:
        query = build_provider_search_query(sample_request(condition="psa_10", variant="graded"))
        self.assertNotIn("-psa", query.query_text)
        self.assertNotIn("-graded", query.query_text)
        self.assertIn("-proxy", query.query_text)


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
        self.assertEqual(comp.raw_metadata["soldDateText"], "Sold 29 May 2026")

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
                "sold_listing_evidence": [{"included_in_estimate": True}, {"included_in_estimate": False}],
            }
        )
        cache_summary = summary["cache_price_summary"]
        self.assertEqual(cache_summary["price_basis"], "item_price")
        self.assertTrue(cache_summary["landed_price_available"])
        self.assertEqual(cache_summary["item_recommended_price"], 13.0)
        self.assertEqual(cache_summary["landed_recommended_price"], 24.0)
        self.assertEqual(cache_summary["included_count"], 2)
        self.assertEqual(cache_summary["rejected_count"], 1)


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
