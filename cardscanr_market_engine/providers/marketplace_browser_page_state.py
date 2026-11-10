"""Canonical eBay browser page security/result classification (shared by provider and Sold health)."""
from __future__ import annotations

import re
from typing import Any

from .sold_page_health import is_ebay_authentication_url

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
MANDATORY_AUTH_VISIBLE_MARKERS = (
    "session expired",
    "log in to continue",
    "please sign in",
    "you must sign in",
    "you must sign in before",
)
OPTIONAL_SIGN_IN_BANNER_SUFFIXES = (
    "for faster checkout",
    "for the best experience",
    "to see saved items",
    "to sync your watchlist",
)
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


def _genuine_authentication_wall(
    *,
    title: str,
    visible_haystack: str,
    visible_haystack_compact: str,
    matched_compact,
) -> str | None:
    """Auth overlays that must hard-stop even when Sold URL / prior evidence is present."""
    title_l = str(title or "").strip().lower()
    if "sign in to continue" in title_l and "ebay" in title_l:
        return "signin_title"
    if "sign in or register" in title_l:
        return "signin_title"
    if re.search(r"\bpassword\b", visible_haystack) and re.search(
        r"\b(email|username|user name)\b",
        visible_haystack,
    ):
        return "auth_form"
    if marker := matched_compact(MANDATORY_AUTH_VISIBLE_MARKERS):
        return marker
    if "sign in to continue" in visible_haystack_compact:
        if any(suffix in visible_haystack_compact for suffix in OPTIONAL_SIGN_IN_BANNER_SUFFIXES):
            return None
        return "sign in to continue"
    if marker := matched_compact(AUTH_TEXT_MARKERS):
        return marker
    return None


def _optional_sign_in_banner_only(*, visible_haystack_compact: str, strong_listing_evidence: bool) -> bool:
    if not strong_listing_evidence:
        return False
    if "sign in to continue" not in visible_haystack_compact:
        return False
    return any(suffix in visible_haystack_compact for suffix in OPTIONAL_SIGN_IN_BANNER_SUFFIXES)


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
    visible_haystack_compact = " ".join(visible_haystack.split())

    def matched_visible_strict(markers: tuple[str, ...]) -> str | None:
        return next((marker for marker in markers if marker.lower() in visible_haystack), None)

    def matched_visible_compact(markers: tuple[str, ...]) -> str | None:
        for marker in markers:
            needle = marker.lower()
            if needle in visible_haystack:
                return marker
            needle_compact = " ".join(needle.split())
            if needle_compact and needle_compact in visible_haystack_compact:
                return marker
        return None

    passive_resources = _passive_challenge_resources(html_source or (body_text if _looks_like_html_document(body_text) else ""))
    # Bare captcha token in visible text alone is not enough; require active phrases/URL/UI.
    url_marker = _active_challenge_url(url)
    ui_active, ui_reason = _challenge_ui_active(challenge_ui)
    active_text_marker = matched_visible_compact(ACTIVE_CHALLENGE_VISIBLE_TEXT_MARKERS)

    ordinary_markers = matched_visible_strict(RESULT_TEXT_MARKERS)
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

    # SORRY / maintenance / access — evaluate on visible text (and title), not CSS.
    if marker := matched_visible_strict(ACCESS_BLOCK_TEXT_MARKERS):
        return {"outcome": "access_blocked", "reason": marker, "retryable": True, **base_diag}
    if marker := matched_visible_strict(MAINTENANCE_TEXT_MARKERS):
        return {"outcome": "provider_unavailable", "reason": marker, "retryable": True, **base_diag}
    if marker := matched_visible_compact(SORRY_ERROR_TEXT_MARKERS) or (
        "error page" in visible_haystack and "something went wrong" in visible_haystack
    ):
        return {"outcome": "provider_unavailable", "reason": "ebay_sorry_error_page", "retryable": True, **base_diag}

    # Genuine auth overlays must stop before active-challenge / body-readiness success paths.
    optional_sign_in_banner = _optional_sign_in_banner_only(
        visible_haystack_compact=visible_haystack_compact,
        strong_listing_evidence=strong_listing_evidence,
    )
    if not optional_sign_in_banner:
        if auth_reason := _genuine_authentication_wall(
            title=title,
            visible_haystack=visible_haystack,
            visible_haystack_compact=visible_haystack_compact,
            matched_compact=matched_visible_compact,
        ):
            return {
                "outcome": "authentication_required",
                "reason": auth_reason,
                "retryable": False,
                "securityClass": "EBAY_AUTH_REQUIRED",
                **base_diag,
            }

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

    if result_count <= 0 and (marker := matched_visible_strict(CONSENT_TEXT_MARKERS)):
        return {"outcome": "provider_unavailable", "reason": f"interstitial:{marker}", "retryable": True, **base_diag}
    if marker := matched_visible_strict(NO_RESULTS_TEXT_MARKERS):
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

