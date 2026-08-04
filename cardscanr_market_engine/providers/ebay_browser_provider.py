from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from ..config import DEFAULT_EBAY_BROWSER_PROFILE_NAME, DEFAULT_EBAY_BROWSER_USER_DATA_DIR, ROOT, MarketEngineConfig
from ..models import ProviderRequest, ProviderResult, SoldComp
from .errors import (
    ProviderAuthenticationRequiredError,
    ProviderBlockedError,
    ProviderDisabledError,
    ProviderError,
    ProviderIdentityUnavailableError,
    ProviderParseError,
    ProviderTemporaryError,
    ProviderUnsupportedMarketError,
    sanitize_provider_diagnostics,
)
from .identity_guard import ENGLISH_MARKET_IDENTITY_UNAVAILABLE, evaluate_english_market_identity
from .query_builder import ProviderSearchQuery, build_provider_search_queries


CHALLENGE_TEXT_MARKERS = (
    "captcha",
    "verify you are human",
    "verify yourself",
    "are you a robot",
    "security challenge",
    "robot check",
)
ACCESS_BLOCK_TEXT_MARKERS = ("access denied", "unusual traffic", "temporarily blocked", "blocked from using")
AUTH_TEXT_MARKERS = ("sign in to continue", "please sign in", "session expired", "log in to continue")
CONSENT_TEXT_MARKERS = ("accept all", "cookie consent", "privacy preferences")
MAINTENANCE_TEXT_MARKERS = ("technical difficulties", "temporarily unavailable", "site maintenance")
NO_RESULTS_TEXT_MARKERS = ("0 results", "no exact matches found", "no results for", "we looked everywhere")
RESULT_TEXT_MARKERS = ("sold items", "completed items", "results for", "shop by category")
BLOCK_TEXT_MARKERS = CHALLENGE_TEXT_MARKERS + ACCESS_BLOCK_TEXT_MARKERS
DEFAULT_SOLD_DATE = datetime(1970, 1, 1, tzinfo=timezone.utc)
SUPPORTED_MARKET_ROUTES = {("AU", "AUD"), ("US", "USD"), ("GB", "GBP"), ("CA", "CAD")}
DEBUG_REPORTS_DIR = ROOT / "reports" / "ebay_browser_debug"
RESULT_SELECTOR_COUNTS = (
    "li.s-item",
    ".s-item",
    "[data-view]",
    ".srp-results li",
    ".srp-results .s-item",
    "a.s-item__link",
    ".s-item__title",
    ".s-item__price",
    '.srp-results a[href*="/itm/"]',
    'a[href*="/itm/"]',
)
PROMO_TITLE_MARKERS = ("shop on ebay", "sponsored", "advertisement")
GENERIC_TITLE_MARKERS = (
    "opens in a new window or tab",
    "new listing",
    "image not available",
)
TITLE_UI_BOUNDARY_RE = re.compile(
    r"\s+(?:"
    r"opens\s+in\s+a\s+new\s+window\s+or\s+tab|"
    r"pre-owned|brand\s+new|"
    r"buy\s+it\s+now|best\s+offer|"
    r"view\s+similar\s+active\s+items|sell\s+one\s+like\s+this"
    r")\b",
    flags=re.IGNORECASE,
)
PICK_YOUR_CARD_PATTERNS = (
    "choose your card",
    "choose your own",
    "you pick",
    "pick your card",
    "pick your own",
    "select your card",
    "complete your set",
    "all pokemon pick",
    "card singles pick",
    "variation listing",
    "singles common",
    "holo/reverse/ex",
    "reverse/holo/ex",
)
LOT_BUNDLE_PATTERNS = (" lot ", " bundle ", " collection ", " bulk ", " card lot ", " holo lot ", " mixed lot ")
GRADED_PATTERNS = (" psa ", " bgs ", " cgc ", " sgc ", " graded ", " slab ")
SEALED_PATTERNS = (" booster ", " sealed ", " pack ", " etb ", " elite trainer box ")
SUPPORTED_EBAY_DOMAINS = ("ebay.com.au", "ebay.com", "ebay.co.uk", "ebay.ca")
EBAY_AUTH_PATH_MARKERS = ("/signin/", "/signin", "/login/", "/login", "/identity/")
DEFAULT_MAX_QUERY_ATTEMPTS = 5
RESULT_CONTAINER_SELECTOR = 'li.s-item, .srp-results, a[href*="/itm/"]'
DIAGNOSTIC_STAGES = (
    "cache_check",
    "browser_launch",
    "marketplace_attempt",
    "results_loaded",
    "comparables_filtered",
    "estimate_normalized",
    "complete",
)
MARKET_COUNTRY_NAMES = {
    "AU": ("australia", "australian"),
    "US": ("united states", "usa", "us "),
    "GB": ("united kingdom", "uk ", "great britain"),
    "CA": ("canada", "canadian"),
}
NON_PRICE_CONTEXT_RE = re.compile(
    r"(?:positive|feedback|product ratings?|stars?|watchers?|views?|seller)",
    flags=re.IGNORECASE,
)
PRICE_CONTEXT_RE = re.compile(
    r"(?:buy it now|best offer|bid|sold|delivery|shipping|postage)",
    flags=re.IGNORECASE,
)
AMOUNT_RE = r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
EXPLICIT_PRICE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("AUD", rf"(?:AU\s*\$|A\s*\$|AUD\s*)\s*{AMOUNT_RE}"),
    ("USD", rf"(?:US\s*\$|USD\s*)\s*{AMOUNT_RE}"),
    ("CAD", rf"(?:C\s*\$|CA\s*\$|CAD\s*)\s*{AMOUNT_RE}"),
    ("GBP", rf"(?:£|GBP\s*)\s*{AMOUNT_RE}"),
)
BARE_DOLLAR_RE = re.compile(r"(?<![A-Z])\$\s*" + AMOUNT_RE, flags=re.IGNORECASE)


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _parse_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalise_text(value: object) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def normalize_ebay_listing_url(href: str, *, provider_domain: str) -> dict[str, str | None]:
    original_href = str(href or "").strip()
    metadata: dict[str, str | None] = {
        "url_quality": "missing",
        "item_id": None,
        "original_href": original_href or None,
        "normalized_listing_url": None,
        "provider_domain": provider_domain,
    }
    if not original_href:
        return metadata
    absolute = urljoin(f"https://www.{provider_domain}/", original_href)
    parsed = urlparse(absolute)
    host = parsed.netloc.lower().split(":", 1)[0]
    if parsed.scheme.lower() != "https" or not any(host == domain or host.endswith(f".{domain}") for domain in SUPPORTED_EBAY_DOMAINS):
        metadata["url_quality"] = "malformed_or_non_ebay"
        return metadata
    item_match = re.search(r"/itm/(?:[^/?#]+/)?([0-9]+)(?:[/?#]|$)", parsed.path, flags=re.IGNORECASE)
    if not item_match:
        metadata["url_quality"] = "generic_non_item"
        return metadata
    item_id = item_match.group(1)
    normalized_url = urlunparse(("https", f"www.{provider_domain}", f"/itm/{item_id}", "", "", ""))
    metadata.update(
        {
            "url_quality": "direct_item",
            "item_id": item_id,
            "normalized_listing_url": normalized_url,
        }
    )
    return metadata


def contains_block_marker(*, title: str = "", body_text: str = "") -> bool:
    haystack = f"{title}\n{body_text}".lower()
    return any(marker in haystack for marker in BLOCK_TEXT_MARKERS)


def is_ebay_authentication_url(url: str) -> bool:
    """Return true when an eBay navigation has left public browsing for authentication."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if not (host == "ebay.com" or host.endswith(".ebay.com") or ".ebay." in host):
        return False
    if host.startswith(("signin.", "login.")):
        return True
    path = parsed.path.lower()
    return any(marker in path for marker in EBAY_AUTH_PATH_MARKERS)


def classify_browser_page_state(
    *,
    title: str = "",
    body_text: str = "",
    selector_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    haystack = f"{title}\n{body_text}".lower()
    selectors = selector_counts or {}
    result_count = sum(int(value or 0) for value in selectors.values())
    matched = lambda markers: next((marker for marker in markers if marker in haystack), None)
    if marker := matched(CHALLENGE_TEXT_MARKERS):
        return {"outcome": "challenge_detected", "reason": marker, "retryable": True}
    if marker := matched(ACCESS_BLOCK_TEXT_MARKERS):
        return {"outcome": "access_blocked", "reason": marker, "retryable": True}
    if marker := matched(AUTH_TEXT_MARKERS):
        return {"outcome": "authentication_required", "reason": marker, "retryable": True}
    if marker := matched(MAINTENANCE_TEXT_MARKERS):
        return {"outcome": "provider_unavailable", "reason": marker, "retryable": True}
    if result_count <= 0 and (marker := matched(CONSENT_TEXT_MARKERS)):
        return {"outcome": "provider_unavailable", "reason": f"interstitial:{marker}", "retryable": True}
    if marker := matched(NO_RESULTS_TEXT_MARKERS):
        return {"outcome": "no_results", "reason": marker, "retryable": False}
    if result_count > 0 or matched(RESULT_TEXT_MARKERS):
        return {"outcome": "success", "reason": "results_page", "retryable": False}
    return {"outcome": "parsing_failure", "reason": "unknown_page_state", "retryable": True}


def _looks_like_non_price_number(text: str, *, start: int, end: int) -> bool:
    after = text[end : min(len(text), end + 24)]
    if after.lstrip().startswith("%"):
        return True
    window = text[max(0, start - 36) : min(len(text), end + 48)]
    return bool(NON_PRICE_CONTEXT_RE.search(window)) and not bool(PRICE_CONTEXT_RE.search(window))


def _iter_price_matches(clean: str, *, expected_currency: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for detected_currency, pattern in EXPLICIT_PRICE_PATTERNS:
        for match in re.finditer(pattern, clean, flags=re.IGNORECASE):
            rejected = _looks_like_non_price_number(clean, start=match.start(1), end=match.end(1))
            matches.append(
                {
                    "currency": detected_currency,
                    "amountText": match.group(1),
                    "start": match.start(),
                    "end": match.end(),
                    "rejected": rejected,
                    "reason": "non_price_context" if rejected else None,
                }
            )
    if expected_currency.upper() == "USD":
        for match in BARE_DOLLAR_RE.finditer(clean):
            prefix = clean[max(0, match.start() - 4) : match.start()].upper().replace(" ", "")
            if prefix.endswith(("AU", "A", "US", "C", "CA")):
                continue
            rejected = _looks_like_non_price_number(clean, start=match.start(1), end=match.end(1))
            matches.append(
                {
                    "currency": "USD",
                    "amountText": match.group(1),
                    "start": match.start(),
                    "end": match.end(),
                    "rejected": rejected,
                    "reason": "non_price_context" if rejected else None,
                }
            )
    return sorted(matches, key=lambda item: int(item["start"]))


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


def parse_price_text(text: str, *, expected_currency: str) -> tuple[float | None, str | None, dict[str, Any]]:  # type: ignore[no-redef]
    clean = _normalise_text(text)
    if not clean:
        return None, None, {"rawText": text, "reason": "empty"}
    currency = expected_currency.upper()
    matches = _iter_price_matches(clean, expected_currency=currency)
    rejected_percent = len(re.findall(r"[0-9][0-9,]*(?:\.[0-9]{1,2})?\s*%", clean))
    rejected_feedback = sum(1 for item in matches if item.get("rejected"))
    valid = [item for item in matches if not item.get("rejected")]
    if not valid:
        return (
            None,
            None,
            {
                "rawText": text,
                "reason": "no_currency_price",
                "rejectedNonPricePercent": rejected_percent,
                "rejectedFeedbackNumber": rejected_feedback,
            },
        )
    preferred = next((item for item in valid if item["currency"] == currency), valid[0])
    amount = float(str(preferred["amountText"]).replace(",", ""))
    detected_currency = str(preferred["currency"])
    diagnostics = {
        "rawText": text,
        "detectedCurrency": detected_currency,
        "matchedText": clean[int(preferred["start"]) : int(preferred["end"])],
        "rejectedNonPricePercent": rejected_percent,
        "rejectedFeedbackNumber": rejected_feedback,
    }
    if detected_currency != currency:
        diagnostics["currencyMismatch"] = True
    return amount, detected_currency, diagnostics


def is_price_range_text(text: str) -> bool:
    clean = _normalise_text(text)
    if not clean:
        return False
    amounts = re.findall(r"[0-9][0-9,]*(?:\.[0-9]{1,2})?", clean)
    return len(amounts) >= 2 and bool(re.search(r"\bto\b|-", clean, flags=re.IGNORECASE))


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
    date_match = re.search(
        r"(?:sold(?:\s+date)?[:\s]+)?([0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4}|[A-Za-z]{3,9}\s+[0-9]{1,2},\s+[0-9]{4})",
        clean,
        flags=re.IGNORECASE,
    )
    if date_match:
        clean = date_match.group(1)
    clean = re.sub(r"^sold(?:\s+date)?[:\s]+", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s+sold$", "", clean, flags=re.IGNORECASE)
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(clean, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return DEFAULT_SOLD_DATE


def extract_sold_date_text(text: str) -> str:
    clean = _normalise_text(text)
    match = re.search(
        r"Sold(?:\s+date)?[:\s]+(?:[0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4}|[A-Za-z]{3,9}\s+[0-9]{1,2},\s+[0-9]{4})",
        clean,
        flags=re.IGNORECASE,
    )
    return match.group(0) if match else ""


def _looks_like_price_line(line: str, *, expected_currency: str) -> bool:
    amount, _currency, diagnostics = parse_price_text(line, expected_currency=expected_currency)
    if amount is None:
        return False
    lowered = line.lower()
    if lowered.lstrip().startswith(("+", "delivery", "shipping", "postage")):
        return False
    return diagnostics.get("reason") != "no_numeric_price"


def extract_price_text_from_lines(lines: list[str], *, expected_currency: str) -> str:
    for line in lines:
        if _looks_like_price_line(line, expected_currency=expected_currency):
            return line
    return ""


def extract_shipping_text_from_lines(lines: list[str], *, expected_currency: str) -> str:
    for line in lines:
        lowered = line.lower()
        if any(marker in lowered for marker in ("delivery", "shipping", "postage")):
            return line
    return ""


def _text_has_any(text: str, patterns: tuple[str, ...]) -> bool:
    padded = f" {text.lower()} "
    return any(pattern in padded for pattern in patterns)


def detect_international_origin(text: str, *, market_country: str) -> bool:
    lowered = _normalise_text(text).lower()
    from_match = re.search(r"\bfrom\s+([A-Za-z ]{2,40})(?:$|[.,|])", lowered)
    if not from_match:
        return False
    origin = from_match.group(1).strip()
    allowed = MARKET_COUNTRY_NAMES.get(market_country.upper(), ())
    return bool(origin and not any(name.strip() in origin for name in allowed))


def extract_location_text(text: str) -> str:
    clean = _normalise_text(text)
    match = re.search(r"\bfrom\s+[A-Za-z ]{2,40}(?:$|[.,|])", clean, flags=re.IGNORECASE)
    return match.group(0).rstrip(".,| ") if match else ""


def clean_candidate_title(value: str) -> str:
    title = _normalise_text(value)
    lowered = title.lower()
    if not title:
        return ""
    if any(marker in lowered for marker in PROMO_TITLE_MARKERS):
        return ""
    if any(marker in lowered for marker in GENERIC_TITLE_MARKERS):
        return ""
    return title


def extract_title_from_lines(lines: list[str], *, href_text: str = "", expected_currency: str = "AUD") -> str:
    anchor_title = clean_candidate_title(href_text)
    if anchor_title:
        return anchor_title
    for line in lines:
        lowered = line.lower()
        candidate_line = line
        if lowered.startswith("sold "):
            candidate_line = re.sub(
                r"^sold\s+(?:[0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4}|[A-Za-z]{3,9}\s+[0-9]{1,2},\s+[0-9]{4})\s*",
                "",
                candidate_line,
                flags=re.IGNORECASE,
            )
        elif "delivery" in lowered or "shipping" in lowered or "postage" in lowered:
            continue
        if _looks_like_price_line(candidate_line, expected_currency=expected_currency):
            price_matches = _iter_price_matches(candidate_line, expected_currency=expected_currency)
            first_price = next((item for item in price_matches if not item.get("rejected")), None)
            if first_price:
                candidate_line = candidate_line[: int(first_price["start"])]
        if not candidate_line.strip():
            continue
        boundary = TITLE_UI_BOUNDARY_RE.search(candidate_line)
        if boundary:
            candidate_line = candidate_line[: boundary.start()]
        candidate_line = candidate_line.strip()
        if not candidate_line:
            continue
        title = clean_candidate_title(candidate_line)
        if title:
            return title
    return ""


def parse_candidate_dict(
    candidate: dict[str, Any],
    *,
    request: ProviderRequest,
    search_query: ProviderSearchQuery,
    index: int,
) -> SoldComp | None:
    href = str(candidate.get("href") or "")
    url_metadata = normalize_ebay_listing_url(href, provider_domain=search_query.provider_domain)
    listing_url = str(url_metadata.get("normalized_listing_url") or "")
    if url_metadata["url_quality"] != "direct_item" or not listing_url:
        return None
    raw_text = str(candidate.get("text") or "")
    lines = [_normalise_text(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    title = extract_title_from_lines(
        lines,
        href_text=str(candidate.get("title") or candidate.get("anchorText") or ""),
        expected_currency=search_query.currency,
    )
    if not title:
        return None
    structured_price_text = _normalise_text(candidate.get("priceText") or "")
    fallback_price_text = ""
    price_source = "structured"
    if structured_price_text:
        price_text = structured_price_text
    else:
        fallback_price_text = extract_price_text_from_lines(lines, expected_currency=search_query.currency)
        price_text = fallback_price_text
        price_source = "fallback"
    price_range_listing = is_price_range_text(price_text)
    sold_price, detected_currency, price_diagnostics = parse_price_text(
        price_text,
        expected_currency=search_query.currency,
    )
    if sold_price is None:
        return None
    shipping_text = _normalise_text(candidate.get("shippingText") or "") or extract_shipping_text_from_lines(
        lines,
        expected_currency=search_query.currency,
    )
    shipping_price, shipping_diagnostics = parse_shipping_text(
        shipping_text,
        expected_currency=search_query.currency,
    )
    sold_date_text = _normalise_text(candidate.get("soldDateText") or "") or extract_sold_date_text(raw_text)
    condition_text = _normalise_text(candidate.get("conditionText") or "")
    item_location_text = _normalise_text(candidate.get("itemLocationText") or "") or extract_location_text(raw_text)
    appears_international = detect_international_origin(
        " ".join([raw_text, item_location_text]),
        market_country=search_query.market_country,
    )
    title_flags_text = f" {title} {raw_text} "
    source_listing_id = source_listing_id_from_url(listing_url, index=index)
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
                **url_metadata,
                "providerMarketplaceId": search_query.provider_marketplace_id,
                "query_index": search_query.query_index,
                "query_source": search_query.query_source,
                "query_style": search_query.diagnostics.get("queryStyle") or "unquoted_discovery",
                "queryStyle": search_query.diagnostics.get("queryStyle") or "unquoted_discovery",
                "query_text": search_query.query_text,
                "query_search_url": search_query.search_url,
                "marketCountry": request.market_country,
                "expectedCurrency": search_query.currency,
                "detectedCurrency": detected_currency,
                "priceText": price_text,
                "shippingText": shipping_text,
                "priceRangeListing": price_range_listing,
                "priceSource": price_source,
                "fallbackPriceUsed": price_source == "fallback",
                "structuredPriceUsed": price_source == "structured",
                "marketScope": "marketplace",
                "item_location_text": item_location_text,
                "seller_location_text": _normalise_text(candidate.get("sellerLocationText") or ""),
                "shipping_origin_text": _normalise_text(candidate.get("shippingOriginText") or ""),
                "appears_international_for_market": appears_international,
                "likely_pick_your_card": _text_has_any(title_flags_text, PICK_YOUR_CARD_PATTERNS),
                "likely_bundle_lot": _text_has_any(title_flags_text, LOT_BUNDLE_PATTERNS),
                "likely_graded": _text_has_any(title_flags_text, GRADED_PATTERNS),
                "likely_sealed": _text_has_any(title_flags_text, SEALED_PATTERNS),
                "priceDiagnostics": price_diagnostics,
                "shippingDiagnostics": shipping_diagnostics,
                "soldDateText": sold_date_text,
                "candidateSource": candidate.get("source"),
                "rawTextSnippet": _normalise_text(raw_text)[:500],
                "identityTitle": title,
            }
        ),
    )


def source_listing_id_from_url(listing_url: str, *, index: int) -> str:
    match = re.search(r"/itm/(?:[^/]+/)?([0-9]+)", listing_url)
    if match:
        return f"ebay-{match.group(1)}"
    digest = hashlib.sha256(f"{listing_url}|{index}".encode("utf-8")).hexdigest()[:16]
    return f"ebay-{digest}"


@dataclass(frozen=True)
class EbayBrowserProviderConfig:
    engine: str
    channel: str
    profile_name: str
    headless: bool
    max_results: int
    timeout_seconds: int
    launch_timeout_seconds: int
    cooldown_seconds: int
    min_seconds_between_requests: int
    user_data_dir: Path
    debug_artifact_dir: Path | None
    market_scope: str
    enabled: bool = False
    kill_switch: bool = False
    max_concurrency: int = 1
    challenge_stop: bool = True
    cache_first: bool = True
    max_requests_per_hour: int = 20
    max_requests_per_day: int = 100
    provider_error_cache_hours: int = 1
    challenge_cache_hours: int = 12

    @classmethod
    def from_env(cls) -> "EbayBrowserProviderConfig":
        profile_name = os.getenv("EBAY_BROWSER_PROFILE_NAME", DEFAULT_EBAY_BROWSER_PROFILE_NAME).strip()
        if not profile_name:
            profile_name = DEFAULT_EBAY_BROWSER_PROFILE_NAME
        raw_user_data_dir = os.getenv("EBAY_BROWSER_USER_DATA_DIR", "").strip()
        if raw_user_data_dir:
            user_data_dir = Path(raw_user_data_dir)
            if not user_data_dir.is_absolute():
                user_data_dir = ROOT / user_data_dir
        else:
            user_data_dir = DEFAULT_EBAY_BROWSER_USER_DATA_DIR
        config = cls(
            engine=os.getenv("EBAY_BROWSER_ENGINE", "chrome").strip().lower() or "chrome",
            channel=os.getenv("EBAY_BROWSER_CHANNEL", "chrome").strip().lower() or "chrome",
            profile_name=profile_name,
            headless=_parse_bool("EBAY_BROWSER_HEADLESS", True),
            max_results=min(_parse_positive_int("EBAY_BROWSER_MAX_RESULTS", 30), 100),
            timeout_seconds=_parse_positive_int("EBAY_BROWSER_TIMEOUT_SECONDS", 45),
            launch_timeout_seconds=_parse_positive_int("EBAY_BROWSER_LAUNCH_TIMEOUT_SECONDS", 45),
            cooldown_seconds=_parse_positive_int("EBAY_BROWSER_COOLDOWN_SECONDS", 20),
            min_seconds_between_requests=_parse_positive_int(
                "EBAY_BROWSER_MIN_DELAY_SECONDS",
                _parse_positive_int("EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS", 20),
            ),
            user_data_dir=user_data_dir,
            debug_artifact_dir=Path(os.getenv("EBAY_BROWSER_DEBUG_ARTIFACT_DIR", "").strip())
            if os.getenv("EBAY_BROWSER_DEBUG_ARTIFACT_DIR", "").strip()
            else None,
            market_scope=os.getenv("EBAY_MARKET_SCOPE", "marketplace").strip().lower() or "marketplace",
            enabled=_parse_bool("EBAY_BROWSER_ENABLED", _parse_bool("ENABLE_EBAY_REAL_LOOKUP", False)),
            kill_switch=_parse_bool("EBAY_BROWSER_KILL_SWITCH", False),
            max_concurrency=_parse_positive_int("EBAY_BROWSER_MAX_CONCURRENCY", 1),
            challenge_stop=_parse_bool("EBAY_BROWSER_CHALLENGE_STOP", True),
            cache_first=_parse_bool("EBAY_BROWSER_CACHE_FIRST", True),
            max_requests_per_hour=_parse_positive_int("EBAY_BROWSER_MAX_REQUESTS_PER_HOUR", 20),
            max_requests_per_day=_parse_positive_int("EBAY_BROWSER_MAX_REQUESTS_PER_DAY", 100),
            provider_error_cache_hours=_parse_positive_int("MARKET_CACHE_PROVIDER_ERROR_HOURS", 1),
            challenge_cache_hours=_parse_positive_int("MARKET_CACHE_PROVIDER_CHALLENGE_HOURS", 12),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.kill_switch:
            raise ProviderDisabledError("EBAY_BROWSER_KILL_SWITCH=true disables the eBay browser provider")
        if self.max_concurrency != 1:
            raise ProviderDisabledError("EBAY_BROWSER_MAX_CONCURRENCY must be 1 for the MVP browser provider.")
        if self.engine != "chrome":
            raise ProviderDisabledError(
                "EBAY_BROWSER_ENGINE must be 'chrome'. Bundled Chromium fallback is intentionally disabled."
            )
        if self.channel != "chrome":
            raise ProviderDisabledError("EBAY_BROWSER_CHANNEL must be 'chrome' for installed Google Chrome.")
        if self.profile_name != DEFAULT_EBAY_BROWSER_PROFILE_NAME:
            raise ProviderDisabledError("EBAY_BROWSER_PROFILE_NAME must be 'cardscanr' for this local provider.")
        if appears_to_be_personal_chrome_profile(self.user_data_dir):
            raise ProviderDisabledError(
                "EBAY_BROWSER_USER_DATA_DIR appears to point at a personal Chrome profile. "
                "Use the dedicated repo profile under .browser_profiles/cardscanr."
            )
        if self.market_scope != "marketplace":
            raise ProviderDisabledError("EBAY_MARKET_SCOPE currently supports only 'marketplace'.")

    def ensure_profile_dir(self) -> Path:
        self.validate()
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        return self.user_data_dir

    def safe_diagnostics(self) -> dict[str, Any]:
        return sanitize_provider_diagnostics(
            {
                "engine": self.engine,
                "channel": self.channel,
                "profileName": self.profile_name,
                "userDataDir": "<dedicated-cardscanr-profile>",
                "headless": self.headless,
                "maxResults": self.max_results,
                "timeoutSeconds": self.timeout_seconds,
                "launchTimeoutSeconds": self.launch_timeout_seconds,
                "cooldownSeconds": self.cooldown_seconds,
                "minSecondsBetweenRequests": self.min_seconds_between_requests,
                "debugArtifactDir": str(self.debug_artifact_dir) if self.debug_artifact_dir else None,
                "marketScope": self.market_scope,
                "enabled": self.enabled,
                "killSwitch": self.kill_switch,
                "maxConcurrency": self.max_concurrency,
                "challengeStop": self.challenge_stop,
                "cacheFirst": self.cache_first,
                "maxRequestsPerHour": self.max_requests_per_hour,
                "maxRequestsPerDay": self.max_requests_per_day,
                "providerErrorCacheHours": self.provider_error_cache_hours,
                "challengeCacheHours": self.challenge_cache_hours,
            }
        )


class StageTimings:
    def __init__(self) -> None:
        self._started = time.monotonic()
        self.fields: dict[str, Any] = {
            "startedAtMonotonic": round(self._started, 6),
            "stageDurationsMs": {},
            "stageSequence": [],
            "currentStage": None,
            "timedOutStage": None,
        }

    def record(self, stage: str, started: float, *, status: str = "completed", extra: dict[str, Any] | None = None) -> None:
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        self.fields["stageDurationsMs"][stage] = elapsed_ms
        item: dict[str, Any] = {"stage": stage, "durationMs": elapsed_ms, "status": status}
        if extra:
            item.update(extra)
        self.fields["stageSequence"].append(item)
        self.fields["currentStage"] = stage
        if status == "timeout":
            self.fields["timedOutStage"] = stage

    def snapshot(self) -> dict[str, Any]:
        payload = dict(self.fields)
        payload["elapsedMs"] = round((time.monotonic() - self._started) * 1000, 2)
        return payload


class _StageTimer:
    def __init__(self, timings: StageTimings, stage: str) -> None:
        self.timings = timings
        self.stage = stage
        self.started = 0.0

    def __enter__(self) -> "_StageTimer":
        self.started = time.monotonic()
        self.timings.fields["currentStage"] = self.stage
        return self

    def __exit__(self, exc_type: Any, exc: Any, _tb: Any) -> bool:
        status = "timeout" if _looks_like_timeout(exc) else "failed" if exc is not None else "completed"
        extra = {"errorType": type(exc).__name__} if exc is not None else None
        self.timings.record(self.stage, self.started, status=status, extra=extra)
        return False


def _looks_like_timeout(exc: Any) -> bool:
    if exc is None:
        return False
    name = type(exc).__name__.lower()
    return "timeout" in name


def _safe_to_try_next_query(search_query: ProviderSearchQuery) -> bool:
    query_text = str(search_query.query_text or "")
    return all(ord(ch) <= 127 for ch in query_text)


def appears_to_be_personal_chrome_profile(path: Path | str) -> bool:
    text = str(path).replace("/", "\\").lower().rstrip("\\")
    return (
        "\\appdata\\local\\google\\chrome\\user data" in text
        or text.endswith("\\google\\chrome\\user data")
        or text.endswith("\\chrome\\user data\\default")
        or "\\google\\chrome\\user data\\default" in text
    )


def count_candidate_selectors(page: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for selector in RESULT_SELECTOR_COUNTS:
        try:
            counts[selector] = int(page.locator(selector).count())
        except Exception:
            counts[selector] = -1
    return counts


def _safe_page_title(page: Any) -> str:
    try:
        return str(page.title())
    except Exception:
        return ""


def _safe_body_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=5000))
    except Exception:
        return ""


def collect_candidate_dicts(page: Any, *, max_results: int) -> list[dict[str, Any]]:
    script = """
    ({ maxResults }) => {
      const norm = (value) => (value || '').replace(/\\s+/g, ' ').trim();
      const blockText = (value) => (value || '').replace(/\\r/g, '').trim();
      const textOf = (root, selectors) => {
        for (const selector of selectors) {
          const node = root.querySelector(selector);
          const text = norm(node && node.innerText);
          if (text) return text;
        }
        return '';
      };
      const hrefOf = (root) => {
        const link = root.matches && root.matches('a[href*="/itm/"]') ? root : root.querySelector('a[href*="/itm/"]');
        return link ? { href: link.href || '', anchorText: norm(link.innerText || link.getAttribute('aria-label')) } : { href: '', anchorText: '' };
      };
      const usefulParent = (anchor) => {
        const selectors = ['li.s-item', '.s-item', '.srp-results li', '[data-view]'];
        for (const selector of selectors) {
          const node = anchor.closest(selector);
          if (node) return node;
        }
        return anchor.parentElement || anchor;
      };
      const seen = new Set();
      const out = [];
      const add = (node, source) => {
        if (!node || out.length >= maxResults) return;
        const link = hrefOf(node);
        if (!link.href || !link.href.includes('/itm/')) return;
        const key = link.href.split('?')[0];
        if (seen.has(key)) return;
        seen.add(key);
        const text = blockText(node.innerText);
        out.push({
          source,
          href: link.href,
          anchorText: link.anchorText,
          title: textOf(node, ['.s-item__title span', '.s-item__title', 'a.s-item__link']),
          priceText: textOf(node, ['.s-item__price', '.s-item__detail--primary']),
          shippingText: textOf(node, ['.s-item__shipping', '.s-item__logisticsCost']),
          soldDateText: textOf(node, ['.s-item__title--tagblock .POSITIVE', '.s-item__caption--row']),
          conditionText: textOf(node, ['.SECONDARY_INFO', '.s-item__subtitle']),
          itemLocationText: textOf(node, ['.s-item__location', '.s-item__itemLocation', '.s-item__seller-info-text']),
          text
        });
      };
      for (const selector of ['li.s-item', '.s-item', '.srp-results li']) {
        document.querySelectorAll(selector).forEach((node) => add(node, selector));
      }
      document.querySelectorAll('a[href*="/itm/"]').forEach((anchor) => add(usefulParent(anchor), 'a[href*="/itm/"]'));
      return out.slice(0, maxResults);
    }
    """
    try:
        result = page.evaluate(script, {"maxResults": max_results})
    except Exception:
        return []
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def build_quality_summary(comps: list[SoldComp], *, request: ProviderRequest) -> dict[str, int]:
    identity_guard = evaluate_english_market_identity(request)
    requested_name = identity_guard.search_card_name.lower() or request.price_key.normalized_card_name.replace("_", " ").lower() or request.price_key.card_name.lower()
    collector_number = request.price_key.collector_number.lower()
    summary = {
        "total_parsed": len(comps),
        "exact_title_or_number_matches": 0,
        "range_price_count": 0,
        "missing_price_count": 0,
        "international_origin_count": 0,
        "likely_pick_your_card_count": 0,
        "likely_bundle_lot_count": 0,
        "likely_graded_count": 0,
        "likely_sealed_count": 0,
        "rejected_non_price_percent_count": 0,
        "rejected_feedback_number_count": 0,
        "currency_mismatch_count": 0,
        "fallback_price_used_count": 0,
        "structured_price_used_count": 0,
        "useful_candidate_count": 0,
        "direct_item_url_count": 0,
        "generic_url_count": 0,
        "missing_url_count": 0,
    }
    for comp in comps:
        title = comp.title.lower()
        raw = comp.raw_metadata
        url_quality = str(raw.get("url_quality") or "missing")
        if url_quality == "direct_item":
            summary["direct_item_url_count"] += 1
        elif url_quality == "generic_non_item":
            summary["generic_url_count"] += 1
        else:
            summary["missing_url_count"] += 1
        exactish = requested_name in title or collector_number in title
        if exactish:
            summary["exact_title_or_number_matches"] += 1
        if raw.get("priceRangeListing"):
            summary["range_price_count"] += 1
        if comp.sold_price <= 0:
            summary["missing_price_count"] += 1
        if raw.get("appears_international_for_market"):
            summary["international_origin_count"] += 1
        if raw.get("likely_pick_your_card"):
            summary["likely_pick_your_card_count"] += 1
        if raw.get("likely_bundle_lot"):
            summary["likely_bundle_lot_count"] += 1
        if raw.get("likely_graded"):
            summary["likely_graded_count"] += 1
        if raw.get("likely_sealed"):
            summary["likely_sealed_count"] += 1
        price_diagnostics = raw.get("priceDiagnostics") if isinstance(raw.get("priceDiagnostics"), dict) else {}
        if price_diagnostics.get("rejectedNonPricePercent"):
            summary["rejected_non_price_percent_count"] += int(price_diagnostics.get("rejectedNonPricePercent") or 0)
        if price_diagnostics.get("rejectedFeedbackNumber"):
            summary["rejected_feedback_number_count"] += int(price_diagnostics.get("rejectedFeedbackNumber") or 0)
        if raw.get("detectedCurrency") and str(raw.get("detectedCurrency")).upper() != request.currency.upper():
            summary["currency_mismatch_count"] += 1
        if raw.get("fallbackPriceUsed"):
            summary["fallback_price_used_count"] += 1
        if raw.get("structuredPriceUsed"):
            summary["structured_price_used_count"] += 1
        if (
            exactish
            and not raw.get("priceRangeListing")
            and not raw.get("likely_pick_your_card")
            and not raw.get("likely_bundle_lot")
            and not raw.get("likely_sealed")
            and not (raw.get("detectedCurrency") and str(raw.get("detectedCurrency")).upper() != request.currency.upper())
            and comp.sold_price > 0
        ):
            summary["useful_candidate_count"] += 1
    return summary


def _max_query_attempts() -> int:
    return min(_parse_positive_int("EBAY_BROWSER_MAX_QUERY_ATTEMPTS", DEFAULT_MAX_QUERY_ATTEMPTS), DEFAULT_MAX_QUERY_ATTEMPTS)


def _tag_comp_with_query(comp: SoldComp, search_query: ProviderSearchQuery) -> SoldComp:
    metadata = dict(comp.raw_metadata)
    metadata["query_index"] = search_query.query_index
    metadata["query_source"] = search_query.query_source
    metadata["query_style"] = search_query.diagnostics.get("queryStyle") or "unquoted_discovery"
    metadata["query_text"] = search_query.query_text
    metadata["query_search_url"] = search_query.search_url
    metadata["query_sources"] = [search_query.query_source]
    metadata["query_indexes"] = [search_query.query_index]
    return SoldComp(
        source_listing_id=comp.source_listing_id,
        title=comp.title,
        sold_price=comp.sold_price,
        shipping_price=comp.shipping_price,
        total_price=comp.total_price,
        currency=comp.currency,
        sold_date=comp.sold_date,
        listing_url=comp.listing_url,
        condition_text=comp.condition_text,
        raw_metadata=metadata,
    )


def _dedupe_key(comp: SoldComp) -> str:
    raw = comp.raw_metadata
    item_id = str(raw.get("item_id") or "").strip()
    if item_id:
        return f"item:{item_id}"
    canonical_url = str(raw.get("normalized_listing_url") or comp.listing_url or "").strip().lower()
    if canonical_url:
        return f"url:{canonical_url}"
    normalized_title = _normalise_text(comp.title).lower()
    sold_date = comp.sold_date.astimezone(timezone.utc).date().isoformat() if comp.sold_date is not None else "unknown-date"
    return f"title-price-date:{normalized_title}|{comp.sold_price:.2f}|{sold_date}"


def dedupe_sold_comps(comps: list[SoldComp]) -> list[SoldComp]:
    deduped: dict[str, SoldComp] = {}
    for comp in comps:
        key = _dedupe_key(comp)
        existing = deduped.get(key)
        if existing is None:
            metadata = dict(comp.raw_metadata)
            source = metadata.get("query_source")
            index = metadata.get("query_index")
            metadata.setdefault("dedupe_key", key)
            metadata.setdefault("query_sources", [source] if source is not None else [])
            metadata.setdefault("query_indexes", [index] if index is not None else [])
            deduped[key] = SoldComp(
                source_listing_id=comp.source_listing_id,
                title=comp.title,
                sold_price=comp.sold_price,
                shipping_price=comp.shipping_price,
                total_price=comp.total_price,
                currency=comp.currency,
                sold_date=comp.sold_date,
                listing_url=comp.listing_url,
                condition_text=comp.condition_text,
                raw_metadata=metadata,
            )
            continue
        metadata = dict(existing.raw_metadata)
        duplicate_sources = list(metadata.get("query_sources") or [])
        duplicate_indexes = list(metadata.get("query_indexes") or [])
        source = comp.raw_metadata.get("query_source")
        index = comp.raw_metadata.get("query_index")
        if source is not None and source not in duplicate_sources:
            duplicate_sources.append(source)
        if index is not None and index not in duplicate_indexes:
            duplicate_indexes.append(index)
        metadata["query_sources"] = duplicate_sources
        metadata["query_indexes"] = duplicate_indexes
        metadata["duplicate_seen_count"] = int(metadata.get("duplicate_seen_count") or 1) + 1
        metadata.setdefault("duplicate_listing_urls", [])
        duplicate_urls = list(metadata.get("duplicate_listing_urls") or [])
        if comp.listing_url and comp.listing_url not in duplicate_urls:
            duplicate_urls.append(comp.listing_url)
        metadata["duplicate_listing_urls"] = duplicate_urls[:10]
        deduped[key] = SoldComp(
            source_listing_id=existing.source_listing_id,
            title=existing.title,
            sold_price=existing.sold_price,
            shipping_price=existing.shipping_price,
            total_price=existing.total_price,
            currency=existing.currency,
            sold_date=existing.sold_date,
            listing_url=existing.listing_url,
            condition_text=existing.condition_text,
            raw_metadata=metadata,
        )
    return list(deduped.values())


def _merge_quality_summaries(summaries: list[dict[str, int]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for summary in summaries:
        for key, value in summary.items():
            merged[key] = merged.get(key, 0) + int(value or 0)
    return merged


def _comp_flags(item: Any) -> dict[str, Any]:
    raw = item.comp.raw_metadata
    return {
        "card_name_match": raw.get("card_name_match"),
        "collector_number_match": raw.get("collector_number_match"),
        "collector_number_match_quality": raw.get("collector_number_match_quality"),
        "set_name_match": raw.get("set_name_match"),
        "set_match_quality": raw.get("set_match_quality"),
        "requested_variant": raw.get("requested_variant"),
        "detected_variant": raw.get("detected_variant"),
        "variant_match": raw.get("variant_match"),
        "url_quality": raw.get("url_quality"),
    }


def _compact_evaluated_comp(item: Any) -> dict[str, Any]:
    raw = item.comp.raw_metadata
    return {
        "query_index": raw.get("query_index"),
        "query_source": raw.get("query_source"),
        "query_sources": raw.get("query_sources") or [],
        "title": item.comp.title,
        "sold_price": item.comp.sold_price,
        "shipping_price": item.comp.shipping_price,
        "total_price": item.comp.total_price,
        "currency": item.comp.currency,
        "sold_date": utc_iso(item.comp.sold_date) if item.comp.sold_date is not None else None,
        "listing_url": item.comp.listing_url or None,
        "item_id": raw.get("item_id"),
        "score": item.match_score,
        "flags": _comp_flags(item),
        "rejection_reason": item.rejection_reason,
    }


def _attempt_query_index(item: Any) -> int | None:
    raw = item.comp.raw_metadata
    try:
        return int(raw.get("query_index"))
    except Exception:
        return None


def build_query_attempt_summaries(
    attempts: list[tuple[ProviderSearchQuery, ProviderResult]],
    evaluated: list[Any],
    progress_summaries: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    progress_by_index = {item.get("query_index"): item for item in progress_summaries or []}
    for search_query, result in attempts:
        attempt_evaluated = [item for item in evaluated if _attempt_query_index(item) == search_query.query_index]
        progress = progress_by_index.get(search_query.query_index, {})
        summaries.append(
            {
                "query_index": search_query.query_index,
                "query_source": search_query.query_source,
                "query_style": search_query.diagnostics.get("queryStyle") or "unquoted_discovery",
                "query_text": search_query.query_text,
                "search_url": search_query.search_url,
                "result_count": len(result.comps),
                "included_count": sum(1 for item in attempt_evaluated if item.included_in_estimate),
                "rejected_count": sum(1 for item in attempt_evaluated if not item.included_in_estimate),
                "cumulative_included_after_attempt": progress.get("cumulativeIncludedAfterAttempt"),
                "cumulative_rejected_after_attempt": progress.get("cumulativeRejectedAfterAttempt"),
                "new_unique_candidates": progress.get("newUniqueCandidatesPerAttempt"),
                "duplicate_candidates": progress.get("duplicateCandidatesPerAttempt"),
                "clean_included_count": progress.get("cleanIncludedCount"),
                "clean_recent_comp_count": progress.get("cleanRecentCompCount"),
                "clean_stale_comp_count": progress.get("cleanStaleCompCount"),
                "selector_rejected_count": progress.get("selectorRejectedCount"),
                "wrong_language_rejected_count": progress.get("wrongLanguageRejectedCount"),
                "wrong_collector_number_rejected_count": progress.get("wrongCollectorNumberRejectedCount"),
                "wrong_card_name_rejected_count": progress.get("wrongCardNameRejectedCount"),
                "wrong_variant_rejected_count": progress.get("wrongVariantRejectedCount"),
                "dominant_rejection_reason": progress.get("attemptDominantRejectionReason"),
                "useful_exact_candidate_count": progress.get("usefulExactCandidateCount"),
                "noisy_result_ratio": progress.get("attemptNoisyResultRatio"),
                "should_continue_reason": progress.get("shouldContinueReason"),
                "quality_summary": result.raw_metadata.get("qualitySummary") or {},
                "parser_error_count": len(result.raw_metadata.get("parserErrors") or []),
            }
        )
    return summaries


def _is_clean_included(item: Any) -> bool:
    raw = item.comp.raw_metadata
    return (
        bool(item.included_in_estimate)
        and bool(raw.get("card_name_match"))
        and bool(raw.get("collector_number_match"))
        and bool(raw.get("variant_match"))
        and not raw.get("language_rejection")
        and (raw.get("url_quality") == "direct_item" or "/itm/" in item.comp.listing_url)
    )


NOISY_IDENTITY_REJECTION_REASONS = {
    "wrong_collector_number",
    "wrong_card_name",
    "wrong_variant",
    "wrong_variant_holo",
    "wrong_variant_reverse_holo",
    "weak_variant_match",
    "wrong_language",
}


def _is_exact_identity_candidate(item: Any) -> bool:
    raw = item.comp.raw_metadata
    return (
        bool(raw.get("card_name_match"))
        and bool(raw.get("collector_number_match"))
        and bool(raw.get("variant_match"))
        and not raw.get("language_rejection")
        and (raw.get("url_quality") == "direct_item" or "/itm/" in item.comp.listing_url)
    )


def _dominant_rejection_reason(items: list[Any]) -> str | None:
    counts = Counter(str(item.rejection_reason) for item in items if item.rejection_reason)
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _rejection_count(items: list[Any], reasons: set[str]) -> int:
    return sum(1 for item in items if item.rejection_reason in reasons)


def _clean_comp_recency_fields(items: list[Any], *, now: datetime | None = None) -> dict[str, Any]:
    from ..pricing_stats import sold_listing_recency_threshold_days

    threshold_days = sold_listing_recency_threshold_days()
    current_time = now or datetime.now(timezone.utc)
    cutoff = current_time - timedelta(days=threshold_days)
    dates = [item.comp.sold_date for item in items if item.comp.sold_date is not None]
    recent = [item for item in items if item.comp.sold_date is not None and item.comp.sold_date >= cutoff]
    stale = [item for item in items if item.comp.sold_date is None or item.comp.sold_date < cutoff]
    return {
        "cleanRecentCompCount": len(recent),
        "cleanStaleCompCount": len(stale),
        "oldestCleanCompDate": utc_iso(min(dates)) if dates else None,
        "newestCleanCompDate": utc_iso(max(dates)) if dates else None,
        "soldListingRecencyThresholdDays": threshold_days,
        "singleCleanCompOnly": len(items) == 1,
        "staleEvidenceOnly": bool(items) and len(recent) == 0,
    }


def build_attempt_progress_summaries(
    request: ProviderRequest,
    attempts: list[tuple[ProviderSearchQuery, ProviderResult]],
) -> list[dict[str, Any]]:
    from ..filters import filter_comps

    summaries: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    cumulative_raw: list[SoldComp] = []
    for search_query, result in attempts:
        attempt_keys = [_dedupe_key(comp) for comp in result.comps]
        new_keys = {key for key in attempt_keys if key not in seen_keys}
        duplicate_count = sum(1 for key in attempt_keys if key in seen_keys)
        seen_keys.update(attempt_keys)
        cumulative_raw.extend(result.comps)
        cumulative_comps = dedupe_sold_comps(cumulative_raw)
        evaluated = filter_comps(request.price_key, cumulative_comps)
        included = [item for item in evaluated if item.included_in_estimate]
        rejected = [item for item in evaluated if not item.included_in_estimate]
        attempt_evaluated = [item for item in evaluated if _attempt_query_index(item) == search_query.query_index]
        attempt_rejected = [item for item in attempt_evaluated if not item.included_in_estimate]
        attempt_included = [item for item in attempt_evaluated if item.included_in_estimate]
        clean_included = [item for item in included if _is_clean_included(item)]
        clean_recency = _clean_comp_recency_fields(clean_included)
        exact_identity_candidates = [item for item in evaluated if _is_exact_identity_candidate(item)]
        selector_rejected_count = sum(1 for item in rejected if item.rejection_reason == "price_range_or_variation_listing")
        wrong_collector_number_count = sum(1 for item in rejected if item.rejection_reason == "wrong_collector_number")
        wrong_card_name_count = sum(1 for item in rejected if item.rejection_reason == "wrong_card_name")
        wrong_variant_count = _rejection_count(
            rejected,
            {"wrong_variant", "wrong_variant_holo", "wrong_variant_reverse_holo", "weak_variant_match"},
        )
        wrong_language_rejected_count = sum(1 for item in rejected if item.rejection_reason == "wrong_language")
        noisy_rejected_count = _rejection_count(rejected, NOISY_IDENTITY_REJECTION_REASONS)
        noisy_result_ratio = round(noisy_rejected_count / len(rejected), 4) if rejected else 0.0
        attempt_noisy_count = _rejection_count(attempt_rejected, NOISY_IDENTITY_REJECTION_REASONS)
        attempt_noisy_ratio = round(attempt_noisy_count / len(attempt_rejected), 4) if attempt_rejected else 0.0
        new_clean_count = sum(1 for item in clean_included if _dedupe_key(item.comp) in new_keys)
        summaries.append(
            {
                "query_index": search_query.query_index,
                "query_source": search_query.query_source,
                "queryStyle": search_query.diagnostics.get("queryStyle") or "unquoted_discovery",
                "cumulativeIncludedAfterAttempt": len(included),
                "cumulativeRejectedAfterAttempt": len(rejected),
                "newUniqueCandidatesPerAttempt": len(new_keys),
                "duplicateCandidatesPerAttempt": duplicate_count,
                "newCleanIncludedPerAttempt": new_clean_count,
                "cleanIncludedCount": len(clean_included),
                "cleanExactCompCount": len(clean_included),
                **clean_recency,
                "exactIdentityResultCount": len(exact_identity_candidates),
                "usefulExactCandidateCount": len([item for item in attempt_evaluated if _is_exact_identity_candidate(item)]),
                "selectorRejectedCount": selector_rejected_count,
                "wrongCollectorNumberRejectedCount": wrong_collector_number_count,
                "wrongCardNameRejectedCount": wrong_card_name_count,
                "wrongVariantRejectedCount": wrong_variant_count,
                "wrongLanguageRejectedCount": wrong_language_rejected_count,
                "noisyResultRatio": noisy_result_ratio,
                "totalRejectedCount": len(rejected),
                "attemptIncludedCount": len(attempt_included),
                "attemptRejectedCount": len(attempt_rejected),
                "attemptDominantRejectionReason": _dominant_rejection_reason(attempt_rejected),
                "attemptNoisyResultRatio": attempt_noisy_ratio,
                "allRejectedReasons": sorted({str(item.rejection_reason) for item in rejected if item.rejection_reason}),
            }
        )
    return summaries


def _early_stop_decision(
    *,
    request: ProviderRequest,
    attempts: list[tuple[ProviderSearchQuery, ProviderResult]],
    search_queries: list[ProviderSearchQuery],
) -> dict[str, Any]:
    progress = build_attempt_progress_summaries(request, attempts)
    if not progress:
        return {"stop": False, "progress": progress}
    latest = progress[-1]
    clean_count = int(latest.get("cleanIncludedCount") or 0)
    selector_count = int(latest.get("selectorRejectedCount") or 0)
    rejected_count = int(latest.get("totalRejectedCount") or 0)
    new_unique = int(latest.get("newUniqueCandidatesPerAttempt") or 0)
    duplicate_count = int(latest.get("duplicateCandidatesPerAttempt") or 0)
    new_clean = int(latest.get("newCleanIncludedPerAttempt") or 0)
    noisy_ratio = float(latest.get("noisyResultRatio") or 0.0)
    dominant_rejection = str(latest.get("attemptDominantRejectionReason") or "")
    noisy_dominant = dominant_rejection in NOISY_IDENTITY_REJECTION_REASONS
    clean_recent_count = int(latest.get("cleanRecentCompCount") or 0)
    stale_evidence_only = bool(latest.get("staleEvidenceOnly"))
    attempted_count = len(attempts)
    next_query = search_queries[attempted_count] if attempted_count < len(search_queries) else None
    attempted_sources = {search_query.query_source for search_query, _result in attempts}
    set_code_attempted = any("set_code" in source for source in attempted_sources)
    quoted_attempted = any((search_query.diagnostics.get("queryStyle") or "") == "quoted_precision" for search_query, _result in attempts)

    def _next_index_matching(predicate: Any) -> int | None:
        for index in range(attempted_count, len(search_queries)):
            if predicate(search_queries[index]):
                return index
        return None

    if clean_count >= 3:
        latest["shouldContinueReason"] = "stop_enough_clean_comps"
        return {"stop": True, "reason": "enough_clean_comps", "progress": progress}
    single_clean_noisy_market = clean_count == 1 and noisy_ratio >= 0.7 and (noisy_dominant or set_code_attempted or quoted_attempted)
    if single_clean_noisy_market:
        if not set_code_attempted:
            next_index = _next_index_matching(lambda query: "set_code" in query.query_source)
            if next_index is not None:
                latest["shouldContinueReason"] = "skip_to_set_code_after_single_clean_noisy_broad_results"
                return {"stop": False, "nextQueryIndex": next_index, "progress": progress}
        if not quoted_attempted:
            next_index = _next_index_matching(lambda query: (query.diagnostics.get("queryStyle") or "") == "quoted_precision")
            if next_index is not None:
                latest["shouldContinueReason"] = "skip_to_quoted_precision_after_single_clean_noisy_results"
                return {"stop": False, "nextQueryIndex": next_index, "progress": progress}
        latest["shouldContinueReason"] = "stop_single_clean_comp_sparse_market"
        return {
            "stop": True,
            "reason": "stale_single_comp_only" if stale_evidence_only or clean_recent_count == 0 else "single_clean_comp_sparse_market",
            "lowConfidenceSparseMarketReason": "single_clean_comp_with_noisy_results",
            "progress": progress,
        }
    if clean_count == 2 and attempted_count >= 2 and (new_unique == 0 or new_clean == 0 or duplicate_count > 0):
        latest["shouldContinueReason"] = "stop_sparse_clean_market_evidence"
        return {
            "stop": True,
            "reason": "sparse_clean_market_evidence",
            "lowConfidenceSparseMarketReason": "two_clean_comps_after_duplicate_or_noisy_evidence",
            "progress": progress,
        }
    if set_code_attempted and clean_count == 0 and rejected_count > 0 and selector_count == rejected_count:
        latest["shouldContinueReason"] = "stop_only_selector_results"
        return {"stop": True, "reason": "only_selector_results", "progress": progress}
    if clean_count == 0 and rejected_count >= 3 and noisy_ratio >= 0.6:
        if not set_code_attempted:
            next_index = _next_index_matching(lambda query: "set_code" in query.query_source)
            if next_index is not None:
                latest["shouldContinueReason"] = "skip_to_set_code_after_noisy_broad_results"
                return {"stop": False, "nextQueryIndex": next_index, "progress": progress}
        if not quoted_attempted:
            next_index = _next_index_matching(lambda query: (query.diagnostics.get("queryStyle") or "") == "quoted_precision")
            if next_index is not None:
                latest["shouldContinueReason"] = "skip_to_quoted_precision_after_noisy_set_code_results"
                return {"stop": False, "nextQueryIndex": next_index, "progress": progress}
        latest["shouldContinueReason"] = "stop_noisy_results_no_exact_comps"
        return {"stop": True, "reason": "noisy_results_no_exact_comps", "progress": progress}
    if set_code_attempted and clean_count == 0 and int(latest.get("cumulativeRejectedAfterAttempt") or 0) == 0:
        latest["shouldContinueReason"] = "stop_no_useful_candidates"
        return {"stop": True, "reason": "no_useful_candidates", "progress": progress}
    if clean_count == 2 and attempted_count >= 3 and (new_unique == 0 or new_clean == 0 or duplicate_count > 0):
        latest["shouldContinueReason"] = "stop_low_confidence_sparse_market"
        return {
            "stop": True,
            "reason": "low_confidence_enough_for_sparse_market",
            "lowConfidenceSparseMarketReason": "two_clean_comps_after_no_new_useful_candidates",
            "progress": progress,
        }
    if (
        next_query is not None
        and (next_query.diagnostics.get("queryStyle") or "") == "quoted_precision"
        and clean_count > 0
        and selector_count == 0
        and new_unique == 0
    ):
        latest["shouldContinueReason"] = "skip_quoted_precision_after_clean_duplicates"
        return {
            "stop": True,
            "reason": "sparse_clean_market_evidence" if clean_count == 2 else "all_query_attempts_exhausted",
            "lowConfidenceSparseMarketReason": "quoted_fallback_skipped_after_clean_duplicate_unquoted_results" if clean_count == 2 else None,
            "progress": progress,
        }
    latest["shouldContinueReason"] = "continue_collecting_evidence"
    return {"stop": False, "progress": progress}


class EbayBrowserSoldCompsProvider:
    provider_name = "ebay_browser"
    marketplace_name = "ebay"

    _request_lock = threading.Lock()
    _lookup_lock = threading.Lock()
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
        with self._lookup_lock:
            return self._fetch_comps_serial(request)

    def _fetch_comps_serial(self, request: ProviderRequest) -> ProviderResult:
        lookup_timings = StageTimings()
        route = (request.market_country.upper(), request.currency.upper())
        if route not in SUPPORTED_MARKET_ROUTES:
            raise ProviderUnsupportedMarketError(
                "eBay browser provider currently supports AU/AUD, US/USD, GB/GBP, and CA/CAD only",
                diagnostics={"marketCountry": request.market_country, "currency": request.currency},
            )
        identity_guard = evaluate_english_market_identity(request)
        if identity_guard.blocked:
            raise ProviderIdentityUnavailableError(
                ENGLISH_MARKET_IDENTITY_UNAVAILABLE,
                diagnostics=identity_guard.diagnostics,
            )
        search_queries = build_provider_search_queries(request, max_attempts=_max_query_attempts())
        attempts: list[tuple[ProviderSearchQuery, ProviderResult]] = []
        failed_attempts: list[dict[str, Any]] = []
        aggregate_comps: list[SoldComp] = []
        early_stop_progress: list[dict[str, Any]] = []
        low_confidence_sparse_market_reason: str | None = None
        stop_reason = "all_query_attempts_exhausted"
        try:
            query_cursor = 0
            while query_cursor < len(search_queries):
                search_query = search_queries[query_cursor]
                attempt_stage = f"run_query_attempt_{search_query.query_index + 1}"
                try:
                    with _StageTimer(lookup_timings, attempt_stage):
                        self._wait_for_request_slot()
                        result = self._fetch_with_playwright(request=request, search_query=search_query)
                except ProviderTemporaryError as exc:
                    diagnostics = dict(getattr(exc, "diagnostics", {}) or {})
                    attempt_failure = sanitize_provider_diagnostics(
                        {
                            "query_index": search_query.query_index,
                            "query_source": search_query.query_source,
                            "query_text": search_query.query_text,
                            "search_url": search_query.search_url,
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                            "timed_out_stage": diagnostics.get("timedOutStage"),
                            "stage_timings": diagnostics.get("stageTimings") or diagnostics.get("stage_timings"),
                            "selector_counts": diagnostics.get("candidateSelectorCounts"),
                            "debug_artifacts": diagnostics.get("debugArtifacts"),
                        }
                    )
                    failed_attempts.append(attempt_failure)
                    if (
                        diagnostics.get("timedOutStage")
                        and _safe_to_try_next_query(search_query)
                        and search_query.query_index + 1 < len(search_queries)
                    ):
                        query_cursor += 1
                        continue
                    raise
                tagged_comps = [_tag_comp_with_query(comp, search_query) for comp in result.comps]
                result = ProviderResult(
                    provider_name=result.provider_name,
                    marketplace=result.marketplace,
                    provider_fingerprint=result.provider_fingerprint,
                    query_used=result.query_used,
                    comps=tagged_comps,
                    raw_metadata=result.raw_metadata,
                )
                attempts.append((search_query, result))
                aggregate_comps = dedupe_sold_comps([comp for _query, attempt in attempts for comp in attempt.comps])
                stop_decision = _early_stop_decision(
                    request=request,
                    attempts=attempts,
                    search_queries=search_queries,
                )
                early_stop_progress = list(stop_decision.get("progress") or [])
                if stop_decision.get("stop"):
                    stop_reason = str(stop_decision.get("reason") or "all_query_attempts_exhausted")
                    low_confidence_sparse_market_reason = stop_decision.get("lowConfidenceSparseMarketReason")  # type: ignore[assignment]
                    break
                next_query_index = stop_decision.get("nextQueryIndex")
                if isinstance(next_query_index, int) and next_query_index > query_cursor:
                    query_cursor = next_query_index
                else:
                    query_cursor += 1
            return self._build_aggregate_result(
                request=request,
                attempts=attempts,
                comps=aggregate_comps,
                stop_reason=stop_reason,
                query_attempt_limit=len(search_queries),
                failed_attempts=failed_attempts,
                stage_timings=lookup_timings.snapshot(),
                early_stop_progress=early_stop_progress,
                low_confidence_sparse_market_reason=low_confidence_sparse_market_reason,
            )
        except ProviderError:
            if failed_attempts and self.config.debug_artifact_dir is not None:
                self._write_timeout_debug_summary(
                    request=request,
                    failed_attempts=failed_attempts,
                    stage_timings=lookup_timings.snapshot(),
                    stop_reason="query_attempt_timeout",
                )
            raise
        except Exception as exc:
            raise ProviderTemporaryError(
                "eBay browser lookup failed temporarily",
                diagnostics={
                    "errorType": type(exc).__name__,
                    "providerDomain": request.provider_domain,
                    "stageTimings": lookup_timings.snapshot(),
                },
            ) from exc

    def _build_aggregate_result(
        self,
        *,
        request: ProviderRequest,
        attempts: list[tuple[ProviderSearchQuery, ProviderResult]],
        comps: list[SoldComp],
        stop_reason: str,
        query_attempt_limit: int,
        failed_attempts: list[dict[str, Any]] | None = None,
        stage_timings: dict[str, Any] | None = None,
        early_stop_progress: list[dict[str, Any]] | None = None,
        low_confidence_sparse_market_reason: str | None = None,
    ) -> ProviderResult:
        from ..filters import filter_comps

        if not attempts:
            raise ProviderParseError(
                "No eBay query attempts were available",
                diagnostics={"providerDomain": request.provider_domain},
            )
        aggregate_timings = StageTimings()
        with _StageTimer(aggregate_timings, "evidence_filtering"):
            evaluated = filter_comps(request.price_key, comps)
        progress_summaries = early_stop_progress or build_attempt_progress_summaries(request, attempts)
        query_attempts = build_query_attempt_summaries(attempts, evaluated, progress_summaries)
        latest_progress = progress_summaries[-1] if progress_summaries else {}
        quality_summary = build_quality_summary(comps, request=request)
        attempted_quality_summary = _merge_quality_summaries(
            [
                result.raw_metadata.get("qualitySummary") or {}
                for _search_query, result in attempts
                if isinstance(result.raw_metadata.get("qualitySummary") or {}, dict)
            ]
        )
        all_parser_errors: list[dict[str, Any]] = []
        for search_query, result in attempts:
            for error in result.raw_metadata.get("parserErrors") or []:
                if isinstance(error, dict):
                    all_parser_errors.append(
                        {
                            "query_index": search_query.query_index,
                            "query_source": search_query.query_source,
                            **error,
                        }
                    )
        first_query = attempts[0][0]
        query_used = " || ".join(search_query.query_text for search_query, _result in attempts)
        metadata = sanitize_provider_diagnostics(
            {
                "providerDomain": first_query.provider_domain,
                "providerMarketplaceId": first_query.provider_marketplace_id,
                "marketCountry": first_query.market_country,
                "currency": first_query.currency,
                "resultCount": len(comps),
                "rawResultCountBeforeDedupe": sum(len(result.comps) for _query, result in attempts),
                "dedupedResultCount": len(comps),
                "duplicateCount": max(0, sum(len(result.comps) for _query, result in attempts) - len(comps)),
                "maxResults": self.config.max_results,
                "browserConfig": self.config.safe_diagnostics(),
                "queryDiagnostics": first_query.diagnostics,
                "queryAttempts": query_attempts,
                "failedQueryAttempts": failed_attempts or [],
                "queryAttemptsUsed": len(attempts),
                "queryAttemptLimit": query_attempt_limit,
                "queryStopReason": stop_reason,
                "providerOutcome": "success" if comps else "no_results",
                "diagnosticStages": DIAGNOSTIC_STAGES if comps else (*DIAGNOSTIC_STAGES[:-2], "no_price", "complete"),
                "earlyStopApplied": stop_reason != "all_query_attempts_exhausted",
                "cumulativeIncludedAfterEachAttempt": [
                    item.get("cumulativeIncludedAfterAttempt") for item in progress_summaries
                ],
                "cumulativeRejectedAfterEachAttempt": [
                    item.get("cumulativeRejectedAfterAttempt") for item in progress_summaries
                ],
                "newUniqueCandidatesPerAttempt": [
                    item.get("newUniqueCandidatesPerAttempt") for item in progress_summaries
                ],
                "duplicateCandidatesPerAttempt": [
                    item.get("duplicateCandidatesPerAttempt") for item in progress_summaries
                ],
                "cleanIncludedCount": latest_progress.get("cleanIncludedCount", 0),
                "cleanExactCompCount": latest_progress.get("cleanExactCompCount", 0),
                "cleanRecentCompCount": latest_progress.get("cleanRecentCompCount", 0),
                "cleanStaleCompCount": latest_progress.get("cleanStaleCompCount", 0),
                "oldestCleanCompDate": latest_progress.get("oldestCleanCompDate"),
                "newestCleanCompDate": latest_progress.get("newestCleanCompDate"),
                "soldListingRecencyThresholdDays": latest_progress.get("soldListingRecencyThresholdDays"),
                "singleCleanCompOnly": latest_progress.get("singleCleanCompOnly", False),
                "staleEvidenceOnly": latest_progress.get("staleEvidenceOnly", False),
                "exactIdentityResultCount": latest_progress.get("exactIdentityResultCount", 0),
                "wrongCollectorNumberRejectedCount": latest_progress.get("wrongCollectorNumberRejectedCount", 0),
                "wrongCardNameRejectedCount": latest_progress.get("wrongCardNameRejectedCount", 0),
                "wrongVariantRejectedCount": latest_progress.get("wrongVariantRejectedCount", 0),
                "selectorRejectedCount": latest_progress.get("selectorRejectedCount", 0),
                "wrongLanguageRejectedCount": latest_progress.get("wrongLanguageRejectedCount", 0),
                "noisyResultRatio": latest_progress.get("noisyResultRatio", 0.0),
                "lowConfidenceSparseMarketReason": low_confidence_sparse_market_reason,
                "stageTimings": {
                    **(stage_timings or {}),
                    "aggregate": aggregate_timings.snapshot(),
                },
                "marketScope": self.config.market_scope,
                "qualitySummary": quality_summary,
                "attemptedQualitySummaryBeforeDedupe": attempted_quality_summary,
                "parserErrors": all_parser_errors[:50],
            }
        )
        provider_result = ProviderResult(
            provider_name=self.provider_name,
            marketplace=first_query.provider_marketplace_id,
            provider_fingerprint=self._aggregate_provider_fingerprint(attempts),
            query_used=query_used,
            comps=comps,
            raw_metadata=metadata,
        )
        with _StageTimer(aggregate_timings, "report_writing"):
            self._write_aggregate_debug_artifacts(
                request=request,
                provider_result=provider_result,
                evaluated=evaluated,
            )
        provider_result.raw_metadata["stageTimings"]["aggregate"] = aggregate_timings.snapshot()
        return provider_result

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
        launch_timeout_ms = self.config.launch_timeout_seconds * 1000
        stage_timings = StageTimings()
        with sync_playwright() as playwright:
            context: Any = None
            try:
                profile_dir = self.config.ensure_profile_dir()
                try:
                    with _StageTimer(stage_timings, "launch_browser"):
                        context = playwright.chromium.launch_persistent_context(
                            str(profile_dir),
                            channel=self.config.channel,
                            headless=self.config.headless,
                            locale=request.search_locale,
                            viewport={"width": 1366, "height": 900},
                            timeout=launch_timeout_ms,
                        )
                except Exception as exc:
                    raise ProviderTemporaryError(
                        "Installed Google Chrome could not be launched through Playwright channel='chrome'. "
                        "Install Google Chrome, then verify Playwright support with: python -m playwright install chromium",
                        diagnostics={
                            "errorType": type(exc).__name__,
                            "browserConfig": self.config.safe_diagnostics(),
                            "timedOutStage": "launch_browser" if _looks_like_timeout(exc) else None,
                            "stageTimings": stage_timings.snapshot(),
                        },
                    ) from exc
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                with _StageTimer(stage_timings, "open_ebay_page"):
                    page.goto(search_query.search_url, wait_until="domcontentloaded", timeout=timeout_ms)
                if is_ebay_authentication_url(page.url):
                    current_url = urlparse(page.url)
                    raise ProviderAuthenticationRequiredError(
                        "eBay redirected the public sold-listing search to authentication; sign-in is not attempted",
                        diagnostics={
                            "providerOutcome": "authentication_redirect",
                            "providerDomain": search_query.provider_domain,
                            "redirectHost": current_url.netloc,
                            "redirectPath": current_url.path,
                            "stageTimings": stage_timings.snapshot(),
                        },
                    )
                with _StageTimer(stage_timings, "apply_sold_completed_filters"):
                    if "LH_Sold=1" not in search_query.search_url or "LH_Complete=1" not in search_query.search_url:
                        raise ProviderParseError(
                            "eBay search URL is missing sold/completed filters",
                            diagnostics={"searchUrl": search_query.search_url},
                        )
                try:
                    with _StageTimer(stage_timings, "wait_for_network_idle"):
                        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15000))
                except PlaywrightTimeoutError:
                    pass
                if is_ebay_authentication_url(page.url):
                    current_url = urlparse(page.url)
                    raise ProviderAuthenticationRequiredError(
                        "eBay redirected the public sold-listing search to authentication; sign-in is not attempted",
                        diagnostics={
                            "providerOutcome": "authentication_redirect",
                            "providerDomain": search_query.provider_domain,
                            "redirectHost": current_url.netloc,
                            "redirectPath": current_url.path,
                            "stageTimings": stage_timings.snapshot(),
                        },
                    )

                try:
                    with _StageTimer(stage_timings, "wait_for_result_container"):
                        page.wait_for_selector(RESULT_CONTAINER_SELECTOR, timeout=timeout_ms)
                except PlaywrightTimeoutError as exc:
                    title = _safe_page_title(page)
                    body_text = _safe_body_text(page)
                    selector_counts = count_candidate_selectors(page)
                    page_state = classify_browser_page_state(
                        title=title,
                        body_text=body_text,
                        selector_counts=selector_counts,
                    )
                    self._write_debug_artifacts(
                        page=page,
                        request=request,
                        search_query=search_query,
                        title=title,
                        body_text=body_text,
                        detected_block=page_state["outcome"] in {"challenge_detected", "access_blocked"},
                        selector_counts=selector_counts,
                        comps=[],
                        parser_errors=[
                            {
                                "errorType": "TimeoutError",
                                "stage": "wait_for_result_container",
                                "browserOutcome": page_state["outcome"],
                                "browserReason": page_state["reason"],
                            }
                        ],
                        stage_timings=stage_timings.snapshot(),
                    )
                    if page_state["outcome"] == "no_results":
                        return ProviderResult(
                            provider_name=self.provider_name,
                            marketplace=search_query.provider_marketplace_id,
                            provider_fingerprint=self._provider_fingerprint(search_query),
                            query_used=search_query.query_text,
                            comps=[],
                            raw_metadata=sanitize_provider_diagnostics(
                                {
                                    "providerDomain": search_query.provider_domain,
                                    "providerMarketplaceId": search_query.provider_marketplace_id,
                                    "marketCountry": search_query.market_country,
                                    "currency": search_query.currency,
                                    "searchUrl": search_query.search_url,
                                    "queryIndex": search_query.query_index,
                                    "querySource": search_query.query_source,
                                    "resultCount": 0,
                                    "providerOutcome": "no_results",
                                    "browserPageState": page_state,
                                    "diagnosticStages": ["browser_launch", "marketplace_attempt", "results_loaded", "no_price"],
                                    "browserConfig": self.config.safe_diagnostics(),
                                    "candidateSelectorCounts": selector_counts,
                                    "stageTimings": stage_timings.snapshot(),
                                }
                            ),
                        )
                    if page_state["outcome"] == "challenge_detected":
                        raise ProviderBlockedError(
                            "eBay returned a verification challenge; captcha bypass is not attempted",
                            diagnostics={
                                "providerOutcome": "challenge_detected",
                                "browserPageState": page_state,
                                "providerDomain": search_query.provider_domain,
                                "searchUrlHost": urlparse(search_query.search_url).netloc,
                                "stageTimings": stage_timings.snapshot(),
                            },
                        ) from exc
                    if page_state["outcome"] == "access_blocked":
                        raise ProviderBlockedError(
                            "eBay returned an access-block page; retry loop stopped",
                            diagnostics={
                                "providerOutcome": "access_blocked",
                                "browserPageState": page_state,
                                "providerDomain": search_query.provider_domain,
                                "searchUrlHost": urlparse(search_query.search_url).netloc,
                                "stageTimings": stage_timings.snapshot(),
                            },
                        ) from exc
                    if page_state["outcome"] == "authentication_required":
                        raise ProviderAuthenticationRequiredError(
                            "eBay browser session requires sign-in before pricing can continue",
                            diagnostics={
                                "providerOutcome": "authentication_required",
                                "browserPageState": page_state,
                                "providerDomain": search_query.provider_domain,
                                "stageTimings": stage_timings.snapshot(),
                            },
                        ) from exc
                    raise ProviderTemporaryError(
                        "Timed out waiting for eBay result container",
                        diagnostics={
                            "errorType": type(exc).__name__,
                            "providerOutcome": "timeout",
                            "browserPageState": page_state,
                            "timedOutStage": "wait_for_result_container",
                            "stageTimings": stage_timings.snapshot(),
                            "candidateSelectorCounts": selector_counts,
                            "debugArtifacts": self._debug_artifact_paths(),
                        },
                    ) from exc

                title = page.title()
                body_text = page.locator("body").inner_text(timeout=5000)
                selector_counts = count_candidate_selectors(page)
                page_state = classify_browser_page_state(
                    title=title,
                    body_text=body_text,
                    selector_counts=selector_counts,
                )
                detected_block = page_state["outcome"] in {"challenge_detected", "access_blocked"}
                if page_state["outcome"] in {"challenge_detected", "access_blocked", "authentication_required"}:
                    self._write_debug_artifacts(
                        page=page,
                        request=request,
                        search_query=search_query,
                        title=title,
                        body_text=body_text,
                        detected_block=detected_block,
                        selector_counts=selector_counts,
                        comps=[],
                        parser_errors=[],
                        stage_timings=stage_timings.snapshot(),
                    )
                    if page_state["outcome"] == "authentication_required":
                        raise ProviderAuthenticationRequiredError(
                            "eBay browser session requires sign-in before pricing can continue",
                            diagnostics={
                                "providerOutcome": "authentication_required",
                                "browserPageState": page_state,
                                "providerDomain": search_query.provider_domain,
                                "searchUrlHost": urlparse(search_query.search_url).netloc,
                                "stageTimings": stage_timings.snapshot(),
                            },
                        )
                    raise ProviderBlockedError(
                        "eBay returned a block or verification page; captcha bypass is not attempted",
                        diagnostics={
                            "providerOutcome": page_state["outcome"],
                            "browserPageState": page_state,
                            "pageTitle": title,
                            "providerDomain": search_query.provider_domain,
                            "searchUrlHost": urlparse(search_query.search_url).netloc,
                            "stageTimings": stage_timings.snapshot(),
                        },
                    )

                with _StageTimer(stage_timings, "parse_result_rows"):
                    comps, parser_errors, visible_sample = self._parse_page(
                        page=page,
                        request=request,
                        search_query=search_query,
                    )
                quality_summary = build_quality_summary(comps, request=request)
                for error in parser_errors:
                    url_quality = error.get("url_quality")
                    if url_quality == "generic_non_item":
                        quality_summary["generic_url_count"] += 1
                    elif url_quality in {"missing", "malformed_or_non_ebay"}:
                        quality_summary["missing_url_count"] += 1
                self._write_debug_artifacts(
                    page=page,
                    request=request,
                    search_query=search_query,
                    title=title,
                    body_text=body_text,
                    detected_block=detected_block,
                    selector_counts=selector_counts,
                    comps=comps,
                    parser_errors=parser_errors,
                    visible_result_text_sample=visible_sample,
                    quality_summary=quality_summary,
                    stage_timings=stage_timings.snapshot(),
                )
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
                            "searchUrl": search_query.search_url,
                            "queryIndex": search_query.query_index,
                            "querySource": search_query.query_source,
                            "resultCount": len(comps),
                            "providerOutcome": "success" if comps else "no_results",
                            "browserPageState": page_state,
                            "diagnosticStages": DIAGNOSTIC_STAGES,
                            "maxResults": self.config.max_results,
                            "browserConfig": self.config.safe_diagnostics(),
                            "queryDiagnostics": search_query.diagnostics,
                            "marketScope": self.config.market_scope,
                            "qualitySummary": quality_summary,
                            "candidateSelectorCounts": selector_counts,
                            "parserErrors": parser_errors[:20],
                            "visibleResultTextSample": visible_sample,
                            "stageTimings": stage_timings.snapshot(),
                        }
                    ),
                )
            finally:
                if context is not None:
                    context.close()

    def _parse_page(
        self,
        *,
        page: Any,
        request: ProviderRequest,
        search_query: ProviderSearchQuery,
    ) -> tuple[list[SoldComp], list[dict[str, Any]], str]:
        candidates = collect_candidate_dicts(page, max_results=self.config.max_results * 3)
        comps: list[SoldComp] = []
        parse_errors: list[dict[str, Any]] = []
        visible_sample = ""
        for index, candidate in enumerate(candidates):
            if not visible_sample and candidate.get("text"):
                visible_sample = _normalise_text(candidate.get("text"))[:1000]
            try:
                comp = parse_candidate_dict(
                    candidate,
                    index=index,
                    request=request,
                    search_query=search_query,
                )
            except Exception as exc:
                parse_errors.append({"index": index, "errorType": type(exc).__name__, "source": candidate.get("source")})
                continue
            if comp is not None:
                comps.append(comp)
                if len(comps) >= self.config.max_results:
                    break
            else:
                url_metadata = normalize_ebay_listing_url(
                    str(candidate.get("href") or ""),
                    provider_domain=search_query.provider_domain,
                )
                parse_errors.append(
                    {
                        "index": index,
                        "errorType": "candidate_not_parseable",
                        "source": candidate.get("source"),
                        "url_quality": url_metadata["url_quality"],
                        "original_href": url_metadata["original_href"],
                    }
                )
        return comps, parse_errors, visible_sample

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
        url_metadata = normalize_ebay_listing_url(href, provider_domain=search_query.provider_domain)
        listing_url = str(url_metadata.get("normalized_listing_url") or "")
        if url_metadata["url_quality"] != "direct_item" or not listing_url:
            return None
        source_listing_id = source_listing_id_from_url(listing_url, index=index)
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
                    **url_metadata,
                    "providerMarketplaceId": search_query.provider_marketplace_id,
                    "query_index": search_query.query_index,
                    "query_source": search_query.query_source,
                    "query_style": search_query.diagnostics.get("queryStyle") or "unquoted_discovery",
                    "query_text": search_query.query_text,
                    "query_search_url": search_query.search_url,
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
        return source_listing_id_from_url(listing_url, index=index)

    def _provider_fingerprint(self, search_query: ProviderSearchQuery) -> str:
        digest = hashlib.sha256(search_query.search_url.encode("utf-8")).hexdigest()[:16]
        return f"ebay_browser:{search_query.provider_marketplace_id}:{digest}"

    def _aggregate_provider_fingerprint(self, attempts: list[tuple[ProviderSearchQuery, ProviderResult]]) -> str:
        joined = "|".join(search_query.search_url for search_query, _result in attempts)
        marketplace = attempts[0][0].provider_marketplace_id
        digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
        return f"ebay_browser:{marketplace}:aggregate:{digest}"

    def _write_aggregate_debug_artifacts(
        self,
        *,
        request: ProviderRequest,
        provider_result: ProviderResult,
        evaluated: list[Any],
    ) -> None:
        if self.config.debug_artifact_dir is None:
            return
        from ..pricing_stats import calculate_pricing_stats

        pricing_stats = calculate_pricing_stats(
            evaluated,
            config=MarketEngineConfig.from_env(require_supabase=False),
        )
        included = [item for item in evaluated if item.included_in_estimate]
        rejected = [item for item in evaluated if not item.included_in_estimate]
        summary = sanitize_provider_diagnostics(
            {
                "timestamp": utc_iso(),
                "aggregate": True,
                "search_url": (provider_result.raw_metadata.get("queryAttempts") or [{}])[0].get("search_url"),
                "query_attempts": provider_result.raw_metadata.get("queryAttempts") or [],
                "failed_query_attempts": provider_result.raw_metadata.get("failedQueryAttempts") or [],
                "query_attempts_used": provider_result.raw_metadata.get("queryAttemptsUsed"),
                "query_stop_reason": provider_result.raw_metadata.get("queryStopReason"),
                "early_stop_applied": provider_result.raw_metadata.get("earlyStopApplied"),
                "cumulative_included_after_each_attempt": provider_result.raw_metadata.get("cumulativeIncludedAfterEachAttempt") or [],
                "cumulative_rejected_after_each_attempt": provider_result.raw_metadata.get("cumulativeRejectedAfterEachAttempt") or [],
                "new_unique_candidates_per_attempt": provider_result.raw_metadata.get("newUniqueCandidatesPerAttempt") or [],
                "duplicate_candidates_per_attempt": provider_result.raw_metadata.get("duplicateCandidatesPerAttempt") or [],
                "clean_included_count": provider_result.raw_metadata.get("cleanIncludedCount"),
                "clean_exact_comp_count": provider_result.raw_metadata.get("cleanExactCompCount"),
                "clean_recent_comp_count": provider_result.raw_metadata.get("cleanRecentCompCount"),
                "clean_stale_comp_count": provider_result.raw_metadata.get("cleanStaleCompCount"),
                "oldest_clean_comp_date": provider_result.raw_metadata.get("oldestCleanCompDate"),
                "newest_clean_comp_date": provider_result.raw_metadata.get("newestCleanCompDate"),
                "sold_listing_recency_threshold_days": provider_result.raw_metadata.get("soldListingRecencyThresholdDays"),
                "single_clean_comp_only": provider_result.raw_metadata.get("singleCleanCompOnly"),
                "stale_evidence_only": provider_result.raw_metadata.get("staleEvidenceOnly"),
                "exact_identity_result_count": provider_result.raw_metadata.get("exactIdentityResultCount"),
                "wrong_collector_number_rejected_count": provider_result.raw_metadata.get("wrongCollectorNumberRejectedCount"),
                "wrong_card_name_rejected_count": provider_result.raw_metadata.get("wrongCardNameRejectedCount"),
                "wrong_variant_rejected_count": provider_result.raw_metadata.get("wrongVariantRejectedCount"),
                "selector_rejected_count": provider_result.raw_metadata.get("selectorRejectedCount"),
                "wrong_language_rejected_count": provider_result.raw_metadata.get("wrongLanguageRejectedCount"),
                "noisy_result_ratio": provider_result.raw_metadata.get("noisyResultRatio"),
                "low_confidence_sparse_market_reason": provider_result.raw_metadata.get("lowConfidenceSparseMarketReason"),
                "stage_timings": provider_result.raw_metadata.get("stageTimings") or {},
                "result_count": len(provider_result.comps),
                "raw_result_count_before_dedupe": provider_result.raw_metadata.get("rawResultCountBeforeDedupe"),
                "deduped_result_count": provider_result.raw_metadata.get("dedupedResultCount"),
                "duplicate_count": provider_result.raw_metadata.get("duplicateCount"),
                "quality_summary": provider_result.raw_metadata.get("qualitySummary") or {},
                "price_spread_ratio": pricing_stats.price_spread_ratio,
                "confidence": pricing_stats.confidence,
                "confidence_warnings": list(pricing_stats.confidence_warnings),
                "included_price_distribution": list(pricing_stats.included_price_distribution),
                "final_price_basis": pricing_stats.price_basis,
                "recommended_price": pricing_stats.recommended_price,
                "no_reliable_price_reason": pricing_stats.no_reliable_price_reason,
                "price_reliability": pricing_stats.price_reliability,
                "top_included_comps": [_compact_evaluated_comp(item) for item in included[:10]],
                "top_rejected_comps": [_compact_evaluated_comp(item) for item in rejected[:20]],
                "parser_errors": provider_result.raw_metadata.get("parserErrors") or [],
                "browser_config": self.config.safe_diagnostics(),
                "market_config": {
                    "marketCountry": request.market_country,
                    "currency": request.currency,
                    "marketplace": request.marketplace,
                    "providerMarketplaceId": request.provider_marketplace_id,
                    "providerDomain": request.provider_domain,
                    "searchLocale": request.search_locale,
                },
                "query_text": provider_result.query_used,
            }
        )
        latest_dir = self.config.debug_artifact_dir
        latest_dir.mkdir(parents=True, exist_ok=True)
        write_json(latest_dir / "debug_summary.json", summary)
        append_jsonl(DEBUG_REPORTS_DIR / "runs.jsonl", summary)

    def _write_timeout_debug_summary(
        self,
        *,
        request: ProviderRequest,
        failed_attempts: list[dict[str, Any]],
        stage_timings: dict[str, Any],
        stop_reason: str,
    ) -> None:
        if self.config.debug_artifact_dir is None:
            return
        latest_dir = self.config.debug_artifact_dir
        latest_dir.mkdir(parents=True, exist_ok=True)
        summary = sanitize_provider_diagnostics(
            {
                "timestamp": utc_iso(),
                "status": "failed",
                "query_stop_reason": stop_reason,
                "failed_query_attempts": failed_attempts,
                "stage_timings": stage_timings,
                "browser_config": self.config.safe_diagnostics(),
                "market_config": {
                    "marketCountry": request.market_country,
                    "currency": request.currency,
                    "marketplace": request.marketplace,
                    "providerMarketplaceId": request.provider_marketplace_id,
                    "providerDomain": request.provider_domain,
                    "searchLocale": request.search_locale,
                },
            }
        )
        write_json(latest_dir / "debug_summary.json", summary)
        append_jsonl(DEBUG_REPORTS_DIR / "runs.jsonl", summary)

    def _debug_artifact_paths(self) -> dict[str, str] | None:
        if self.config.debug_artifact_dir is None:
            return None
        return {
            "directory": str(self.config.debug_artifact_dir),
            "pageHtml": str(self.config.debug_artifact_dir / "page.html"),
            "screenshot": str(self.config.debug_artifact_dir / "screenshot.png"),
            "summary": str(self.config.debug_artifact_dir / "debug_summary.json"),
        }

    def _write_debug_artifacts(
        self,
        *,
        page: Any,
        request: ProviderRequest,
        search_query: ProviderSearchQuery,
        title: str,
        body_text: str,
        detected_block: bool,
        selector_counts: dict[str, int],
        comps: list[SoldComp],
        parser_errors: list[dict[str, Any]],
        visible_result_text_sample: str = "",
        quality_summary: dict[str, int] | None = None,
        stage_timings: dict[str, Any] | None = None,
    ) -> None:
        if self.config.debug_artifact_dir is None:
            return
        latest_dir = self.config.debug_artifact_dir
        latest_dir.mkdir(parents=True, exist_ok=True)
        try:
            (latest_dir / "page.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        try:
            page.screenshot(path=str(latest_dir / "screenshot.png"), full_page=True)
        except Exception:
            pass
        from ..filters import filter_comps
        from ..pricing_stats import calculate_pricing_stats

        evaluated = filter_comps(request.price_key, comps)
        pricing_stats = calculate_pricing_stats(
            evaluated,
            config=MarketEngineConfig.from_env(require_supabase=False),
        )

        summary = sanitize_provider_diagnostics(
            {
                "timestamp": utc_iso(),
                "search_url": search_query.search_url,
                "query_attempts": [
                    {
                        "query_index": search_query.query_index,
                        "query_source": search_query.query_source,
                        "query_text": search_query.query_text,
                        "search_url": search_query.search_url,
                        "result_count": len(comps),
                    }
                ],
                "page_url_after_load": getattr(page, "url", ""),
                "page_title": title,
                "detected_block_or_captcha": detected_block,
                "visible_result_text_sample": visible_result_text_sample,
                "body_text_sample": _normalise_text(body_text)[:2000],
                "candidate_selector_counts": selector_counts,
                "stage_timings": stage_timings or {},
                "result_count": len(comps),
                "quality_summary": quality_summary or {},
                "sample_urls": [
                    {
                        "url_quality": comp.raw_metadata.get("url_quality"),
                        "item_id": comp.raw_metadata.get("item_id"),
                        "listing_url": comp.listing_url or None,
                        "original_href": comp.raw_metadata.get("original_href"),
                    }
                    for comp in comps[:10]
                ],
                "price_spread_ratio": pricing_stats.price_spread_ratio,
                "confidence": pricing_stats.confidence,
                "confidence_warnings": list(pricing_stats.confidence_warnings),
                "included_price_distribution": list(pricing_stats.included_price_distribution),
                "final_price_basis": pricing_stats.price_basis,
                "recommended_price": pricing_stats.recommended_price,
                "no_reliable_price_reason": pricing_stats.no_reliable_price_reason,
                "price_reliability": pricing_stats.price_reliability,
                "clean_recent_comp_count": pricing_stats.clean_recent_comp_count,
                "clean_stale_comp_count": pricing_stats.clean_stale_comp_count,
                "oldest_clean_comp_date": utc_iso(pricing_stats.oldest_clean_comp_date) if pricing_stats.oldest_clean_comp_date else None,
                "newest_clean_comp_date": utc_iso(pricing_stats.newest_clean_comp_date) if pricing_stats.newest_clean_comp_date else None,
                "sold_listing_recency_threshold_days": pricing_stats.sold_listing_recency_threshold_days,
                "top_included_comps": [_compact_evaluated_comp(item) for item in evaluated if item.included_in_estimate][:5],
                "top_rejected_comps": [_compact_evaluated_comp(item) for item in evaluated if not item.included_in_estimate][:10],
                "parser_errors": parser_errors[:50],
                "browser_config": self.config.safe_diagnostics(),
                "market_config": {
                    "marketCountry": request.market_country,
                    "currency": request.currency,
                    "marketplace": request.marketplace,
                    "providerMarketplaceId": request.provider_marketplace_id,
                    "providerDomain": request.provider_domain,
                    "searchLocale": request.search_locale,
                },
                "query_text": search_query.query_text,
            }
        )
        write_json(latest_dir / "debug_summary.json", summary)
        append_jsonl(DEBUG_REPORTS_DIR / "runs.jsonl", summary)
