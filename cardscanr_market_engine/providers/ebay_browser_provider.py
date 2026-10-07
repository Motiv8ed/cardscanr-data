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

from ..bulk.set_id_aliases import is_smoke_pricing_key
from ..config import DEFAULT_EBAY_BROWSER_PROFILE_NAME, DEFAULT_EBAY_BROWSER_USER_DATA_DIR, ROOT, MarketEngineConfig
from ..models import ProviderRequest, ProviderResult, SoldComp
from .errors import (
    ProviderAuthenticationRequiredError,
    ProviderBlockedError,
    ProviderDisabledError,
    ProviderError,
    ProviderIdentityUnavailableError,
    ProviderMarketplaceMismatchError,
    ProviderParseError,
    ProviderTemporaryError,
    ProviderUnsupportedMarketError,
    sanitize_provider_diagnostics,
)
from .sold_page_health import is_ebay_authentication_url
from .identity_guard import ENGLISH_MARKET_IDENTITY_UNAVAILABLE, evaluate_english_market_identity
from .query_builder import ProviderSearchQuery, build_provider_search_queries
from ..marketplaces import ebay_host_matches_provider_domain, normalize_ebay_host
from ..navigation_runtime_context import (
    classify_pre_submit_runtime_error,
    exception_diagnostics,
    load_navigation_runtime_context,
    pre_submit_only_requested,
)


# ACTIVE challenge: user-facing verification copy (never bare "captcha" alone).
ACTIVE_CHALLENGE_VISIBLE_TEXT_MARKERS = (
    "verify you are human",
    "verify yourself",
    "are you a robot",
    "security challenge",
    "robot check",
    "confirm you are human",
    "please verify yourself",
    "to continue, please verify",
    "press and hold",
    "security measure",
)
# ACTIVE challenge: known eBay security/challenge navigation surfaces.
ACTIVE_CHALLENGE_URL_MARKERS = (
    "/splashui/",
    "captcha.ebay.",
    "/challenge?",
    "/challenge/",
)
# Backward-compatible alias used by older tests/helpers (active visible text only).
CHALLENGE_TEXT_MARKERS = ACTIVE_CHALLENGE_VISIBLE_TEXT_MARKERS
ACCESS_BLOCK_TEXT_MARKERS = ("access denied", "unusual traffic", "temporarily blocked", "blocked from using")
AUTH_TEXT_MARKERS = ("sign in to continue", "please sign in", "session expired", "log in to continue")
CONSENT_TEXT_MARKERS = ("accept all", "cookie consent", "privacy preferences")
MAINTENANCE_TEXT_MARKERS = ("technical difficulties", "temporarily unavailable", "site maintenance")
SORRY_ERROR_TEXT_MARKERS = ("something went wrong on our end", "sorry\nsomething went wrong")
NO_RESULTS_TEXT_MARKERS = ("0 results", "no exact matches found", "no results for", "we looked everywhere")
RESULT_TEXT_MARKERS = ("sold items", "completed items", "results for", "shop by category", "sold listings")
BLOCK_TEXT_MARKERS = ACTIVE_CHALLENGE_VISIBLE_TEXT_MARKERS + ACCESS_BLOCK_TEXT_MARKERS
# Passiveive CAPTCHA/challenge capability in page source — diagnostic only.
PASSIVE_CHALLENGE_RESOURCE_RES = (
    re.compile(r"google\.com/recaptcha", re.I),
    re.compile(r"gstatic\.com/recaptcha", re.I),
    re.compile(r"recaptcha/api", re.I),
    re.compile(r"\.ifh-captcha\b", re.I),
    re.compile(r"\bifh-captcha\b", re.I),
    re.compile(r"""['"][^'"]*captcha[^'"]*\.(?:js|css)""", re.I),
)
PASSIVE_HIDDEN_RECAPTCHA_IFRAME_RE = re.compile(
    r"""<iframe[^>]+src=["'][^"']*recaptcha[^"']*["'][^>]*>""",
    re.I,
)

# Sold/Completed are applied via on-page refine links after an active (unsold) search.
# Deep-linking LH_Sold/LH_Complete on first navigation is rejected by ebay.com.au.
SOLD_ITEMS_FILTER_SELECTORS = (
    'a.su-selection-group__link:has-text("Sold items")',
    'a.su-selection-group__link:has-text("Sold Items")',
    'a[href*="LH_Sold=1"]',
)
COMPLETED_ITEMS_FILTER_SELECTORS = (
    'a.su-selection-group__link:has-text("Completed items")',
    'a.su-selection-group__link:has-text("Completed Items")',
    'a[href*="LH_Complete=1"]:not([href*="LH_Sold=1"])',
    'a[href*="LH_Complete=1"]',
)
DEFAULT_SOLD_DATE = datetime(1970, 1, 1, tzinfo=timezone.utc)
SUPPORTED_MARKET_ROUTES = {
    ("AU", "AUD"),
    ("US", "USD"),
    ("GB", "GBP"),
    ("CA", "CAD"),
    ("DE", "EUR"),
    ("FR", "EUR"),
    ("IT", "EUR"),
    ("ES", "EUR"),
}
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
    "pre-owned",
    "pre owned",
    "brand new",
    "best offer accepted",
    "buy it now",
)
# Exact UI-control titles occasionally scraped from US sold-result cards.
CHROME_ONLY_TITLES = frozenset(
    {
        "buy it now",
        "best offer",
        "best offer accepted",
        "or",
        "add to cart",
        "shop now",
        "watch",
        "watching",
        "bids",
        "1 bid",
        "2 bids",
        "3 bids",
        "sponsored",
        "pre-owned",
        "pre owned",
        "brand new",
        "new listing",
        "see all",
        "more options",
        "make offer",
    }
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
SOLD_DATE_LINE_RE = re.compile(
    r"^sold\s+(?:[0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4}|[A-Za-z]{3,9}\s+[0-9]{1,2},\s+[0-9]{4})",
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
SUPPORTED_EBAY_DOMAINS = (
    "ebay.com.au",
    "ebay.com",
    "ebay.co.uk",
    "ebay.ca",
    "ebay.de",
    "ebay.fr",
    "ebay.it",
    "ebay.es",
)
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
# Process-local Timeout A instrumentation (feature-flagged recording; lightweight fields always on timeout).
WAIT_DURATION_BUCKETS_MS = (
    ("lt_5s", 5_000),
    ("lt_15s", 15_000),
    ("lt_30s", 30_000),
    ("lt_60s", 60_000),
    ("gte_60s", None),
)
_WAIT_FOR_RESULT_CONTAINER_BUCKET_COUNTS: Counter[str] = Counter()
_BROWSER_SESSION_RECYCLE_EVENTS = 0
_TIMEOUT_INSTRUMENTATION_LOCK = threading.Lock()
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


def timeout_instrumentation_enabled() -> bool:
    """EBAY_BROWSER_TIMEOUT_INSTRUMENTATION=1 enables histogram recording (default OFF)."""
    return _parse_bool("EBAY_BROWSER_TIMEOUT_INSTRUMENTATION", False)


def smoke_failfast_enabled() -> bool:
    """CS-012e-B0: smoke empty-DOM fail-fast (default ON). Set EBAY_BROWSER_B0_SMOKE_FAILFAST=0 to disable."""
    return _parse_bool("EBAY_BROWSER_B0_SMOKE_FAILFAST", True)


def smoke_failfast_settle_ms(*, timeout_ms: int) -> int:
    """Short settle budget for smoke keys — never exceeds the normal timeout."""
    raw = os.getenv("EBAY_BROWSER_B0_SMOKE_SETTLE_MS", "3500").strip() or "3500"
    try:
        settle = int(raw)
    except ValueError:
        settle = 3500
    settle = max(500, settle)
    return min(int(timeout_ms), settle)


def should_fail_fast_empty_result_dom(
    *,
    is_smoke: bool,
    selector_counts: dict[str, int] | None,
    page_state: dict[str, Any] | None,
) -> bool:
    """Tight gate: smoke + zero selectors + unknown/empty page state only."""
    if not is_smoke:
        return False
    selectors = selector_counts or {}
    if any(int(v or 0) > 0 for v in selectors.values()):
        return False
    state = page_state or {}
    outcome = str(state.get("outcome") or "")
    reason = str(state.get("reason") or "")
    if outcome == "no_results":
        return True
    if outcome == "parsing_failure" and reason in {"unknown_page_state", "unknown_page_state"}:
        return True
    if outcome == "parsing_failure" and "unknown" in reason:
        return True
    return False


def wait_duration_bucket(duration_ms: float) -> str:
    for label, upper_ms in WAIT_DURATION_BUCKETS_MS:
        if upper_ms is None or duration_ms < upper_ms:
            return label
    return "gte_60s"


def record_wait_for_result_container_duration(duration_ms: float) -> str:
    """Record a wait duration into process-local buckets when instrumentation is enabled."""
    bucket = wait_duration_bucket(duration_ms)
    if timeout_instrumentation_enabled():
        with _TIMEOUT_INSTRUMENTATION_LOCK:
            _WAIT_FOR_RESULT_CONTAINER_BUCKET_COUNTS[bucket] += 1
    return bucket


def snapshot_wait_for_result_container_histogram() -> dict[str, int]:
    with _TIMEOUT_INSTRUMENTATION_LOCK:
        return {label: int(_WAIT_FOR_RESULT_CONTAINER_BUCKET_COUNTS.get(label, 0)) for label, _ in WAIT_DURATION_BUCKETS_MS}


def reset_timeout_instrumentation_counters() -> None:
    """Test helper: clear process-local Timeout A counters."""
    global _BROWSER_SESSION_RECYCLE_EVENTS
    with _TIMEOUT_INSTRUMENTATION_LOCK:
        _WAIT_FOR_RESULT_CONTAINER_BUCKET_COUNTS.clear()
        _BROWSER_SESSION_RECYCLE_EVENTS = 0


def note_browser_session_recycle() -> None:
    global _BROWSER_SESSION_RECYCLE_EVENTS
    with _TIMEOUT_INSTRUMENTATION_LOCK:
        _BROWSER_SESSION_RECYCLE_EVENTS += 1


def browser_session_recycle_events() -> int:
    with _TIMEOUT_INSTRUMENTATION_LOCK:
        return int(_BROWSER_SESSION_RECYCLE_EVENTS)


def build_result_container_timeout_diagnostics(
    *,
    page_url: str | None,
    wait_duration_ms: float | None,
    browser_session_navs: int,
    timeout_ms: int,
    timeout_seconds: int,
    stage_timings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Lightweight timeout diagnostics always attached; histogram when flag is on."""
    duration_ms = float(wait_duration_ms) if wait_duration_ms is not None else None
    if duration_ms is None and isinstance(stage_timings, dict):
        durations = stage_timings.get("stageDurationsMs")
        if isinstance(durations, dict) and "wait_for_result_container" in durations:
            try:
                duration_ms = float(durations["wait_for_result_container"])
            except (TypeError, ValueError):
                duration_ms = None
    bucket = record_wait_for_result_container_duration(duration_ms) if duration_ms is not None else None
    payload: dict[str, Any] = {
        "lastUrl": page_url,
        "browserSessionNavs": int(browser_session_navs),
        "browserSessionRecycleEvents": browser_session_recycle_events(),
        "timeoutMs": int(timeout_ms),
        "timeoutSeconds": int(timeout_seconds),
        "waitForResultContainerDurationMs": duration_ms,
        "waitForResultContainerBucket": bucket,
        "timeoutInstrumentationEnabled": timeout_instrumentation_enabled(),
    }
    if timeout_instrumentation_enabled():
        payload["waitForResultContainerHistogram"] = snapshot_wait_for_result_container_histogram()
    return payload


def assert_final_url_matches_requested_marketplace(
    *,
    final_url: str,
    expected_provider_domain: str,
    requested_market_country: str,
    requested_currency: str,
) -> None:
    """Reject silent marketplace redirects (e.g. US request ending on ebay.com.au)."""
    if ebay_host_matches_provider_domain(
        final_url_or_host=final_url,
        provider_domain=expected_provider_domain,
    ):
        return
    parsed = urlparse(final_url)
    raise ProviderMarketplaceMismatchError(
        "eBay redirected away from the requested pricing marketplace; "
        "wrong-market results are not accepted",
        diagnostics={
            "providerOutcome": "marketplace_domain_mismatch",
            "requestedMarketCountry": requested_market_country,
            "requestedCurrency": requested_currency,
            "expectedProviderDomain": expected_provider_domain,
            "finalUrlHost": normalize_ebay_host(parsed.netloc),
            "finalUrlPath": parsed.path,
        },
    )


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


def _looks_like_html_document(text: str) -> bool:
    sample = str(text or "")[:4000].lower()
    if not sample.strip():
        return False
    if "<html" in sample or sample.lstrip().startswith("<!doctype"):
        return True
    # Dense markup with scripts/styles is HTML source, not visible body text.
    tag_hits = sample.count("<") + sample.count(">")
    return tag_hits >= 40 and ("<script" in sample or "<style" in sample or "<iframe" in sample)


def _strip_html_to_approx_visible_text(html: str) -> str:
    """Best-effort visible text from HTML for classification (not a full browser render)."""
    text = str(html or "")
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", text)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _passive_challenge_resources(html_or_source: str) -> list[str]:
    source = str(html_or_source or "")
    if not source:
        return []
    found: list[str] = []
    for pattern in PASSIVE_CHALLENGE_RESOURCE_RES:
        if pattern.search(source):
            found.append(pattern.pattern)
    if PASSIVE_HIDDEN_RECAPTCHA_IFRAME_RE.search(source):
        # Prefer classifying zero-size/hidden recaptcha iframes as passive.
        for match in PASSIVE_HIDDEN_RECAPTCHA_IFRAME_RE.finditer(source):
            tag = match.group(0).lower()
            hidden = (
                "display: none" in tag
                or "display:none" in tag
                or "visibility: hidden" in tag
                or "visibility:hidden" in tag
                or 'width="0"' in tag
                or "width='0'" in tag
                or 'height="0"' in tag
                or "height='0'" in tag
            )
            found.append("hidden_recaptcha_iframe" if hidden else "recaptcha_iframe")
            break
    # Bare "captcha" in CSS class / framework markup is passive when not visible copy.
    if re.search(r"\bcaptcha\b", source, flags=re.I) and "captcha_token_in_source" not in found:
        found.append("captcha_token_in_source")
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for item in found:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _active_challenge_url(url: str) -> str | None:
    blob = str(url or "").lower()
    if not blob:
        return None
    for marker in ACTIVE_CHALLENGE_URL_MARKERS:
        if marker in blob:
            return marker
    return None


def _challenge_ui_active(challenge_ui: dict[str, Any] | None) -> tuple[bool, str | None]:
    if not isinstance(challenge_ui, dict):
        return False, None
    if challenge_ui.get("visibleChallengeText") is True:
        return True, "visible_challenge_text"
    try:
        if int(challenge_ui.get("visibleCaptchaFrameCount") or 0) > 0:
            return True, "visible_captcha_iframe"
    except (TypeError, ValueError):
        pass
    if challenge_ui.get("visibleChallengeWidget") is True:
        return True, "visible_challenge_widget"
    return False, None


def contains_block_marker(*, title: str = "", body_text: str = "") -> bool:
    """True only for active/user-facing block or challenge copy — not passive captcha resources."""
    visible = body_text
    html_source = ""
    if _looks_like_html_document(body_text):
        html_source = body_text
        visible = _strip_html_to_approx_visible_text(body_text)
    haystack = f"{title}\n{visible}".lower()
    if any(marker in haystack for marker in BLOCK_TEXT_MARKERS):
        return True
    # Passiveive captcha resources in HTML must not count as a block by themselves.
    _ = html_source
    return False


def classify_browser_page_state(
    *,
    title: str = "",
    body_text: str = "",
    html_document: str | None = None,
    url: str = "",
    selector_counts: dict[str, int] | None = None,
    challenge_ui: dict[str, Any] | None = None,
    x11_sold_state_verified: bool | None = None,
) -> dict[str, Any]:
    """Classify page state with ACTIVE vs PASSIVE challenge awareness.

    Representation-aware:
    - visible body text / titles drive ACTIVE challenge detection
    - raw HTML captcha/recaptcha resources are PASSIVE diagnostics unless a
      visible challenge widget/text/URL proves an active challenge
    - strong ordinary Sold/listing evidence + only passive resources => success
    - genuinely conflicting active+ordinary evidence => ambiguous_security_state
    """
    selectors = selector_counts or {}
    result_count = sum(int(value or 0) for value in selectors.values())
    html_source = str(html_document or "")
    visible_text = str(body_text or "")
    if html_source and not _looks_like_html_document(visible_text):
        # Explicit split: body_text is visible, html_document is source.
        pass
    elif _looks_like_html_document(visible_text):
        html_source = html_source or visible_text
        visible_text = _strip_html_to_approx_visible_text(visible_text)
    elif not visible_text and html_source:
        visible_text = _strip_html_to_approx_visible_text(html_source)

    visible_haystack = f"{title}\n{visible_text}".lower()
    matched_visible = lambda markers: next((marker for marker in markers if marker in visible_haystack), None)

    passive_resources = _passive_challenge_resources(html_source or (body_text if _looks_like_html_document(body_text) else ""))
    # Bare captcha token in visible text alone is not enough; require active phrases/URL/UI.
    url_marker = _active_challenge_url(url)
    ui_active, ui_reason = _challenge_ui_active(challenge_ui)
    active_text_marker = matched_visible(ACTIVE_CHALLENGE_VISIBLE_TEXT_MARKERS)

    ordinary_markers = matched_visible(RESULT_TEXT_MARKERS)
    url_l = str(url or "").lower()
    ordinary_url = ("/sch/" in url_l or "/itm/" in url_l) and "ebay." in url_l
    sold_url = "lh_sold=1" in url_l
    strong_listing_evidence = (
        result_count > 0
        or bool(ordinary_markers)
        or (sold_url and ordinary_url)
        or bool(x11_sold_state_verified)
        or int(selectors.get("canonical_itm_href_count") or 0) > 0
    )

    active_reasons: list[str] = []
    if url_marker:
        active_reasons.append(f"url:{url_marker}")
    if active_text_marker:
        active_reasons.append(f"visible_text:{active_text_marker}")
    if ui_active and ui_reason:
        active_reasons.append(f"challenge_ui:{ui_reason}")

    base_diag = {
        "classificationModel": "active_vs_passive_challenge_v1",
        "passiveChallengeResources": passive_resources,
        "activeChallengeEvidence": active_reasons,
        "ordinaryResultsEvidence": {
            "resultSelectorCount": result_count,
            "resultTextMarker": ordinary_markers,
            "soldUrl": sold_url,
            "ordinaryEbayUrl": ordinary_url,
            "x11SoldStateVerified": x11_sold_state_verified,
            "canonicalItmHrefCount": int(selectors.get("canonical_itm_href_count") or 0),
        },
        "representation": {
            "usedHtmlDocument": bool(html_source),
            "visibleTextChars": len(visible_text),
            "htmlChars": len(html_source),
        },
    }

    # Auth wall is not CAPTCHA. Classify before active-challenge URL markers.
    if is_ebay_authentication_url(url):
        return {
            "outcome": "authentication_required",
            "reason": "signin.ebay",
            "retryable": False,
            "securityClass": "EBAY_AUTH_REQUIRED",
            **base_diag,
        }

    # SORRY / maintenance / auth / access — evaluate on visible text (and title), not CSS.
    if marker := matched_visible(ACCESS_BLOCK_TEXT_MARKERS):
        return {"outcome": "access_blocked", "reason": marker, "retryable": True, **base_diag}
    if marker := matched_visible(AUTH_TEXT_MARKERS):
        return {"outcome": "authentication_required", "reason": marker, "retryable": False, **base_diag}
    if marker := matched_visible(MAINTENANCE_TEXT_MARKERS):
        return {"outcome": "provider_unavailable", "reason": marker, "retryable": True, **base_diag}
    if marker := matched_visible(SORRY_ERROR_TEXT_MARKERS) or (
        "error page" in visible_haystack and "something went wrong" in visible_haystack
    ):
        return {"outcome": "provider_unavailable", "reason": "ebay_sorry_error_page", "retryable": True, **base_diag}

    # Active challenge vs ordinary results / ambiguity.
    if active_reasons and strong_listing_evidence and not (url_marker or ui_active):
        # Visible challenge phrases with simultaneous strong Sold/listing evidence —
        # fail closed rather than silently price.
        return {
            "outcome": "ambiguous_security_state",
            "reason": "active_text_with_ordinary_listing_evidence",
            "retryable": True,
            "securityClass": "AMBIGUOUS_SECURITY_STATE",
            **base_diag,
        }
    if active_reasons:
        return {
            "outcome": "challenge_detected",
            "reason": active_reasons[0],
            "retryable": True,
            "securityClass": "ACTIVE_CHALLENGE",
            **base_diag,
        }

    if result_count <= 0 and (marker := matched_visible(CONSENT_TEXT_MARKERS)):
        return {"outcome": "provider_unavailable", "reason": f"interstitial:{marker}", "retryable": True, **base_diag}
    if marker := matched_visible(NO_RESULTS_TEXT_MARKERS):
        return {"outcome": "no_results", "reason": marker, "retryable": False, **base_diag}
    if strong_listing_evidence or ordinary_markers:
        return {
            "outcome": "success",
            "reason": "results_page",
            "retryable": False,
            "securityClass": "ORDINARY_RESULTS",
            "passiveChallengeResourcesOnly": bool(passive_resources),
            **base_diag,
        }
    # Passiveive captcha resources alone on an otherwise unknown page are not an active challenge.
    if passive_resources and not active_reasons:
        return {
            "outcome": "parsing_failure",
            "reason": "unknown_page_state_with_passive_challenge_resources",
            "retryable": True,
            "securityClass": "PASSIVE_CHALLENGE_RESOURCE",
            **base_diag,
        }
    return {"outcome": "parsing_failure", "reason": "unknown_page_state", "retryable": True, **base_diag}


def _ebay_https_origin(url_or_host: str, *, fallback: str = "https://www.ebay.com.au") -> str:
    """Build https origin without doubling www (hostname may already include www)."""
    raw = str(url_or_host or "").strip()
    if not raw:
        return fallback
    host = raw
    if "://" in raw:
        host = (urlparse(raw).hostname or "").lower()
    else:
        host = raw.lower().split("/")[0]
    if not host:
        return fallback
    if host.startswith("www."):
        return f"https://{host}"
    return f"https://www.{host}"


def _url_has_sold_completed_filters(url: str) -> bool:
    lower = str(url or "").lower()
    return "lh_sold=1" in lower and "lh_complete=1" in lower


def _click_first_visible(page: Any, selectors: tuple[str, ...], *, timeout_ms: int = 4000) -> str | None:
    """Click the first matching visible locator. Returns the selector used, or None."""
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            continue
        if count <= 0:
            continue
        try:
            target = locator.first
            target.scroll_into_view_if_needed(timeout=min(2000, timeout_ms))
            # Brief human-like settle reduces intermittent AU SORRY after Sold/Completed.
            try:
                target.hover(timeout=min(2000, timeout_ms))
            except Exception:
                pass
            time.sleep(0.35)
            target.click(timeout=timeout_ms, delay=80)
            return selector
        except Exception:
            continue
    return None


def verify_sold_result_state(
    *,
    url: str,
    title: str,
    body_text: str,
) -> dict[str, Any]:
    """Independently verify the page is a sold-results state (not active listings).

    Sold-only navigation is acceptable when URL/state and result-level sold evidence
    agree. Active listings must never be accepted as sold comps.
    """
    url_l = str(url or "").lower()
    title_l = str(title or "").lower()
    body = str(body_text or "")
    body_l = body.lower()
    sold_url = "lh_sold=1" in url_l
    complete_url = "lh_complete=1" in url_l
    sold_date_lines = sum(
        1
        for line in body.splitlines()
        if line.strip().lower().startswith("sold ")
        and not line.strip().lower().startswith("sold items")
        and not line.strip().lower().startswith("sold listings")
    )
    sold_listings_label = "sold listings" in body_l or "sold items" in body_l
    # Result-level sold evidence: dated sold lines are the strongest page signal.
    result_level_sold = sold_date_lines >= 1
    verified = bool(sold_url and (result_level_sold or (sold_listings_label and complete_url)))
    return {
        "SOLD_STATE_VERIFIED": verified,
        "soldUrlParam": sold_url,
        "completedUrlParam": complete_url,
        "soldDateLines": sold_date_lines,
        "soldListingsLabel": sold_listings_label,
        "resultLevelSoldEvidence": result_level_sold,
        "titleHasSold": "sold" in title_l,
        "activeListingContaminationPossible": bool(not verified and ("buy it now" in body_l or "add to cart" in body_l)),
    }


def apply_sold_completed_filters_via_ui(page: Any, *, timeout_ms: int = 45000) -> dict[str, Any]:
    """Apply Sold items + Completed items via the refine UI (not a sold deep-link goto).

    ebay.com.au rejects direct ``LH_Sold``/``LH_Complete`` deep links with a SORRY page,
    while the same filters succeed when clicked from an active search results page.
    """
    started = page.url
    diagnostics: dict[str, Any] = {
        "soldFilterMode": "ui_after_active_search",
        "urlBeforeFilters": started,
        "soldSelectorUsed": None,
        "completedSelectorUsed": None,
        "SOLD_STATE_VERIFIED": False,
    }
    if _url_has_sold_completed_filters(started):
        diagnostics["urlAfterFilters"] = started
        diagnostics["alreadyApplied"] = True
        sold_state = verify_sold_result_state(
            url=started,
            title=_safe_page_title(page),
            body_text=_safe_body_text(page),
        )
        diagnostics.update(sold_state)
        if not sold_state["SOLD_STATE_VERIFIED"]:
            raise ProviderParseError(
                "eBay sold URL present but sold-result state could not be verified",
                diagnostics=diagnostics,
            )
        return diagnostics

    # Sold items first (may navigate). Prefer in-page refine links over any deep-link goto.
    # Longer settle after clicks: AU intermittently returns SORRY when the next check races
    # mid-navigation (headed UI path is otherwise proven).
    if "lh_sold=1" not in started.lower():
        # Wait for active search readiness, then re-query Sold. Clicking Sold before the
        # results/filter chrome finishes settling is a primary AU SORRY/chrome-error trigger.
        ready_deadline = time.time() + min(20.0, max(6.0, timeout_ms / 1000.0))
        diagnostics["searchReadySignals"] = []
        while time.time() < ready_deadline:
            body_probe = (_safe_body_text(page) or "").lower()
            title_probe = (_safe_page_title(page) or "").lower()
            state_probe = classify_browser_page_state(title=title_probe, body_text=body_probe)
            if state_probe.get("reason") == "ebay_sorry_error_page" or str(page.url or "").lower().startswith(
                "chrome-error:"
            ):
                raise ProviderTemporaryError(
                    "eBay PRE_SOLD_SORRY: active search page unavailable before Sold items filter click",
                    diagnostics={
                        **diagnostics,
                        "preSoldSorry": "PRE_SOLD_SORRY",
                        "reason": "ebay_sorry_error_page",
                        "browserPageState": state_probe,
                        "urlBeforeFilters": page.url,
                    },
                )
            sold_visible = False
            try:
                sold_loc = page.locator(
                    'a.su-selection-group__link:has-text("Sold items"), a[href*="LH_Sold=1"]'
                )
                sold_visible = sold_loc.count() > 0 and sold_loc.first.is_visible()
            except Exception:
                sold_visible = False
            results_signal = (
                "results for" in body_probe
                or "shop by category" in body_probe
                or "filter" in body_probe
            )
            if sold_visible and results_signal:
                diagnostics["searchReadySignals"] = ["sold_control_visible", "results_copy_present"]
                break
            time.sleep(0.4)
        else:
            diagnostics["searchReadySignals"] = ["timeout_waiting_for_active_search_ready"]
        # Re-read URL after settle; do not click a locator captured before navigation finished.
        started = page.url
        diagnostics["urlBeforeFilters"] = started
        time.sleep(0.8)
        sold_sel = _click_first_visible(page, SOLD_ITEMS_FILTER_SELECTORS, timeout_ms=min(15000, timeout_ms))
        diagnostics["soldSelectorUsed"] = sold_sel
        if sold_sel is None:
            raise ProviderTemporaryError(
                "Could not find eBay Sold items filter control on active search results",
                diagnostics=diagnostics,
            )
        try:
            page.wait_for_load_state("domcontentloaded", timeout=min(20000, timeout_ms))
        except Exception:
            pass
        time.sleep(4.0)
        try:
            page.wait_for_url(re.compile(r"(?i)lh_sold=1"), timeout=min(15000, timeout_ms))
        except Exception:
            pass

    after_sold = page.url
    diagnostics["urlAfterSold"] = after_sold
    # AU sold DOM can paint LH_Sold=1 before dated sold lines appear; wait briefly.
    settle_deadline = time.time() + min(12.0, max(4.0, timeout_ms / 1000.0))
    body_after_sold = _safe_body_text(page)
    title_after_sold = _safe_page_title(page)
    while time.time() < settle_deadline:
        if str(after_sold or "").lower().startswith("chrome-error:"):
            break
        probe = verify_sold_result_state(
            url=after_sold,
            title=title_after_sold,
            body_text=body_after_sold,
        )
        if probe["SOLD_STATE_VERIFIED"]:
            break
        time.sleep(1.0)
        after_sold = page.url
        diagnostics["urlAfterSold"] = after_sold
        body_after_sold = _safe_body_text(page)
        title_after_sold = _safe_page_title(page)
    state_after_sold = classify_browser_page_state(title=title_after_sold, body_text=body_after_sold)
    chrome_error_after_sold = str(after_sold or "").lower().startswith("chrome-error:")
    if chrome_error_after_sold or (
        state_after_sold.get("outcome") == "provider_unavailable"
        and state_after_sold.get("reason") == "ebay_sorry_error_page"
    ):
        # Recovery: homepage warm-up then re-open the active search, then re-click Sold.
        # Re-goto of the same active URL alone is often still SORRY on AU.
        # chrome-error:// also needs a full homepage restart, not a same-URL reload.
        diagnostics["soldFilterSorryRetry"] = True
        if chrome_error_after_sold:
            diagnostics["soldFilterChromeErrorRetry"] = True
        home_origin = _ebay_https_origin(started, fallback="https://www.ebay.com.au")
        diagnostics["soldFilterRecoveryHome"] = home_origin
        try:
            page.goto(f"{home_origin}/", wait_until="domcontentloaded", timeout=min(20000, timeout_ms))
            time.sleep(3.0)
            page.goto(started, wait_until="domcontentloaded", timeout=min(20000, timeout_ms))
            time.sleep(5.0)
            try:
                page.wait_for_selector(
                    'a.su-selection-group__link:has-text("Sold items"), a[href*="LH_Sold=1"]',
                    timeout=min(15000, timeout_ms),
                    state="visible",
                )
            except Exception:
                pass
            sold_sel_retry = _click_first_visible(
                page, SOLD_ITEMS_FILTER_SELECTORS, timeout_ms=min(15000, timeout_ms)
            )
            diagnostics["soldSelectorRetryUsed"] = sold_sel_retry
            try:
                page.wait_for_load_state("domcontentloaded", timeout=min(20000, timeout_ms))
            except Exception:
                pass
            time.sleep(4.0)
        except Exception:
            pass
        after_sold = page.url
        diagnostics["urlAfterSold"] = after_sold
        body_after_sold = _safe_body_text(page)
        title_after_sold = _safe_page_title(page)
        state_after_sold = classify_browser_page_state(title=title_after_sold, body_text=body_after_sold)
        if str(after_sold or "").lower().startswith("chrome-error:") or (
            state_after_sold.get("outcome") == "provider_unavailable"
            and state_after_sold.get("reason") == "ebay_sorry_error_page"
        ):
            raise ProviderTemporaryError(
                "eBay returned SORRY error page after Sold items filter click",
                diagnostics={**diagnostics, "browserPageState": state_after_sold},
            )
        settle_deadline = time.time() + min(12.0, max(4.0, timeout_ms / 1000.0))
        while time.time() < settle_deadline:
            if str(after_sold or "").lower().startswith("chrome-error:"):
                break
            probe = verify_sold_result_state(
                url=after_sold,
                title=title_after_sold,
                body_text=body_after_sold,
            )
            if probe["SOLD_STATE_VERIFIED"]:
                break
            time.sleep(1.0)
            after_sold = page.url
            diagnostics["urlAfterSold"] = after_sold
            body_after_sold = _safe_body_text(page)
            title_after_sold = _safe_page_title(page)
        state_after_sold = classify_browser_page_state(title=title_after_sold, body_text=body_after_sold)
        if str(after_sold or "").lower().startswith("chrome-error:"):
            raise ProviderTemporaryError(
                "eBay browser navigated to chrome-error after Sold items filter click",
                diagnostics={**diagnostics, "browserPageState": state_after_sold},
            )

    if str(after_sold or "").lower().startswith("chrome-error:"):
        raise ProviderTemporaryError(
            "eBay browser navigated to chrome-error after Sold items filter click",
            diagnostics={**diagnostics, "browserPageState": state_after_sold},
        )

    # Prefer Sold-only when already independently verified. Completed is optional and
    # on AU can clear sold evidence or hit SORRY even after a good Sold page.
    sold_state_pre_completed = verify_sold_result_state(
        url=after_sold,
        title=title_after_sold,
        body_text=body_after_sold,
    )
    if sold_state_pre_completed["SOLD_STATE_VERIFIED"] and "lh_complete=1" not in after_sold.lower():
        diagnostics.update(sold_state_pre_completed)
        diagnostics["completedFilterSkipped"] = "sold_state_verified_completed_optional"
        diagnostics["urlAfterFilters"] = after_sold
        diagnostics["browserPageState"] = state_after_sold
        diagnostics["soldEvidenceWithoutBothUrlParams"] = True
        return diagnostics

    if "lh_complete=1" not in after_sold.lower():
        url_before_completed = after_sold
        completed_sel = _click_first_visible(
            page, COMPLETED_ITEMS_FILTER_SELECTORS, timeout_ms=min(12000, timeout_ms)
        )
        diagnostics["completedSelectorUsed"] = completed_sel
        if completed_sel is None:
            # Completed is optional only when Sold-only state independently verifies.
            sold_state = verify_sold_result_state(
                url=after_sold,
                title=title_after_sold,
                body_text=body_after_sold,
            )
            diagnostics.update(sold_state)
            if sold_state["SOLD_STATE_VERIFIED"]:
                diagnostics["completedFilterSkipped"] = "control_missing_sold_state_verified"
                diagnostics["urlAfterFilters"] = after_sold
                diagnostics["browserPageState"] = state_after_sold
                diagnostics["soldEvidenceWithoutBothUrlParams"] = True
                return diagnostics
            raise ProviderTemporaryError(
                "Could not find eBay Completed items filter control after Sold items",
                diagnostics=diagnostics,
            )
        try:
            page.wait_for_load_state("domcontentloaded", timeout=min(20000, timeout_ms))
        except Exception:
            pass
        time.sleep(3.0)
        try:
            page.wait_for_url(re.compile(r"(?i)lh_complete=1"), timeout=min(15000, timeout_ms))
        except Exception:
            pass

        # Completed click can intermittently land on SORRY even when Sold succeeded.
        # Recover to the Sold URL and continue if sold evidence remains available.
        body_after_completed = _safe_body_text(page)
        title_after_completed = _safe_page_title(page)
        state_after_completed = classify_browser_page_state(
            title=title_after_completed, body_text=body_after_completed
        )
        if (
            state_after_completed.get("outcome") == "provider_unavailable"
            and state_after_completed.get("reason") == "ebay_sorry_error_page"
        ):
            diagnostics["completedFilterSorry"] = True
            try:
                page.goto(url_before_completed, wait_until="domcontentloaded", timeout=min(20000, timeout_ms))
                time.sleep(2.0)
            except Exception:
                pass
            body_recovered = _safe_body_text(page)
            title_recovered = _safe_page_title(page)
            state_recovered = classify_browser_page_state(title=title_recovered, body_text=body_recovered)
            sold_state = verify_sold_result_state(
                url=page.url,
                title=title_recovered,
                body_text=body_recovered,
            )
            diagnostics.update(sold_state)
            if state_recovered.get("reason") != "ebay_sorry_error_page" and sold_state["SOLD_STATE_VERIFIED"]:
                diagnostics["completedFilterSkipped"] = "sorry_after_completed_recovered_sold"
                diagnostics["urlAfterFilters"] = page.url
                diagnostics["browserPageState"] = state_recovered
                diagnostics["soldEvidenceWithoutBothUrlParams"] = True
                return diagnostics
            raise ProviderTemporaryError(
                "eBay returned SORRY error page after Sold/Completed filter UI flow",
                diagnostics={**diagnostics, "browserPageState": state_after_completed},
            )

    final_url = page.url
    diagnostics["urlAfterFilters"] = final_url
    body_final = _safe_body_text(page)
    title_final = _safe_page_title(page)
    state_final = classify_browser_page_state(title=title_final, body_text=body_final)
    diagnostics["browserPageState"] = state_final
    if state_final.get("outcome") == "provider_unavailable" and state_final.get("reason") == "ebay_sorry_error_page":
        raise ProviderTemporaryError(
            "eBay returned SORRY error page after Sold/Completed filter UI flow",
            diagnostics=diagnostics,
        )
    sold_state = verify_sold_result_state(url=final_url, title=title_final, body_text=body_final)
    diagnostics.update(sold_state)
    if not sold_state["SOLD_STATE_VERIFIED"]:
        raise ProviderParseError(
            "eBay Sold/Completed filters did not produce a verified sold-result state",
            diagnostics=diagnostics,
        )
    if not _url_has_sold_completed_filters(final_url):
        diagnostics["soldEvidenceWithoutBothUrlParams"] = True
    return diagnostics


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


def is_chrome_only_title(value: str) -> bool:
    """True when the extracted string is only a purchase/control label, not a listing title."""
    title = _normalise_text(value)
    if not title:
        return True
    lowered = title.lower()
    if lowered in CHROME_ONLY_TITLES:
        return True
    if any(marker in lowered for marker in PROMO_TITLE_MARKERS):
        return True
    if any(marker == lowered or lowered.startswith(f"{marker} ") for marker in GENERIC_TITLE_MARKERS):
        return True
    # Very short all-control tokens ("or", "bid", "new").
    if len(lowered) <= 3 and not any(ch.isdigit() for ch in lowered):
        return True
    return False


def clean_candidate_title(value: str) -> str:
    title = _normalise_text(value)
    if not title:
        return ""
    # Strip leading "New listing" badge text that sometimes prefixes real titles.
    title = re.sub(r"^(?:new\s+listing)\s*", "", title, flags=re.IGNORECASE).strip()
    # Product-rating prefixes sometimes leave a leading dash before the real title.
    title = re.sub(r"^[\-\u2013\u2014]+\s*", "", title).strip()
    boundary = TITLE_UI_BOUNDARY_RE.search(title)
    if boundary:
        title = title[: boundary.start()].strip()
    if not title:
        return ""
    if is_chrome_only_title(title):
        return ""
    lowered = title.lower()
    if any(marker in lowered for marker in PROMO_TITLE_MARKERS):
        return ""
    if any(marker in lowered for marker in GENERIC_TITLE_MARKERS) and len(title.split()) <= 4:
        # Short strings that merely contain a chrome marker are not titles.
        return ""
    return title


def _line_looks_like_listing_title(line: str) -> bool:
    text = _normalise_text(line)
    if not text or is_chrome_only_title(text):
        return False
    lowered = text.lower()
    if SOLD_DATE_LINE_RE.match(text):
        return False
    # Reject pure shipping/seller lines, but keep titles that merely mention postage.
    if re.match(r"^(?:delivery|shipping|postage|from|seller)\b", lowered):
        return False
    if any(token in lowered for token in ("free shipping", "free postage", "free delivery")) and len(text) < 40:
        return False
    # Prefer substantive titles: length, collector form, or card-ish tokens.
    if len(text) >= 18:
        return True
    if re.search(r"\b\d+\s*/\s*\d+\b", text) or re.search(r"#\s*\d+\b", text):
        return True
    if any(token in lowered for token in ("pokemon", "pokémon", "base set", "tcg", "holo")):
        return True
    return False


def extract_title_from_lines(lines: list[str], *, href_text: str = "", expected_currency: str = "AUD") -> str:
    """Pick a canonical listing title; never return purchase-control chrome."""
    for candidate in (href_text,):
        cleaned = clean_candidate_title(candidate)
        if cleaned and _line_looks_like_listing_title(cleaned):
            return cleaned
        if cleaned and len(cleaned) >= 12 and not is_chrome_only_title(cleaned):
            return cleaned

    scored: list[tuple[int, str]] = []
    for line in lines:
        lowered = line.lower()
        candidate_line = line
        if SOLD_DATE_LINE_RE.match(candidate_line):
            candidate_line = SOLD_DATE_LINE_RE.sub("", candidate_line).strip()
        elif lowered.startswith("sold "):
            candidate_line = re.sub(
                r"^sold\s+(?:[0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4}|[A-Za-z]{3,9}\s+[0-9]{1,2},\s+[0-9]{4})\s*",
                "",
                candidate_line,
                flags=re.IGNORECASE,
            )
        elif any(token in lowered for token in ("delivery", "shipping", "postage")):
            continue
        if is_chrome_only_title(candidate_line):
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
        title = clean_candidate_title(candidate_line)
        if not title:
            continue
        if not _line_looks_like_listing_title(title):
            continue
        score = len(title)
        if re.search(r"\b\d+\s*/\s*\d+\b", title):
            score += 40
        if "pokemon" in title.lower() or "pokémon" in title.lower():
            score += 20
        scored.append((score, title))
    if not scored:
        return ""
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


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
    structured_title = clean_candidate_title(str(candidate.get("title") or ""))
    href_title = clean_candidate_title(str(candidate.get("anchorText") or ""))
    # Prefer dedicated listing-title node, then safe aria/title attrs, then
    # scored line extraction. Never prefer whole-card chrome first.
    title = ""
    title_source = "missing"
    if structured_title and not is_chrome_only_title(structured_title):
        title = structured_title
        title_source = str(candidate.get("titleSource") or "s-item__title")
    elif href_title and not is_chrome_only_title(href_title) and len(href_title) >= 12:
        title = href_title
        title_source = "anchor-attr"
    else:
        title = extract_title_from_lines(
            lines,
            href_text="",
            expected_currency=search_query.currency,
        )
        title_source = "scored-lines" if title else "missing"
    if not title or is_chrome_only_title(title):
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
                "titleSource": title_source,
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
    reuse_context: bool = False
    recycle_after_navigations: int = 20

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
            reuse_context=_parse_bool("EBAY_BROWSER_REUSE_CONTEXT", False),
            recycle_after_navigations=_parse_positive_int("EBAY_BROWSER_RECYCLE_AFTER_NAVIGATIONS", 20),
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
                "reuseContext": self.reuse_context,
                "recycleAfterNavigations": self.recycle_after_navigations,
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
        extra = None
        if exc is not None:
            extra = {
                "errorType": type(exc).__name__,
                "errorMessage": str(exc)[:2000],
            }
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


def _safe_page_url(page: Any) -> str | None:
    try:
        url = getattr(page, "url", None)
        text = str(url or "").strip()
        return text or None
    except Exception:
        return None


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
      const chromeExact = new Set([
        'buy it now', 'best offer', 'best offer accepted', 'or', 'add to cart',
        'shop now', 'watch', 'watching', 'bids', '1 bid', '2 bids', '3 bids',
        'sponsored', 'pre-owned', 'pre owned', 'brand new', 'new listing',
        'see all', 'more options', 'make offer', 'shop on ebay'
      ]);
      const isChrome = (value) => {
        const t = norm(value).toLowerCase();
        if (!t) return true;
        if (chromeExact.has(t)) return true;
        if (t.length <= 3 && !/\\d/.test(t)) return true;
        return false;
      };
      const textOf = (root, selectors) => {
        for (const selector of selectors) {
          const node = root.querySelector(selector);
          if (!node) continue;
          // Prefer heading text; avoid nested CTA buttons inside the title node.
          const heading = node.querySelector('[role="heading"]') || node;
          let text = norm(heading.innerText || heading.textContent);
          // Drop "New listing" badge prefixes.
          text = text.replace(/^(?:new listing)\\s+/i, '').trim();
          if (text && !isChrome(text)) return text;
        }
        return '';
      };
      const hrefOf = (root) => {
        const link = root.matches && root.matches('a[href*="/itm/"]') ? root : root.querySelector('a[href*="/itm/"]');
        if (!link) return { href: '', anchorText: '', titleSource: '' };
        // Prefer title/aria attributes over innerText — US sold cards often put
        // "Buy It Now" / "or" / "Best Offer" into the link's innerText.
        const aria = norm(link.getAttribute('aria-label') || '');
        const titleAttr = norm(link.getAttribute('title') || '');
        let anchorText = '';
        let titleSource = '';
        for (const [candidate, source] of [[aria, 'aria-label'], [titleAttr, 'title-attr']]) {
          if (candidate && !isChrome(candidate) && candidate.length >= 12) {
            anchorText = candidate;
            titleSource = source;
            break;
          }
        }
        return { href: link.href || '', anchorText, titleSource };
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
        const structuredTitle = textOf(node, [
          'h3.s-item__title [role="heading"]',
          'h3.s-item__title',
          '.s-item__title [role="heading"]',
          '.s-item__title span[role="heading"]',
          '.s-item__title span',
          '.s-item__title',
          '[class*="s-card__title"] [role="heading"]',
          '[class*="s-card__title"]',
          'div[role="heading"]',
          'h3[role="heading"]',
          '.su-card-container__header [role="heading"]',
        ]);
        const text = blockText(node.innerText);
        out.push({
          source,
          href: link.href,
          anchorText: link.anchorText,
          title: structuredTitle,
          titleSource: structuredTitle ? 's-item__title' : (link.titleSource || ''),
          priceText: textOf(node, ['.s-item__price', '.s-item__detail--primary']),
          shippingText: textOf(node, ['.s-item__shipping', '.s-item__logisticsCost']),
          soldDateText: textOf(node, ['.s-item__title--tagblock .POSITIVE', '.s-item__caption--row', '.s-item__caption']),
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
        self._pw: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._session_navs = 0
        self._session_locale: str | None = None
        self._desktop_cdp_browser: Any | None = None
        self._desktop_nav_ready: bool = False

    def _wait_for_request_slot(self) -> None:
        min_wait = max(self.config.cooldown_seconds, self.config.min_seconds_between_requests)
        with self._request_lock:
            now = time.monotonic()
            elapsed = now - self.__class__._last_request_monotonic
            if elapsed < min_wait:
                time.sleep(min_wait - elapsed)
            self.__class__._last_request_monotonic = time.monotonic()

    def _close_browser_session(self) -> None:
        page = self._page
        context = self._context
        pw = self._pw
        cdp_browser = self._desktop_cdp_browser
        self._page = None
        self._context = None
        self._pw = None
        self._desktop_cdp_browser = None
        self._desktop_nav_ready = False
        self._session_navs = 0
        self._session_locale = None
        # For CDP-attached Chrome: disconnect only. Never close contexts/pages that
        # belong to the externally owned desktop browser.
        if cdp_browser is not None:
            try:
                cdp_browser.close()  # disconnect from CDP; does not exit Chrome
            except Exception:
                pass
        elif context is not None:
            try:
                context.close()
            except Exception:
                pass
        elif page is not None:
            try:
                page.close()
            except Exception:
                pass
        if pw is not None and cdp_browser is None:
            try:
                pw.stop()
            except Exception:
                pass
        elif pw is not None and cdp_browser is not None:
            # Keep sync_playwright alive across desktop jobs when possible; only stop
            # if we are fully tearing down (caller cleared browser already).
            try:
                pw.stop()
            except Exception:
                pass

    def _ensure_browser_session(self, *, request: ProviderRequest) -> tuple[Any, Any]:
        """Return (context, page), launching or recycling when configured."""
        from playwright.sync_api import sync_playwright

        needs_recycle = (
            self._context is None
            or self._page is None
            or self._pw is None
            or self._session_navs >= max(1, int(self.config.recycle_after_navigations))
            or (self._session_locale and self._session_locale != request.search_locale)
        )
        if needs_recycle and self._pw is not None:
            note_browser_session_recycle()
            self._close_browser_session()
        if self._context is not None and self._page is not None:
            return self._context, self._page

        launch_timeout_ms = self.config.launch_timeout_seconds * 1000
        timeout_ms = self.config.timeout_seconds * 1000
        profile_dir = self.config.ensure_profile_dir()
        pw = sync_playwright().start()
        try:
            context = pw.chromium.launch_persistent_context(
                str(profile_dir),
                channel=self.config.channel,
                headless=self.config.headless,
                locale=request.search_locale,
                viewport={"width": 1366, "height": 900},
                timeout=launch_timeout_ms,
            )
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
        except Exception:
            try:
                pw.stop()
            except Exception:
                pass
            raise
        self._pw = pw
        self._context = context
        self._page = page
        self._session_navs = 0
        self._session_locale = request.search_locale
        return context, page

    def fetch_comps(self, request: ProviderRequest) -> ProviderResult:
        with self._lookup_lock:
            try:
                return self._fetch_comps_serial(request)
            except Exception:
                # Poisoned/challenge sessions must not leak into later jobs.
                if self.config.reuse_context:
                    if self._desktop_nav_mode_enabled():
                        # Keep Chrome alive; only drop stale CDP handles.
                        self._page = None
                        self._context = None
                        try:
                            if self._desktop_cdp_browser is not None:
                                list(self._desktop_cdp_browser.contexts)
                        except Exception:
                            self._desktop_cdp_browser = None
                    else:
                        self._close_browser_session()
                raise

    def _fetch_comps_serial(self, request: ProviderRequest) -> ProviderResult:
        lookup_timings = StageTimings()
        route = (request.market_country.upper(), request.currency.upper())
        if route not in SUPPORTED_MARKET_ROUTES:
            raise ProviderUnsupportedMarketError(
                "eBay browser provider currently supports "
                "AU/AUD, US/USD, GB/GBP, CA/CAD, DE/EUR, FR/EUR, IT/EUR, ES/EUR only",
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
                            "sold_filter_mode": diagnostics.get("soldFilterMode"),
                        }
                    )
                    if diagnostics.get("preSoldSorry") == "PRE_SOLD_SORRY":
                        attempt_failure["preSoldSorry"] = "PRE_SOLD_SORRY"
                    failed_attempts.append(attempt_failure)
                    sold_filter_miss = "sold items filter control" in str(exc).lower()
                    page_state = diagnostics.get("browserPageState") or {}
                    pre_sold_sorry = (
                        diagnostics.get("preSoldSorry") == "PRE_SOLD_SORRY"
                        or "pre_sold_sorry" in str(exc).lower()
                    )
                    sorry_or_entry_block = (
                        pre_sold_sorry
                        or diagnostics.get("reason") == "ebay_sorry_error_page"
                        or page_state.get("reason") == "ebay_sorry_error_page"
                        or "sorry error page" in str(exc).lower()
                        or "active search page unavailable" in str(exc).lower()
                        or "chrome-error" in str(exc).lower()
                    )
                    if (
                        (
                            diagnostics.get("timedOutStage")
                            or sold_filter_miss
                            or sorry_or_entry_block
                        )
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
            ctx = load_navigation_runtime_context()
            loc = exception_diagnostics(exc)
            failure_class = classify_pre_submit_runtime_error(exc, runtime_mode=ctx.runtime_mode)
            stage = str(lookup_timings.fields.get("currentStage") or "run_query_attempt")
            raise ProviderTemporaryError(
                str(exc) or "eBay browser lookup failed temporarily",
                diagnostics={
                    **loc,
                    "failureStage": stage,
                    "failureClass": failure_class,
                    "navigationFailureClass": failure_class,
                    "runtimeMode": ctx.runtime_mode,
                    "attemptId": ctx.current_attempt_id or os.environ.get("CARDSCANR_LIVE_ATTEMPT_ID"),
                    "jobId": ctx.current_job_id or os.environ.get("CARDSCANR_JOB_ID"),
                    "priceKeyId": ctx.current_price_key_id or os.environ.get("CARDSCANR_PRICE_KEY_ID"),
                    "expectedPriorTargetId": (
                        ctx.expected_prior.target_id if ctx.expected_prior is not None else None
                    ),
                    "queryPrepared": True,
                    "searchSubmissionStarted": False,
                    "childProcessStarted": False,
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
        # Preserve compact operational phase metadata from the latest successful attempt.
        # Aggregate used to drop x11SoldStateVerified / postSoldCapturePhase / capture
        # artifact fields — that broke reliability harness truth.
        last_attempt_meta = attempts[-1][1].raw_metadata if isinstance(attempts[-1][1].raw_metadata, dict) else {}
        op_keys = (
            "navMode",
            "desktopNav",
            "soldState",
            "x11SoldStateVerified",
            "postSoldCapturePhase",
            "parsePhase",
            "postSoldCapture",
            "finalizeTerminal",
            "directSearchURL",
            "playwrightNavigation",
            "cdpUsedFor",
            "browserPageState",
            "searchUrl",
        )
        operational: dict[str, Any] = {k: last_attempt_meta[k] for k in op_keys if k in last_attempt_meta}
        # Compact current-job capture metadata (no HTML body).
        persisted = None
        stage_from_attempt = last_attempt_meta.get("stageTimings")
        if isinstance(stage_from_attempt, dict):
            persisted = stage_from_attempt.get("persistedCaptureArtifact")
            if "postSoldCapturePhase" not in operational and stage_from_attempt.get("postSoldCapturePhase"):
                operational["postSoldCapturePhase"] = stage_from_attempt.get("postSoldCapturePhase")
            if "parsePhase" not in operational and stage_from_attempt.get("parsePhase"):
                operational["parsePhase"] = stage_from_attempt.get("parsePhase")
            if "x11SoldStateVerified" not in operational and stage_from_attempt.get("x11SoldStateVerified"):
                operational["x11SoldStateVerified"] = stage_from_attempt.get("x11SoldStateVerified")
            if "desktopNav" not in operational and isinstance(stage_from_attempt.get("desktopNav"), dict):
                operational["desktopNav"] = stage_from_attempt.get("desktopNav")
        if isinstance(persisted, dict):
            operational["persistedCaptureArtifact"] = {
                k: persisted.get(k)
                for k in (
                    "htmlPath",
                    "sha256",
                    "bodyPath",
                    "bodySha256",
                    "targetId",
                    "jobId",
                    "attemptId",
                    "priceKeyId",
                    "fingerprint",
                    "captureOrigin",
                    "captureMethod",
                    "htmlByteSize",
                    "bodyLength",
                )
                if persisted.get(k) is not None
            }
            operational["currentJobCapture"] = dict(operational["persistedCaptureArtifact"])
        # Prefer attempt stageTimings nested under aggregate for forensic recovery.
        merged_stage = {
            **(stage_timings or {}),
            "aggregate": aggregate_timings.snapshot(),
        }
        if isinstance(stage_from_attempt, dict):
            merged_stage["lastAttempt"] = {
                k: stage_from_attempt.get(k)
                for k in (
                    "postSoldCapturePhase",
                    "parsePhase",
                    "x11SoldStateVerified",
                    "finalizeTerminal",
                    "persistedCaptureArtifact",
                    "desktopNav",
                    "postSoldCapture",
                )
                if stage_from_attempt.get(k) is not None
            }
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
                "stageTimings": merged_stage,
                "marketScope": self.config.market_scope,
                "qualitySummary": quality_summary,
                "attemptedQualitySummaryBeforeDedupe": attempted_quality_summary,
                "parserErrors": all_parser_errors[:50],
                **operational,
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

    def _desktop_nav_mode_enabled(self) -> bool:
        from .search_entry_contract import resolved_ebay_browser_nav_mode

        return resolved_ebay_browser_nav_mode() in {
            "desktop_win32",
            "desktop",
            "real_desktop",
            "linux_x11",
            "linux_gui",
        }

    def _linux_x11_nav_mode_enabled(self) -> bool:
        from .search_entry_contract import resolved_ebay_browser_nav_mode

        return resolved_ebay_browser_nav_mode() in {
            "linux_x11",
            "linux_gui",
        }

    def _ensure_linux_chrome_cdp_ready(self) -> int:
        """Ensure Linux :99 Chrome is up with CDP; do not attach Playwright yet."""
        from .linux_x11_ebay_nav import DEFAULT_CDP_PORT as _DEFAULT_CDP_PORT
        from .linux_x11_ebay_nav import ensure_chrome_with_cdp

        port = _parse_positive_int("EBAY_BROWSER_CDP_PORT", _DEFAULT_CDP_PORT) or _DEFAULT_CDP_PORT
        ctx = load_navigation_runtime_context()
        ensure_chrome_with_cdp(
            cdp_port=port,
            runtime_mode=ctx.runtime_mode,
            allow_existing_ebay_targets=ctx.is_inter_card(),
        )
        return int(port)

    def _connect_desktop_cdp_browser(self) -> Any:
        """Connect Playwright over CDP; do not select a page target yet."""
        from playwright.sync_api import sync_playwright

        if self._linux_x11_nav_mode_enabled():
            from .linux_x11_ebay_nav import DEFAULT_CDP_PORT as _DEFAULT_CDP_PORT
        else:
            from .desktop_win32_ebay_nav import DEFAULT_CDP_PORT as _DEFAULT_CDP_PORT

        port = _parse_positive_int("EBAY_BROWSER_CDP_PORT", _DEFAULT_CDP_PORT)
        connect_timeout_ms = max(5_000, int(self.config.launch_timeout_seconds * 1000))

        def _connect() -> Any:
            if self._linux_x11_nav_mode_enabled():
                from .linux_x11_ebay_nav import ensure_chrome_with_cdp

                ctx = load_navigation_runtime_context()
                ensure_chrome_with_cdp(
                    cdp_port=port or _DEFAULT_CDP_PORT,
                    runtime_mode=ctx.runtime_mode,
                    allow_existing_ebay_targets=ctx.is_inter_card(),
                )
            else:
                from .desktop_win32_ebay_nav import ensure_chrome_with_cdp

                ensure_chrome_with_cdp(
                    profile_dir=self.config.ensure_profile_dir(),
                    cdp_port=port or _DEFAULT_CDP_PORT,
                )
            if self._pw is None:
                self._pw = sync_playwright().start()
            self._desktop_cdp_browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port or _DEFAULT_CDP_PORT}",
                timeout=connect_timeout_ms,
            )
            self._desktop_nav_ready = True
            return self._desktop_cdp_browser

        browser = self._desktop_cdp_browser
        if browser is None:
            browser = _connect()
        try:
            _ = list(browser.contexts)
        except Exception:
            self._desktop_cdp_browser = None
            self._page = None
            self._context = None
            browser = _connect()
        return browser

    def _ensure_desktop_cdp_page(self) -> Any:
        """Attach read-only Playwright and return a page (legacy callers / Win32).

        Linux post-Sold capture must use capture_verified_sold_page binding instead of
        arbitrarily selecting ebay_pages[-1].
        """
        browser = self._connect_desktop_cdp_browser()
        contexts = list(browser.contexts)
        if not contexts:
            raise ProviderTemporaryError(
                "Desktop Chrome CDP attached but no browser contexts available",
                diagnostics={},
            )
        pages = list(contexts[0].pages)
        if not pages:
            raise ProviderTemporaryError(
                "Desktop Chrome CDP attached but no page targets available",
                diagnostics={},
            )
        # Prefer an LH_Sold page when present; otherwise last eBay page (legacy).
        sold_pages = [p for p in pages if "lh_sold=1" in (p.url or "").lower()]
        ebay_pages = [p for p in pages if "ebay." in (p.url or "").lower()]
        page = sold_pages[-1] if sold_pages else (ebay_pages[-1] if ebay_pages else pages[-1])
        try:
            page.set_default_timeout(min(self.config.timeout_seconds * 1000, 45_000))
        except Exception:
            pass
        self._page = page
        self._context = contexts[0]
        return page

    def _list_cdp_page_targets(self) -> list[dict[str, Any]]:
        browser = self._connect_desktop_cdp_browser()
        contexts = list(browser.contexts)
        if not contexts:
            return []
        pages = list(contexts[0].pages)
        self._context = contexts[0]
        out: list[dict[str, Any]] = []
        for idx, page in enumerate(pages):
            try:
                url = str(page.url or "")
            except Exception:
                url = ""
            try:
                title = str(page.title() or "")
            except Exception:
                title = ""
            out.append(
                {
                    "id": f"pw-{idx}",
                    "type": "page",
                    "url": url,
                    "title": title,
                    "_page": page,
                }
            )
        return out

    def _disconnect_desktop_cdp_client(self) -> None:
        try:
            browser = self._desktop_cdp_browser
            if browser is not None:
                browser.close()
        except Exception:
            pass
        self._desktop_cdp_browser = None
        self._page = None
        self._context = None

    def _fetch_with_desktop_win32(
        self,
        *,
        request: ProviderRequest,
        search_query: ProviderSearchQuery,
    ) -> ProviderResult:
        """Navigate with real Win32/Linux X11 mouse/keyboard; parse only via CDP attach."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        from ..finalize_deadline import FINALIZE_SUCCESS, finalize_timeout_seconds, run_with_finalize_deadline

        if self._linux_x11_nav_mode_enabled():
            from .linux_x11_ebay_nav import navigate_query_to_sold

            nav_mode_label = "linux_x11"
            nav_stage = "linux_x11_search_and_sold"
        else:
            from .desktop_win32_ebay_nav import navigate_query_to_sold

            nav_mode_label = "desktop_win32"
            nav_stage = "desktop_win32_search_and_sold"

        timeout_ms = self.config.timeout_seconds * 1000
        stage_timings = StageTimings()
        with _StageTimer(stage_timings, "desktop_ensure_chrome_cdp"):
            if self._linux_x11_nav_mode_enabled():
                # Do not hold a Playwright CDP session across long X11 GUI nav —
                # that raced the Ceruledge post-Sold hang (CDP attach before parse).
                port = self._ensure_linux_chrome_cdp_ready()
                stage_timings.fields["cdpPortReady"] = port
                stage_timings.fields["playwrightAttachedBeforeNav"] = False
            else:
                self._ensure_desktop_cdp_page()
                stage_timings.fields["playwrightAttachedBeforeNav"] = True

        with _StageTimer(stage_timings, nav_stage):
            from .search_entry_contract import (
                SEARCH_ENTRY_MODE_RENDERED_UI_X11,
                build_search_entry_evidence,
            )

            ctx = load_navigation_runtime_context()
            pre_submit = pre_submit_only_requested(flag=False)
            attempt_id = os.environ.get("CARDSCANR_LIVE_ATTEMPT_ID")
            # Production query entry: RENDERED_UI_X11 only (no manufactured /sch?_nkw=).
            stage_timings.fields["searchEntryMode"] = SEARCH_ENTRY_MODE_RENDERED_UI_X11
            stage_timings.fields["directSearchUrlNavigation"] = False
            stage_timings.fields["auSearchEntry"] = "rendered_ui_x11"
            nav = navigate_query_to_sold(
                search_query.query_text,
                reset_homepage=not ctx.is_inter_card(),
                attempt_id=attempt_id,
                price_key_id=str(getattr(request.price_key, "id", None) or "") or None,
                pre_submit_only=pre_submit,
            )
            nav_diag = nav.diagnostics if isinstance(nav.diagnostics, dict) else {}
            search_diag = nav_diag.get("search") if isinstance(nav_diag.get("search"), dict) else {}
            gui_timings = search_diag.get("guiAttemptTimings") or (nav_diag.get("guiAttemptTimings"))
            submission_event = search_diag.get("searchSubmissionStarted") or nav_diag.get(
                "searchSubmissionStarted"
            )
            stage_timings.fields["searchEntryEvidence"] = build_search_entry_evidence(
                search_input_located=bool(
                    search_diag.get("searchInputLocated")
                    or search_diag.get("ok")
                    or nav.search_success
                    or submission_event
                ),
                query_typed=bool(submission_event or nav.search_success or search_diag.get("queryTyped")),
                query_before_submit=str(search_query.query_text or "") or None,
                direct_search_url_navigation=False,
                search_submission_event_written=bool(submission_event),
                enter_pressed=bool(nav.search_success) and not pre_submit,
                ordinary_results_confirmed=bool(nav.search_success and not nav.sorry and not nav.challenge),
                attempt_id=attempt_id,
            )
            stage_timings.fields["desktopNav"] = {
                "ok": nav.ok,
                "searchSuccess": nav.search_success,
                "soldClickSuccess": nav.sold_click_success,
                "SOLD_STATE_VERIFIED": nav.sold_state_verified,
                "url": nav.url,
                "sorry": nav.sorry,
                "challenge": nav.challenge,
                "soldDateLines": nav.sold_date_lines,
                "error": nav.error,
                "navMode": nav_mode_label,
                "guiAttemptTimings": gui_timings,
                "preSubmit": (search_diag.get("diagnostics") or {}).get("preSubmit")
                if isinstance(search_diag.get("diagnostics"), dict)
                else search_diag.get("preSubmit"),
                "postNavigation": (search_diag.get("diagnostics") or {}).get("postNavigation")
                if isinstance(search_diag.get("diagnostics"), dict)
                else search_diag.get("postNavigation"),
            }
            if isinstance(gui_timings, dict):
                stage_timings.fields["guiAttemptTimings"] = gui_timings
        search_result_code = str(
            search_diag.get("resultCode") or nav_diag.get("resultCode") or nav.error or ""
        )
        if search_result_code == "PRE_SUBMIT_QUERY_READY" or nav_diag.get("preSubmitQueryReady"):
            return ProviderResult(
                provider_name=self.provider_name,
                marketplace=request.provider_marketplace_id,
                provider_fingerprint="pre_submit_only",
                query_used=search_query.query_text,
                comps=[],
                raw_metadata=sanitize_provider_diagnostics(
                    {
                        "resultCode": "PRE_SUBMIT_QUERY_READY",
                        "searchSubmissionStarted": False,
                        "runtimeMode": ctx.runtime_mode,
                        "navMode": nav_mode_label,
                        "desktopNav": stage_timings.fields.get("desktopNav"),
                        "stageTimings": stage_timings.snapshot(),
                    }
                ),
            )
        if is_ebay_authentication_url(nav.url or "") or str(nav.error or "") in {
            "EBAY_AUTH_REQUIRED",
            "AUTH_REQUIRED",
        }:
            raise ProviderAuthenticationRequiredError(
                "EBAY_AUTH_REQUIRED: eBay redirected pricing to sign-in; credentials are not entered automatically",
                diagnostics={
                    "providerOutcome": "authentication_required",
                    "ownedDailyOutcome": "EBAY_AUTH_REQUIRED",
                    "operationalStatus": "EBAY_AUTH_REQUIRED",
                    "failureClass": "EBAY_AUTH_REQUIRED",
                    "navMode": nav_mode_label,
                    "desktopNav": stage_timings.fields.get("desktopNav"),
                    "stageTimings": stage_timings.snapshot(),
                    "url": nav.url,
                    "markFresh": False,
                    "lastGoodRetained": True,
                },
            )
        if nav.challenge:
            raise ProviderBlockedError(
                "eBay returned a verification challenge during desktop navigation; captcha bypass is not attempted",
                diagnostics={
                    "providerOutcome": "challenge_detected",
                    "navMode": nav_mode_label,
                    "desktopNav": stage_timings.fields.get("desktopNav"),
                    "stageTimings": stage_timings.snapshot(),
                },
            )
        if nav.sorry or str(nav.error or "") == "TEMPORARY_EBAY_SERVER_FAILURE":
            raise ProviderTemporaryError(
                "TEMPORARY_EBAY_SERVER_FAILURE: eBay SORRY/error page during desktop navigation",
                diagnostics={
                    "reason": "ebay_sorry_error_page",
                    "ownedDailyOutcome": "TEMPORARY_EBAY_SERVER_FAILURE",
                    "preSoldSorry": "PRE_SOLD_SORRY" if not nav.sold_click_success else None,
                    "navMode": nav_mode_label,
                    "desktopNav": stage_timings.fields.get("desktopNav"),
                    "stageTimings": stage_timings.snapshot(),
                },
            )
        if str(nav.error or "") in {
            "ALTERNATE_EBAY_SURFACE",
            "EBAY_LIVE_RESULTS",
            "SOLD_UNAVAILABLE_ON_ALTERNATE_SURFACE",
            "LOCAL_SEARCH_SURFACE_STATE_LEAK",
            "LOCAL_SEARCH_SURFACE_RECOVERY_FAILED",
        } or "ebaylive/search" in str(nav.url or "").lower():
            owned = (
                "LOCAL_SEARCH_SURFACE_STATE_LEAK"
                if "LOCAL_SEARCH_SURFACE_STATE_LEAK" in str(nav.error or "")
                else (
                    "LOCAL_SEARCH_SURFACE_RECOVERY_FAILED"
                    if "LOCAL_SEARCH_SURFACE_RECOVERY_FAILED" in str(nav.error or "")
                    else "ALTERNATE_EBAY_SURFACE"
                )
            )
            reason = (
                "search_origin_ebay_live"
                if owned.startswith("LOCAL_SEARCH_SURFACE")
                else "ebay_live_results"
            )
            raise ProviderTemporaryError(
                f"{owned}: eBay Live/alternate search surface cannot provide Sold comps"
                if owned == "ALTERNATE_EBAY_SURFACE"
                else f"{owned}: search submitted or attempted from invalid eBay surface",
                diagnostics={
                    "reason": reason,
                    "ownedDailyOutcome": owned,
                    "routeClass": owned,
                    "url": nav.url,
                    "tripsSorryBreaker": False,
                    "markFresh": False,
                    "lastGoodRetained": True,
                    "navMode": nav_mode_label,
                    "desktopNav": stage_timings.fields.get("desktopNav"),
                    "stageTimings": stage_timings.snapshot(),
                },
            )
        if not nav.search_success or not nav.sold_click_success or not nav.sold_state_verified:
            raise ProviderTemporaryError(
                f"Desktop sold navigation failed: {nav.error or 'unknown'}",
                diagnostics={
                    "navMode": nav_mode_label,
                    "desktopNav": stage_timings.fields.get("desktopNav"),
                    "stageTimings": stage_timings.snapshot(),
                },
            )

        from .post_sold_capture import resolve_authoritative_capture_html_path
        from .post_sold_capture_process import (
            capture_deadline_seconds,
            capture_process_result_to_sold_page,
            post_sold_finalize_deadline_seconds,
            run_capture_worker_process,
        )

        # Capture is OS-killable (≤15s). Whole post-Sold finalize wraps parse/price.
        capture_budget = capture_deadline_seconds()
        finalize_budget = post_sold_finalize_deadline_seconds(
            default=max(30, int(capture_budget) + 15)
        )
        stage_timings.fields["captureProcessTimeoutSeconds"] = capture_budget
        stage_timings.fields["finalizeTimeoutSeconds"] = finalize_budget
        authoritative_html_path = resolve_authoritative_capture_html_path(
            job_id=os.environ.get("CARDSCANR_JOB_ID"),
            attempt_id=os.environ.get("CARDSCANR_LIVE_ATTEMPT_ID"),
            price_key_id=(
                os.environ.get("CARDSCANR_PRICE_KEY_ID")
                or str(getattr(request.price_key, "id", "") or "")
                or None
            ),
        )
        stage_timings.fields["authoritativeCaptureArtifactPath"] = str(authoritative_html_path)

        def _disconnect_cdp_for_deadline() -> None:
            # Legacy Playwright disconnect — process-boundary capture does not use it,
            # but keep for any residual in-process CDP handles.
            try:
                browser = self._desktop_cdp_browser
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            self._desktop_cdp_browser = None
            self._page = None
            self._context = None

        def _post_sold_parse() -> ProviderResult:
            from .post_sold_capture import (
                PARSE_COMPLETE,
                PARSE_FAILED,
                PARSE_PENDING,
                POST_SOLD_CAPTURE_FAILED,
                POST_SOLD_CAPTURE_PENDING,
                POST_SOLD_CAPTURE_READY,
            )

            # X11 already established Sold; local CDP capture is a separate phase.
            stage_timings.fields["x11SoldStateVerified"] = True
            stage_timings.fields["postSoldCapturePhase"] = POST_SOLD_CAPTURE_PENDING
            stage_timings.fields["parsePhase"] = PARSE_PENDING

            # Prefer existing X11 Sold clipboard body for diagnostics only (not production provenance).
            x11_body = ""
            x11_body_source = None
            sold_diag = nav_diag.get("sold") if isinstance(nav_diag.get("sold"), dict) else {}
            search_diag_nav = nav_diag.get("search") if isinstance(nav_diag.get("search"), dict) else {}
            tag = str((search_diag_nav.get("diagnostics") or {}).get("tag") or search_diag_nav.get("tag") or "")
            if not tag and isinstance(sold_diag.get("diagnostics"), dict):
                tag = str(sold_diag.get("diagnostics", {}).get("tag") or "")
            if not tag:
                tag = str(search_diag_nav.get("tag") or "")
            body_path = ROOT / "reports" / "artifacts" / f"linux_sold_{tag}_body.txt" if tag else None
            if body_path is not None and body_path.is_file():
                try:
                    x11_body = body_path.read_text(encoding="utf-8", errors="replace")
                    x11_body_source = f"x11_clipboard_file:{body_path.name}"
                except Exception:
                    x11_body = ""
            stage_timings.fields["x11SoldBodyChars"] = len(x11_body)
            stage_timings.fields["x11SoldBodySource"] = x11_body_source

            if self._linux_x11_nav_mode_enabled():
                from .linux_x11_ebay_nav import DEFAULT_CDP_PORT as _DEFAULT_CDP_PORT
            else:
                from .desktop_win32_ebay_nav import DEFAULT_CDP_PORT as _DEFAULT_CDP_PORT

            cdp_port = _parse_positive_int("EBAY_BROWSER_CDP_PORT", _DEFAULT_CDP_PORT) or _DEFAULT_CDP_PORT
            cdp_endpoint = f"http://127.0.0.1:{int(cdp_port)}"

            # HARD PROCESS BOUNDARY — parent never calls connect_over_cdp here.
            # Authoritative unique HTML path is chosen by the parent (not last_capture).
            proc_result = run_capture_worker_process(
                cdp_endpoint=cdp_endpoint,
                expected_url=nav.url,
                expected_query=search_query.query_text,
                expected_origin=str(search_query.provider_domain or "ebay.com.au"),
                deadline_seconds=capture_budget,
                max_results=self.config.max_results * 3,
                socket_timeout=min(4.0, max(2.0, capture_budget / 3.0)),
                artifact_path=str(authoritative_html_path),
            )
            capture = capture_process_result_to_sold_page(
                proc_result,
                x11_sold_state_verified=True,
            )
            # Keep X11 body length visible even when CDP process fails.
            if x11_body and not capture.diagnostics.get("x11SoldBodyChars"):
                capture.diagnostics["x11SoldBodyChars"] = len(x11_body)
                capture.diagnostics["x11SoldBodySource"] = x11_body_source

            probe_capture = capture.to_probe_dict()
            stage_timings.fields["postSoldCapture"] = probe_capture
            stage_timings.fields["postSoldCapturePhase"] = capture.capture_phase
            stage_timings.fields["postSoldCdpAttached"] = bool(capture.target_id)
            stage_timings.fields["soldStateAfterParseAttach"] = capture.sold_state
            stage_timings.fields["x11SoldStateVerified"] = True
            stage_timings.fields["captureProcess"] = (probe_capture.get("diagnostics") or {}).get("captureProcess")

            if not capture.success:
                stage_timings.fields["parsePhase"] = PARSE_FAILED
                fail_cls = str(capture.failure_class or "")
                # Marketplace Error/SORRY page present — not CDP_TARGET_NOT_FOUND / local capture bug.
                if fail_cls in {"MARKETPLACE_ERROR_PAGE", "TARGET_REJECTED_UNHEALTHY_PAGE", "EBAY_ERROR_PAGE"}:
                    health_cls = str(
                        (probe_capture.get("diagnostics") or {}).get("healthClassification")
                        or (probe_capture.get("diagnostics") or {}).get("marketplacePageClass")
                        or "EBAY_ERROR_PAGE"
                    )
                    raise ProviderTemporaryError(
                        f"TEMPORARY_EBAY_SERVER_FAILURE: marketplace {health_cls} after Sold filter "
                        f"(capture NOT_RUN; not CDP_TARGET_NOT_FOUND)",
                        diagnostics={
                            "navMode": nav_mode_label,
                            "reason": "ebay_error_page"
                            if "ERROR" in health_cls.upper()
                            else "ebay_sorry_error_page",
                            "ownedDailyOutcome": "TEMPORARY_EBAY_SERVER_FAILURE",
                            "terminal": health_cls,
                            "failureClass": fail_cls,
                            "failureDetail": capture.failure_detail,
                            "soldFilterStateVerified": True,
                            "soldPageHealthVerified": False,
                            "x11SoldStateVerified": False,
                            "SOLD_STATE_VERIFIED": False,
                            "marketplacePageClass": health_cls,
                            "expectedTargetFound": (probe_capture.get("diagnostics") or {}).get(
                                "expectedTargetFound"
                            ),
                            "capture": "NOT_RUN",
                            "parse": "NOT_RUN",
                            "write": "NOT_RUN",
                            "postSoldCapturePhase": POST_SOLD_CAPTURE_FAILED,
                            "postSoldCapture": probe_capture,
                            "url": capture.target_url or nav.url,
                            "markFresh": False,
                            "lastGoodRetained": True,
                            "tripsSorryBreaker": True,
                            "stageTimings": stage_timings.snapshot(),
                        },
                    )
                raise ProviderTemporaryError(
                    f"POST_SOLD_CAPTURE_FAILURE: local CDP capture failed after X11 SOLD_STATE_VERIFIED "
                    f"({capture.failure_class or 'unknown'})",
                    diagnostics={
                        "navMode": nav_mode_label,
                        "reason": "post_sold_capture_failed",
                        "ownedDailyOutcome": "POST_SOLD_CAPTURE_FAILURE",
                        "terminal": "POST_SOLD_CAPTURE_FAILURE",
                        "x11SoldStateVerified": True,
                        "SOLD_STATE_VERIFIED": True,
                        "postSoldCapturePhase": POST_SOLD_CAPTURE_FAILED,
                        "failureClass": capture.failure_class,
                        "failureDetail": capture.failure_detail,
                        "capture": probe_capture,
                        "postSoldCapture": probe_capture,
                        "url": capture.target_url or nav.url,
                        "markFresh": False,
                        "lastGoodRetained": True,
                        "tripsSorryBreaker": False,
                        "stageTimings": stage_timings.snapshot(),
                    },
                )

            stage_timings.fields["postSoldCapturePhase"] = POST_SOLD_CAPTURE_READY
            title = str(capture.target_title or "")
            # Keep visible body text separate from raw HTML for classification.
            # ONLY use HTML/body from the CURRENT successful capture payload.
            # Never fall back to global post_sold_capture_last/last_capture.html —
            # that file may belong to a prior job and must not be parsed as current evidence.
            visible_body = str((proc_result.payload or {}).get("body_text") or "")
            html_doc = str((proc_result.payload or {}).get("html") or "")
            if not visible_body:
                # Current-capture body only (from this process result), never disk last_capture.
                fallback = str(capture.html_or_text or "")
                visible_body = (
                    ""
                    if _looks_like_html_document(fallback)
                    else fallback
                )
            body_text = visible_body
            if html_doc and "/itm/" in html_doc:
                capture.html_or_text = html_doc
            sold_state = capture.sold_state or verify_sold_result_state(
                url=capture.target_url or nav.url,
                title=title,
                body_text=body_text or html_doc,
            )
            page_url = capture.target_url or nav.url

            # Persist real capture BEFORE parser execution (diagnostic evidence only).
            from .post_sold_capture import persist_sold_capture_artifact

            persist_info = persist_sold_capture_artifact(
                html=html_doc or body_text,
                body_text=str((proc_result.payload or {}).get("body_text") or body_text or ""),
                canonical_itm_href_count=int(
                    (capture.diagnostics or {}).get("canonicalItmHrefCount")
                    or (proc_result.payload or {}).get("canonical_itm_href_count")
                    or 0
                ),
                card_identity={
                    "priceKeyId": getattr(request.price_key, "id", None),
                    "cardName": getattr(request.price_key, "card_name", None),
                    "setName": getattr(request.price_key, "set_name", None),
                    "setCode": getattr(request.price_key, "set_code", None),
                    "collectorNumber": getattr(request.price_key, "collector_number", None),
                    "language": getattr(request.price_key, "language", None),
                },
                query=search_query.query_text,
                target_url=page_url,
                target_title=title,
                capture_method=capture.capture_method,
                market=search_query.market_country,
                extra_meta={
                    "targetId": capture.target_id,
                    "captureElapsedMs": capture.capture_elapsed_ms,
                    "orphanCountAfter": getattr(proc_result, "orphan_count_after", None),
                    "workerExitCode": getattr(proc_result, "exit_code", None),
                    "jobId": os.environ.get("CARDSCANR_JOB_ID"),
                    "attemptId": os.environ.get("CARDSCANR_LIVE_ATTEMPT_ID"),
                    "priceKeyId": os.environ.get("CARDSCANR_PRICE_KEY_ID")
                    or getattr(request.price_key, "id", None),
                    "fingerprint": os.environ.get("CARDSCANR_FINGERPRINT")
                    or getattr(request.price_key, "fingerprint", None),
                    "captureOrigin": os.environ.get("CARDSCANR_CAPTURE_ORIGIN") or "LIVE_BROWSER_CAPTURE",
                },
            )
            # Compact current-job capture contract for harness (correlation fields included).
            if isinstance(persist_info, dict):
                persist_info = {
                    **persist_info,
                    "targetId": capture.target_id,
                    "jobId": os.environ.get("CARDSCANR_JOB_ID"),
                    "attemptId": os.environ.get("CARDSCANR_LIVE_ATTEMPT_ID"),
                    "priceKeyId": os.environ.get("CARDSCANR_PRICE_KEY_ID")
                    or getattr(request.price_key, "id", None),
                    "fingerprint": os.environ.get("CARDSCANR_FINGERPRINT")
                    or getattr(request.price_key, "fingerprint", None),
                    "captureOrigin": os.environ.get("CARDSCANR_CAPTURE_ORIGIN") or "LIVE_BROWSER_CAPTURE",
                    "captureMethod": capture.capture_method,
                    "bodyLength": len(str((proc_result.payload or {}).get("body_text") or body_text or "")),
                }
            stage_timings.fields["persistedCaptureArtifact"] = persist_info
            stage_timings.fields["currentJobCapture"] = persist_info
            gui_marks_persist = stage_timings.fields.get("guiAttemptTimings")
            if not isinstance(gui_marks_persist, dict):
                gui_marks_persist = {"marks": {}}
                stage_timings.fields["guiAttemptTimings"] = gui_marks_persist
            gui_marks_persist.setdefault("marks", {})["T11_artifact_persisted"] = time.time()

            if is_ebay_authentication_url(page_url):
                raise ProviderAuthenticationRequiredError(
                    "eBay redirected the public sold-listing search to authentication; sign-in is not attempted",
                    diagnostics={"providerOutcome": "authentication_redirect", "navMode": nav_mode_label},
                )
            assert_final_url_matches_requested_marketplace(
                final_url=page_url,
                expected_provider_domain=search_query.provider_domain,
                requested_market_country=search_query.market_country,
                requested_currency=search_query.currency,
            )

            # No Playwright page handle — classify with representation-aware challenge model.
            selector_counts = {
                "process_capture": 1,
                "canonical_itm_href_count": int(
                    (capture.diagnostics or {}).get("canonicalItmHrefCount") or 0
                ),
            }
            challenge_ui = None
            worker_diag = (capture.diagnostics or {}).get("workerDiagnostics")
            if isinstance(worker_diag, dict) and isinstance(worker_diag.get("challengeUi"), dict):
                challenge_ui = worker_diag.get("challengeUi")
            elif isinstance((capture.diagnostics or {}).get("challengeUi"), dict):
                challenge_ui = (capture.diagnostics or {}).get("challengeUi")
            page_state = classify_browser_page_state(
                title=title,
                body_text=body_text,
                html_document=html_doc or None,
                url=page_url,
                selector_counts=selector_counts,
                challenge_ui=challenge_ui if isinstance(challenge_ui, dict) else None,
                x11_sold_state_verified=True,
            )
            stage_timings.fields["browserPageState"] = page_state
            if page_state["outcome"] in {
                "challenge_detected",
                "access_blocked",
                "authentication_required",
                "ambiguous_security_state",
            }:
                if page_state["outcome"] == "authentication_required":
                    raise ProviderAuthenticationRequiredError(
                        "eBay browser session requires sign-in before pricing can continue",
                        diagnostics={"providerOutcome": "authentication_required", "navMode": nav_mode_label},
                    )
                raise ProviderBlockedError(
                    "eBay returned a block or verification page; captcha bypass is not attempted"
                    if page_state["outcome"] != "ambiguous_security_state"
                    else "eBay page security state is ambiguous; failing closed without captcha bypass",
                    diagnostics={
                        "providerOutcome": page_state["outcome"],
                        "browserPageState": page_state,
                        "navMode": nav_mode_label,
                        "SOLD_STATE_VERIFIED": True,
                        "x11SoldStateVerified": True,
                    },
                )

            gui_marks = stage_timings.fields.get("guiAttemptTimings")
            if not isinstance(gui_marks, dict):
                gui_marks = {"marks": {}}
                stage_timings.fields["guiAttemptTimings"] = gui_marks
            marks = gui_marks.setdefault("marks", {})
            marks.setdefault("T10_html_data_captured", time.time())

            with _StageTimer(stage_timings, "parse_result_rows"):
                comps, parser_errors, visible_sample = self._parse_capture_candidates(
                    candidates=(capture.diagnostics or {}).get("candidates") or [],
                    request=request,
                    search_query=search_query,
                )
            stage_timings.fields["parsePhase"] = PARSE_COMPLETE
            marks["T11_exact_comp_parse_complete"] = time.time()
            quality_summary = build_quality_summary(comps, request=request)
            for error in parser_errors:
                url_quality = error.get("url_quality")
                if url_quality == "generic_non_item":
                    quality_summary["generic_url_count"] += 1
                elif url_quality in {"missing", "malformed_or_non_ebay"}:
                    quality_summary["missing_url_count"] += 1
            self._session_navs += 1
            self._write_debug_artifacts(
                page=None,
                request=request,
                search_query=search_query,
                title=title,
                body_text=body_text,
                detected_block=False,
                selector_counts=selector_counts,
                comps=comps,
                parser_errors=parser_errors,
                visible_result_text_sample=visible_sample,
                quality_summary=quality_summary,
                stage_timings=stage_timings.snapshot(),
            )
            marks["T12_pricing_calculation_complete"] = time.time()
            marks["T13_db_cache_snapshot_write_complete"] = time.time()
            marks["T14_job_finalized"] = time.time()
            stage_timings.fields["finalizeTerminal"] = FINALIZE_SUCCESS
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
                        "searchUrl": page_url,
                        "queryIndex": search_query.query_index,
                        "querySource": search_query.query_source,
                        "resultCount": len(comps),
                        "providerOutcome": "success" if comps else "no_results",
                        "browserPageState": page_state,
                        "navMode": nav_mode_label,
                        "desktopNav": stage_timings.fields.get("desktopNav"),
                        "soldState": sold_state,
                        "x11SoldStateVerified": True,
                        "postSoldCapturePhase": POST_SOLD_CAPTURE_READY,
                        "parsePhase": PARSE_COMPLETE,
                        "postSoldCapture": {
                            "targetId": capture.target_id,
                            "targetUrl": capture.target_url,
                            "captureElapsedMs": capture.capture_elapsed_ms,
                            "retryUsed": capture.retry_used,
                            "captureMethod": capture.capture_method,
                            "canonicalItmHrefCount": (capture.diagnostics or {}).get("canonicalItmHrefCount"),
                            "process": (capture.diagnostics or {}).get("captureProcess"),
                        },
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
                        "browserSessionNavs": self._session_navs,
                        "directSearchURL": False,
                        "playwrightNavigation": False,
                        "cdpUsedFor": "read_only_process_capture",
                        "finalizeTerminal": FINALIZE_SUCCESS,
                    }
                ),
            )

        return run_with_finalize_deadline(
            _post_sold_parse,
            timeout_seconds=finalize_budget,
            on_timeout=_disconnect_cdp_for_deadline,
            stage="post_sold_cdp_attach_and_parse",
        )

    def _fetch_with_playwright(self, *, request: ProviderRequest, search_query: ProviderSearchQuery) -> ProviderResult:
        if self._desktop_nav_mode_enabled():
            return self._fetch_with_desktop_win32(request=request, search_query=search_query)

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
        reuse = bool(self.config.reuse_context)
        owned_pw: Any | None = None
        context: Any = None
        page: Any = None
        try:
            if reuse:
                try:
                    with _StageTimer(stage_timings, "launch_browser"):
                        context, page = self._ensure_browser_session(request=request)
                except Exception as exc:
                    raise ProviderTemporaryError(
                        "Installed Google Chrome could not be launched through Playwright channel='chrome'. "
                        "Install Google Chrome, then verify Playwright support with: python -m playwright install chromium",
                        diagnostics={
                            "errorType": type(exc).__name__,
                            "browserConfig": self.config.safe_diagnostics(),
                            "timedOutStage": "launch_browser" if _looks_like_timeout(exc) else None,
                            "stageTimings": stage_timings.snapshot(),
                            "reuseContext": True,
                        },
                    ) from exc
                page.set_default_timeout(timeout_ms)
            else:
                owned_pw = sync_playwright().start()
                try:
                    profile_dir = self.config.ensure_profile_dir()
                    try:
                        with _StageTimer(stage_timings, "launch_browser"):
                            context = owned_pw.chromium.launch_persistent_context(
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
                except Exception:
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            pass
                        context = None
                    try:
                        owned_pw.stop()
                    except Exception:
                        pass
                    owned_pw = None
                    raise
            with _StageTimer(stage_timings, "open_ebay_page"):
                # Production query entry is RENDERED_UI_X11 (linux_x11 / desktop_win32).
                # Playwright must never navigate to a constructed /sch?_nkw= URL — that was
                # the Kakuna ACCOUNTING_CONTRACT_VIOLATION (homepage_then_clean_search_url).
                from .search_entry_contract import (
                    SEARCH_ENTRY_MODE_RENDERED_UI_X11,
                    UNACCOUNTED_SEARCH_URL_NAVIGATION,
                    ProviderInvariantError,
                    assert_programmatic_navigation_allowed,
                    is_query_bearing_search_results_url,
                )

                domain = str(search_query.provider_domain or "").strip().lower()
                is_au = domain.endswith("ebay.com.au") or str(search_query.market_country or "").upper() == "AU"
                # Homepage warm is allowed; query-bearing results URL is forbidden here.
                if is_au:
                    home = _ebay_https_origin(domain or "ebay.com.au") + "/"
                    assert_programmatic_navigation_allowed(home)
                    page.goto(home, wait_until="domcontentloaded", timeout=timeout_ms)
                    time.sleep(2.0)
                    entry_body = _safe_body_text(page)
                    entry_title = _safe_page_title(page)
                    entry_state = classify_browser_page_state(title=entry_title, body_text=entry_body)
                    if entry_state.get("reason") == "ebay_sorry_error_page" or str(page.url or "").lower().startswith(
                        "chrome-error:"
                    ):
                        # CASE 1: pre-search Sorry on homepage — no query submitted, no event.
                        stage_timings.fields["preSoldSorry"] = "PRE_SOLD_SORRY"
                        stage_timings.fields["searchEntryMode"] = SEARCH_ENTRY_MODE_RENDERED_UI_X11
                        stage_timings.fields["directSearchUrlNavigation"] = False
                        stage_timings.fields["auSearchEntry"] = "homepage_pre_search_sorry"
                        raise ProviderTemporaryError(
                            "eBay PRE_SOLD_SORRY on homepage before query submission",
                            diagnostics={
                                "preSoldSorry": "PRE_SOLD_SORRY",
                                "reason": "ebay_sorry_error_page",
                                "preSearchSorry": True,
                                "searchSubmissionStarted": False,
                                "browserPageState": entry_state,
                                "urlBeforeFilters": page.url,
                                "auSearchEntry": "homepage_pre_search_sorry",
                                "searchEntryMode": SEARCH_ENTRY_MODE_RENDERED_UI_X11,
                                "directSearchUrlNavigation": False,
                                "stageTimings": stage_timings.snapshot(),
                            },
                        )
                    # Refuse manufactured query URL navigation (programming invariant).
                    # assert_programmatic_navigation_allowed raises when event absent.
                    assert_programmatic_navigation_allowed(search_query.search_url)
                    # If somehow event exists, still refuse Playwright query-URL entry for AU.
                    raise ProviderInvariantError(
                        f"{UNACCOUNTED_SEARCH_URL_NAVIGATION}: Playwright path must not "
                        "open constructed search-results URLs; use RENDERED_UI_X11",
                        diagnostics={
                            "failureClass": UNACCOUNTED_SEARCH_URL_NAVIGATION,
                            "terminal": UNACCOUNTED_SEARCH_URL_NAVIGATION,
                            "searchEntryMode": SEARCH_ENTRY_MODE_RENDERED_UI_X11,
                            "directSearchUrlNavigation": True,
                            "queryBearingUrl": is_query_bearing_search_results_url(search_query.search_url),
                            "searchUrl": str(search_query.search_url or "")[:500],
                            "auSearchEntry": "forbidden_homepage_then_clean_search_url",
                            "stageTimings": stage_timings.snapshot(),
                        },
                    )
                # Non-AU: still forbid unaccounted query-bearing URL navigation.
                assert_programmatic_navigation_allowed(search_query.search_url)
                page.goto(search_query.search_url, wait_until="domcontentloaded", timeout=timeout_ms)
                time.sleep(2.0)
            if reuse:
                self._session_navs += 1
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
            assert_final_url_matches_requested_marketplace(
                    final_url=page.url,
                    expected_provider_domain=search_query.provider_domain,
                    requested_market_country=search_query.market_country,
                    requested_currency=search_query.currency,
                )
            with _StageTimer(stage_timings, "apply_sold_completed_filters"):
                    # Never deep-link sold URLs on first navigation — apply via refine UI.
                    filter_diagnostics = apply_sold_completed_filters_via_ui(page, timeout_ms=timeout_ms)
                    stage_timings.fields.setdefault("soldFilterUi", filter_diagnostics)
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
            assert_final_url_matches_requested_marketplace(
                    final_url=page.url,
                    expected_provider_domain=search_query.provider_domain,
                    requested_market_country=search_query.market_country,
                    requested_currency=search_query.currency,
                )

            smoke_key = is_smoke_pricing_key(
                    fingerprint=request.price_key.fingerprint,
                    set_code=request.price_key.set_code,
                    set_name=request.price_key.set_name,
                    card_name=request.price_key.card_name,
                    collector_number=request.price_key.collector_number,
                )
            b0_failfast = bool(smoke_key and smoke_failfast_enabled())
            wait_budget_ms = smoke_failfast_settle_ms(timeout_ms=timeout_ms) if b0_failfast else timeout_ms
            try:
                    with _StageTimer(stage_timings, "wait_for_result_container"):
                        page.wait_for_selector(RESULT_CONTAINER_SELECTOR, timeout=wait_budget_ms)
                    if timeout_instrumentation_enabled():
                        wait_ms = stage_timings.fields.get("stageDurationsMs", {}).get("wait_for_result_container")
                        if wait_ms is not None:
                            record_wait_for_result_container_duration(float(wait_ms))
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
                                "stage": "wait_for_result_container_failfast"
                                if b0_failfast
                                else "wait_for_result_container",
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
                    if should_fail_fast_empty_result_dom(
                        is_smoke=b0_failfast,
                        selector_counts=selector_counts,
                        page_state=page_state,
                    ):
                        stage_snapshot = stage_timings.snapshot()
                        timeout_instrumentation = build_result_container_timeout_diagnostics(
                            page_url=_safe_page_url(page),
                            wait_duration_ms=None,
                            browser_session_navs=self._session_navs if reuse else 1,
                            timeout_ms=wait_budget_ms,
                            timeout_seconds=self.config.timeout_seconds,
                            stage_timings=stage_snapshot,
                        )
                        raise ProviderTemporaryError(
                            "Empty eBay result DOM for smoke/synthetic key (fail-fast)",
                            diagnostics={
                                "errorType": type(exc).__name__,
                                "providerOutcome": "empty_result_dom",
                                "browserPageState": page_state,
                                "timedOutStage": "wait_for_result_container_failfast",
                                "failFast": True,
                                "smokeKey": True,
                                "waitBudgetMs": wait_budget_ms,
                                "stageTimings": stage_snapshot,
                                "candidateSelectorCounts": selector_counts,
                                "debugArtifacts": self._debug_artifact_paths(),
                                **timeout_instrumentation,
                            },
                        ) from exc
                    stage_snapshot = stage_timings.snapshot()
                    timeout_instrumentation = build_result_container_timeout_diagnostics(
                        page_url=_safe_page_url(page),
                        wait_duration_ms=None,
                        browser_session_navs=self._session_navs if reuse else 1,
                        timeout_ms=timeout_ms,
                        timeout_seconds=self.config.timeout_seconds,
                        stage_timings=stage_snapshot,
                    )
                    raise ProviderTemporaryError(
                        "Timed out waiting for eBay result container",
                        diagnostics={
                            "errorType": type(exc).__name__,
                            "providerOutcome": "timeout",
                            "browserPageState": page_state,
                            "timedOutStage": "wait_for_result_container",
                            "stageTimings": stage_snapshot,
                            "candidateSelectorCounts": selector_counts,
                            "debugArtifacts": self._debug_artifact_paths(),
                            **timeout_instrumentation,
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
                            "browserSessionNavs": self._session_navs if reuse else 1,
                            "browserReuseContext": reuse,
                        }
                    ),
                )
        finally:
            if not reuse:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                if owned_pw is not None:
                    try:
                        owned_pw.stop()
                    except Exception:
                        pass

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

    def _parse_capture_candidates(
        self,
        *,
        candidates: list[Any],
        request: ProviderRequest,
        search_query: ProviderSearchQuery,
    ) -> tuple[list[SoldComp], list[dict[str, Any]], str]:
        """Parse listing candidates already extracted by the capture subprocess."""
        comps: list[SoldComp] = []
        parse_errors: list[dict[str, Any]] = []
        visible_sample = ""
        rows = [c for c in (candidates or []) if isinstance(c, dict)]
        for index, candidate in enumerate(rows):
            if not visible_sample and candidate.get("text"):
                visible_sample = _normalise_text(candidate.get("text"))[:1000]
            try:
                comp = parse_candidate_dict(
                    candidate,
                    index=index,
                    request=request,
                    search_query=search_query,
                )
            except Exception:
                parse_errors.append(
                    {"index": index, "errorType": "candidate_exception", "source": candidate.get("source")}
                )
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
        raw_title = self._first_inner_text(
            card,
            [
                'h3.s-item__title [role="heading"]',
                "h3.s-item__title",
                '.s-item__title [role="heading"]',
                ".s-item__title span",
                ".s-item__title",
            ],
        )
        title = clean_candidate_title(raw_title)
        if not title:
            title = extract_title_from_lines(
                [_normalise_text(line) for line in raw_text.splitlines() if _normalise_text(line)],
                href_text="",
                expected_currency=search_query.currency,
            )
        if not title or is_chrome_only_title(title) or "shop on ebay" in title.lower():
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
            if page is None:
                (latest_dir / "page.html").write_text(
                    body_text if body_text.strip().startswith("<") else f"<!-- text capture -->\n{body_text}",
                    encoding="utf-8",
                )
            else:
                # Avoid unbounded page.content(); rely on page default timeout from CDP attach.
                html = page.evaluate("() => document.documentElement.outerHTML")
                if html:
                    (latest_dir / "page.html").write_text(str(html), encoding="utf-8")
        except Exception:
            try:
                (latest_dir / "page.html").write_text(
                    f"<!-- page.html skipped: content capture failed; title={title!s} -->\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
        try:
            if page is not None:
                page.screenshot(path=str(latest_dir / "screenshot.png"), full_page=False, timeout=8_000)
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
                "page_url_after_load": (getattr(page, "url", "") if page is not None else ""),
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
