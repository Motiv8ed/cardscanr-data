from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

from ..config import DEFAULT_EBAY_BROWSER_PROFILE_NAME, DEFAULT_EBAY_BROWSER_USER_DATA_DIR, ROOT
from ..models import ProviderRequest, ProviderResult, SoldComp
from .errors import (
    ProviderBlockedError,
    ProviderDisabledError,
    ProviderError,
    ProviderParseError,
    ProviderTemporaryError,
    ProviderUnsupportedMarketError,
    sanitize_provider_diagnostics,
)
from .query_builder import ProviderSearchQuery, build_provider_search_query


BLOCK_TEXT_MARKERS = ("captcha", "verify", "robot", "unusual traffic", "access denied", "blocked")
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
PICK_YOUR_CARD_PATTERNS = (
    "choose your card",
    "you pick",
    "pick your card",
    "singles common",
    "holo/reverse/ex",
    "reverse/holo/ex",
)
LOT_BUNDLE_PATTERNS = (" lot ", " bundle ", " collection ")
GRADED_PATTERNS = (" psa ", " bgs ", " cgc ", " sgc ", " graded ", " slab ")
SEALED_PATTERNS = (" booster ", " sealed ", " pack ", " etb ", " elite trainer box ")
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


def contains_block_marker(*, title: str = "", body_text: str = "") -> bool:
    haystack = f"{title}\n{body_text}".lower()
    return any(marker in haystack for marker in BLOCK_TEXT_MARKERS)


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
    if "/itm/" not in href:
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
    listing_url = href.split("?", 1)[0]
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
                "providerMarketplaceId": search_query.provider_marketplace_id,
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
    cooldown_seconds: int
    min_seconds_between_requests: int
    user_data_dir: Path
    debug_artifact_dir: Path | None
    market_scope: str

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
            cooldown_seconds=_parse_positive_int("EBAY_BROWSER_COOLDOWN_SECONDS", 20),
            min_seconds_between_requests=_parse_positive_int("EBAY_BROWSER_MIN_SECONDS_BETWEEN_REQUESTS", 20),
            user_data_dir=user_data_dir,
            debug_artifact_dir=Path(os.getenv("EBAY_BROWSER_DEBUG_ARTIFACT_DIR", "").strip())
            if os.getenv("EBAY_BROWSER_DEBUG_ARTIFACT_DIR", "").strip()
            else None,
            market_scope=os.getenv("EBAY_MARKET_SCOPE", "marketplace").strip().lower() or "marketplace",
        )
        config.validate()
        return config

    def validate(self) -> None:
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
                "userDataDir": str(self.user_data_dir),
                "headless": self.headless,
                "maxResults": self.max_results,
                "timeoutSeconds": self.timeout_seconds,
                "cooldownSeconds": self.cooldown_seconds,
                "minSecondsBetweenRequests": self.min_seconds_between_requests,
                "debugArtifactDir": str(self.debug_artifact_dir) if self.debug_artifact_dir else None,
                "marketScope": self.market_scope,
            }
        )


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
    requested_name = request.price_key.normalized_card_name.replace("_", " ").lower() or request.price_key.card_name.lower()
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
    }
    for comp in comps:
        title = comp.title.lower()
        raw = comp.raw_metadata
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
        except ProviderError:
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
            context: Any = None
            try:
                profile_dir = self.config.ensure_profile_dir()
                try:
                    context = playwright.chromium.launch_persistent_context(
                        str(profile_dir),
                        channel=self.config.channel,
                        headless=self.config.headless,
                        locale=request.search_locale,
                        viewport={"width": 1366, "height": 900},
                    )
                except Exception as exc:
                    raise ProviderTemporaryError(
                        "Installed Google Chrome could not be launched through Playwright channel='chrome'. "
                        "Install Google Chrome, then verify Playwright support with: python -m playwright install chromium",
                        diagnostics={
                            "errorType": type(exc).__name__,
                            "browserConfig": self.config.safe_diagnostics(),
                        },
                    ) from exc
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                page.goto(search_query.search_url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15000))
                except PlaywrightTimeoutError:
                    pass

                title = page.title()
                body_text = page.locator("body").inner_text(timeout=5000)
                selector_counts = count_candidate_selectors(page)
                detected_block = contains_block_marker(title=title, body_text=body_text)
                if detected_block:
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
                    )
                    raise ProviderBlockedError(
                        "eBay returned a block or verification page; captcha bypass is not attempted",
                        diagnostics={
                            "pageTitle": title,
                            "providerDomain": search_query.provider_domain,
                            "searchUrlHost": urlparse(search_query.search_url).netloc,
                        },
                    )

                comps, parser_errors, visible_sample = self._parse_page(
                    page=page,
                    request=request,
                    search_query=search_query,
                )
                quality_summary = build_quality_summary(comps, request=request)
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
                            "resultCount": len(comps),
                            "maxResults": self.config.max_results,
                            "browserConfig": self.config.safe_diagnostics(),
                            "queryDiagnostics": search_query.diagnostics,
                            "marketScope": self.config.market_scope,
                            "qualitySummary": quality_summary,
                            "candidateSelectorCounts": selector_counts,
                            "parserErrors": parser_errors[:20],
                            "visibleResultTextSample": visible_sample,
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
                parse_errors.append({"index": index, "errorType": "candidate_not_parseable", "source": candidate.get("source")})
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
        listing_url = href.split("?", 1)[0]
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
        return source_listing_id_from_url(listing_url, index=index)

    def _provider_fingerprint(self, search_query: ProviderSearchQuery) -> str:
        digest = hashlib.sha256(search_query.search_url.encode("utf-8")).hexdigest()[:16]
        return f"ebay_browser:{search_query.provider_marketplace_id}:{digest}"

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
        summary = sanitize_provider_diagnostics(
            {
                "timestamp": utc_iso(),
                "search_url": search_query.search_url,
                "page_url_after_load": getattr(page, "url", ""),
                "page_title": title,
                "detected_block_or_captcha": detected_block,
                "visible_result_text_sample": visible_result_text_sample,
                "body_text_sample": _normalise_text(body_text)[:2000],
                "candidate_selector_counts": selector_counts,
                "result_count": len(comps),
                "quality_summary": quality_summary or {},
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
