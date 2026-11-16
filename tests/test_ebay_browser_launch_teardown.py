from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.models import ProviderRequest  # noqa: E402
from cardscanr_market_engine.providers.ebay_browser_provider import (  # noqa: E402
    EbayBrowserProviderConfig,
    EbayBrowserSoldCompsProvider,
)


def _minimal_request() -> ProviderRequest:
    from cardscanr_market_engine.marketplaces import resolve_marketplace_config
    from cardscanr_market_engine.models import MarketPriceKey

    market = resolve_marketplace_config(market_country="AU", currency="AUD", marketplace="ebay")
    key = MarketPriceKey(
        id="key-1",
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
        fingerprint="pokemon|en|sv03|125/197|charizard-ex|raw|raw|au|aud",
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


class EbayBrowserLaunchTeardownTests(unittest.TestCase):
    def test_launch_session_teardown_when_persistent_context_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
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
                user_data_dir=Path(tmp),
                market_scope="marketplace",
                debug_artifact_dir=None,
                reuse_context=True,
            )
            provider = EbayBrowserSoldCompsProvider(config=config)
            mock_pw = mock.Mock(name="playwright_driver")
            mock_sync = mock.Mock()
            mock_sync.start.return_value = mock_pw
            mock_pw.chromium.launch_persistent_context.side_effect = RuntimeError("launch failed")

            with mock.patch("playwright.sync_api.sync_playwright", return_value=mock_sync):
                with mock.patch(
                    "cardscanr_market_engine.providers.ebay_browser_provider.teardown_persistent_context"
                ) as teardown:
                    with self.assertRaises(RuntimeError):
                        provider._ensure_browser_session(request=_minimal_request())
                    pw_teardowns = [
                        call.kwargs
                        for call in teardown.call_args_list
                        if call.kwargs.get("pw") is mock_pw
                    ]
                    self.assertTrue(pw_teardowns, "expected launch closure to tear down Playwright driver")
                    self.assertIsNone(pw_teardowns[0].get("context"))

    def test_launch_session_teardown_when_new_page_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
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
                user_data_dir=Path(tmp),
                market_scope="marketplace",
                debug_artifact_dir=None,
                reuse_context=True,
            )
            provider = EbayBrowserSoldCompsProvider(config=config)
            mock_pw = mock.Mock(name="playwright_driver")
            mock_context = mock.Mock(name="browser_context")
            mock_sync = mock.Mock()
            mock_sync.start.return_value = mock_pw
            mock_pw.chromium.launch_persistent_context.return_value = mock_context
            mock_context.new_page.side_effect = RuntimeError("new_page failed")

            with mock.patch("playwright.sync_api.sync_playwright", return_value=mock_sync):
                with mock.patch(
                    "cardscanr_market_engine.providers.ebay_browser_provider.teardown_persistent_context"
                ) as teardown:
                    with self.assertRaises(RuntimeError):
                        provider._ensure_browser_session(request=_minimal_request())
                    pw_teardowns = [
                        call.kwargs
                        for call in teardown.call_args_list
                        if call.kwargs.get("pw") is mock_pw
                    ]
                    self.assertTrue(pw_teardowns, "expected launch closure to tear down Playwright driver")
                    self.assertIs(pw_teardowns[0].get("context"), mock_context)


if __name__ == "__main__":
    unittest.main()