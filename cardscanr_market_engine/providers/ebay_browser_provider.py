from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

from ..models import ProviderRequest, ProviderResult, SoldComp
from .errors import (
    ProviderBlockedError,
    ProviderParseError,
    ProviderTemporaryError,
    ProviderUnsupportedMarketError,
    sanitize_provider_diagnostics,
)
from .query_builder import ProviderSearchQuery, build_provider_search_query


BLOCK_TEXT_MARKERS = ("captcha", "verify", "robot", "unusual traffic", "access denied", "blocked")
DEFAULT_SOLD_DATE = datetime(1970, 1, 1, tzinfo=timezone.utc)
SUPPORTED_MARKET_ROUTES = {("AU", "AUD"), ("US", "USD"), ("GB", "GBP"), ("CA", "CAD")}


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _normalise_text(value: object) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def contains_block_marker(*, title: str = "", body_text: str = "") -> bool:
    haystack = f"{title}\n{body_text}".lower()
    return any(marker in haystack for marker in BLOCK_TEXT_MARKERS)


def parse_price_text(text: str, *, expected_currency: str) -> tuple[float | None, str | None, dict[str, Any]]:
    clean = _normalise_text(text)
    if not clean:
        return None, None, {"rawText": text, "reason": "empty"}
    currency = expected_currency.upper()
    compact = clean.upper().replace(" ", "")
    detected_currency = currency
    if "£" in clean or "GBP" in compact:
        detected_currency = "GBP"
    elif "US$" in compact or "USD" in compact:
        detected_currency = "USD"
    elif "C$" in compact or "CA$" in compact or "CAD" in compact:
        detected_currency = "CAD"
    elif "A$" in compact or "AU$" in compact or "AUD" in compact:
        detected_currency = "AUD"
    match = re.search(r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)", clean)
    if match is None:
        return None, detected_currency, {"rawText": text, "reason": "no_numeric_price"}
    amount = float(match.group(1).replace(",", ""))
    diagnostics = {"rawText": text, "detectedCurrency": detected_currency}
    if detected_currency != currency:
        diagnostics["currencyMismatch"] = True
    return amount, detected_currency, diagnostics


def parse_shipping_text(text: str, *, expected_currency: str) -> tuple[float, dict[str, Any]]:
    clean = _normalise_text(text).lower()
    if not clean or "free" in clean:
        return 0.0, {"rawText": text, "freeShipping": "free" in clean}
    amount, currency, diagnostics = parse_price_text(text, expected_currency=expected_currency)
    diagnostics["detectedCurrency"] = currency
    return float(amount or 0.0), diagnostics


def parse_sold_date_text(text: str) -> datetime:
    clean = _normalise_text(text)
    if not clean:
        return DEFAULT_SOLD_DATE
    clean = re.sub(r"^sold\s+", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s+sold$", "", clean, flags=re.IGNORECASE)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(clean, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return DEFAULT_SOLD_DATE


@dataclass(frozen=True)
class EbayBrowserProviderConfig:
    headless: bool
    max_results: int
    timeout_seconds: int
    cooldown_seconds: int
    min_seconds_between_requests: int
    user_data_dir: str | None

    @classmethod
    def from_env(cls) -> "EbayBrowserProviderConfig":
        return cls(
            headless=_parse_bool("EBAY_BROWSER_HEADLESS", True),
            max_results=min(_parse_positive_int("EBAY_BROWSER_MAX_RESULTS", 30), 100),
            timeout_seconds=_parse_positive_int("EBAY_BROWSER_TIMEOUT_SECONDS", 45),
            cooldown_seconds=_parse_positive_int("EBAY_BROWSER_COOLDOWN_SECONDS", 20),
            min_seconds_between_requests=_parse_positive_int("EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS", 20),
            user_data_dir=os.getenv("EBAY_BROWSER_USER_DATA_DIR", "").strip() or None,
        )


class EbayBrowserSoldCompsProvider:
    provider_name = "ebay_browser"
    marketplace_name = "ebay"

    _request_lock = threading.Lock()
    _last_request_monotonic = 0.0

    def __init__(self, *, config: EbayBrowserProviderConfig | None = None) -> None:
        self.config = config or EbayBrowserProviderConfig.from_env()

    def _wait_for_request_slot(self) -> None:
        min_wait = max(self.config.cooldown_seconds, self.config.min_seconds_between_requests)
        with self._request_lock:
            now = time.monotonic()
            elapsed = now - self.__class__._last_request_monotonic
            if elapsed < min_wait:
                time.sleep(min_wait - elapsed)
            self.__class__._last_request_monotonic = time.monotonic()

    def fetch_comps(self, request: ProviderRequest) -> ProviderResult:
        route = (request.market_country.upper(), request.currency.upper())
        if route not in SUPPORTED_MARKET_ROUTES:
            raise ProviderUnsupportedMarketError(
                "eBay browser provider currently supports AU/AUD, US/USD, GB/GBP, and CA/CAD only",
                diagnostics={"marketCountry": request.market_country, "currency": request.currency},
            )
        search_query = build_provider_search_query(request)
        self._wait_for_request_slot()
        try:
            return self._fetch_with_playwright(request=request, search_query=search_query)
        except ProviderBlockedError:
            raise
        except ProviderParseError:
            raise
        except Exception as exc:
            raise ProviderTemporaryError(
                "eBay browser lookup failed temporarily",
                diagnostics={"errorType": type(exc).__name__, "providerDomain": request.provider_domain},
            ) from exc

    def _fetch_with_playwright(self, *, request: ProviderRequest, search_query: ProviderSearchQuery) -> ProviderResult:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise ProviderTemporaryError(
                "Playwright is not installed or is unavailable. Install dependency and run: python -m playwright install chromium",
                diagnostics={"errorType": type(exc).__name__},
            ) from exc

        timeout_ms = self.config.timeout_seconds * 1000
        with sync_playwright() as playwright:
            browser_or_context: Any = None
            context: Any = None
            try:
                if self.config.user_data_dir:
                    context = playwright.chromium.launch_persistent_context(
                        self.config.user_data_dir,
                        headless=self.config.headless,
                        locale=request.search_locale,
                        viewport={"width": 1366, "height": 900},
                    )
                else:
                    browser_or_context = playwright.chromium.launch(headless=self.config.headless)
                    context = browser_or_context.new_context(
                        locale=request.search_locale,
                        viewport={"width": 1366, "height": 900},
                    )
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                page.goto(search_query.search_url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15000))
                except PlaywrightTimeoutError:
                    pass

                title = page.title()
                body_text = page.locator("body").inner_text(timeout=5000)
                if contains_block_marker(title=title, body_text=body_text):
                    raise ProviderBlockedError(
                        "eBay returned a block or verification page; captcha bypass is not attempted",
                        diagnostics={
                            "pageTitle": title,
                            "providerDomain": search_query.provider_domain,
                            "searchUrlHost": urlparse(search_query.search_url).netloc,
                        },
                    )

                comps = self._parse_page(page=page, request=request, search_query=search_query)
                return ProviderResult(
                    provider_name=self.provider_name,
                    marketplace=search_query.provider_marketplace_id,
                    provider_fingerprint=self._provider_fingerprint(search_query),
                    query_used=search_query.query_text,
                    comps=comps,
                    raw_metadata=sanitize_provider_diagnostics(
                        {
                            "providerDomain": search_query.provider_domain,
                            "providerMarketplaceId": search_query.provider_marketplace_id,
                            "marketCountry": search_query.market_country,
                            "currency": search_query.currency,
                            "resultCount": len(comps),
                            "maxResults": self.config.max_results,
                            "headless": self.config.headless,
                            "queryDiagnostics": search_query.diagnostics,
                        }
                    ),
                )
            finally:
                if context is not None:
                    context.close()
                if browser_or_context is not None:
                    browser_or_context.close()

    def _parse_page(self, *, page: Any, request: ProviderRequest, search_query: ProviderSearchQuery) -> list[SoldComp]:
        cards = page.locator("li.s-item")
        count = min(cards.count(), self.config.max_results)
        comps: list[SoldComp] = []
        parse_errors: list[dict[str, Any]] = []
        for index in range(count):
            card = cards.nth(index)
            try:
                comp = self._parse_card(
                    card=card,
                    index=index,
                    request=request,
                    search_query=search_query,
                )
            except Exception as exc:
                parse_errors.append({"index": index, "errorType": type(exc).__name__})
                continue
            if comp is not None:
                comps.append(comp)
        if not comps and parse_errors:
            raise ProviderParseError(
                "eBay result page contained cards, but none could be parsed",
                diagnostics={"parseErrors": parse_errors[:5], "providerDomain": search_query.provider_domain},
            )
        return comps

    def _parse_card(
        self,
        *,
        card: Any,
        index: int,
        request: ProviderRequest,
        search_query: ProviderSearchQuery,
    ) -> SoldComp | None:
        raw_text = _normalise_text(card.inner_text(timeout=3000))
        title = self._first_inner_text(card, [".s-item__title span", ".s-item__title"])
        if not title or "shop on ebay" in title.lower():
            return None
        price_text = self._first_inner_text(card, [".s-item__price", ".s-item__detail--primary"])
        sold_price, detected_currency, price_diagnostics = parse_price_text(
            price_text,
            expected_currency=search_query.currency,
        )
        if sold_price is None:
            return None
        shipping_text = self._first_inner_text(card, [".s-item__shipping", ".s-item__logisticsCost"])
        shipping_price, shipping_diagnostics = parse_shipping_text(
            shipping_text,
            expected_currency=search_query.currency,
        )
        sold_date_text = self._first_inner_text(card, [".s-item__title--tagblock .POSITIVE", ".s-item__caption--row"])
        condition_text = self._first_inner_text(card, [".SECONDARY_INFO", ".s-item__subtitle"]) or ""
        href = self._first_attribute(card, ["a.s-item__link"], "href")
        if not href:
            return None
        listing_url = href.split("?", 1)[0]
        source_listing_id = self._source_listing_id(listing_url, index=index)
        return SoldComp(
            source_listing_id=source_listing_id,
            title=title,
            sold_price=round(sold_price, 2),
            shipping_price=round(shipping_price, 2),
            total_price=round(sold_price + shipping_price, 2),
            currency=(detected_currency or search_query.currency).upper(),
            sold_date=parse_sold_date_text(sold_date_text),
            listing_url=listing_url,
            condition_text=condition_text,
            raw_metadata=sanitize_provider_diagnostics(
                {
                    "providerDomain": search_query.provider_domain,
                    "providerMarketplaceId": search_query.provider_marketplace_id,
                    "marketCountry": request.market_country,
                    "expectedCurrency": search_query.currency,
                    "detectedCurrency": detected_currency,
                    "priceDiagnostics": price_diagnostics,
                    "shippingDiagnostics": shipping_diagnostics,
                    "soldDateText": sold_date_text,
                    "rawTextSnippet": raw_text[:500],
                }
            ),
        )

    def _first_inner_text(self, root: Any, selectors: list[str]) -> str:
        for selector in selectors:
            try:
                locator = root.locator(selector).first
                if locator.count() <= 0:
                    continue
                text = _normalise_text(locator.inner_text(timeout=1000))
                if text:
                    return text
            except Exception:
                continue
        return ""

    def _first_attribute(self, root: Any, selectors: list[str], attribute: str) -> str:
        for selector in selectors:
            try:
                locator = root.locator(selector).first
                if locator.count() <= 0:
                    continue
                value = locator.get_attribute(attribute, timeout=1000)
                if value:
                    return str(value)
            except Exception:
                continue
        return ""

    def _source_listing_id(self, listing_url: str, *, index: int) -> str:
        match = re.search(r"/itm/(?:[^/]+/)?([0-9]+)", listing_url)
        if match:
            return f"ebay-{match.group(1)}"
        digest = hashlib.sha256(f"{listing_url}|{index}".encode("utf-8")).hexdigest()[:16]
        return f"ebay-{digest}"

    def _provider_fingerprint(self, search_query: ProviderSearchQuery) -> str:
        digest = hashlib.sha256(search_query.search_url.encode("utf-8")).hexdigest()[:16]
        return f"ebay_browser:{search_query.provider_marketplace_id}:{digest}"
