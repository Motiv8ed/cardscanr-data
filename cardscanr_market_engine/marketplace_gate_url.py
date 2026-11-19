"""Detect real eBay captcha/sorry/sign-in surfaces (path/host only, not query text)."""
from __future__ import annotations

from urllib.parse import urlparse

# Whole-document gate titles only (never substring match inside card listing titles).
EXACT_GATE_PAGE_TITLES = frozenset(
    {
        "access denied",
        "security measure",
        "pardon our interruption",
    }
)

_EBAY_AUTH_PATH_MARKERS = ("/signin/", "/signin", "/login/", "/login", "/identity/")

_TITLE_SUFFIXES = (
    " | ebay.com.au",
    " - ebay.com.au",
    " | ebay",
    " - ebay",
)


def normalized_gate_page_title(title: str | None) -> str:
    text = str(title or "").strip()
    if not text:
        return ""
    lower = text.lower()
    for suffix in _TITLE_SUFFIXES:
        if lower.endswith(suffix):
            text = text[: -len(suffix)].strip()
            lower = text.lower()
    return lower


def is_exact_marketplace_gate_page_title(title: str | None) -> bool:
    return normalized_gate_page_title(title) in EXACT_GATE_PAGE_TITLES


def _ebay_host_ok(host: str) -> bool:
    h = (host or "").lower()
    if h.startswith("www."):
        h = h[4:]
    roots = (
        "ebay.com",
        "ebay.com.au",
        "ebay.co.uk",
        "ebay.ca",
        "ebay.de",
        "ebay.fr",
        "ebay.it",
        "ebay.es",
    )
    return any(h == root or h.endswith("." + root) for root in roots)


def is_ebay_marketplace_gate_url(url: str | None) -> bool:
    text = str(url or "").strip()
    if not text or "ebay." not in text.lower():
        return False
    try:
        parsed = urlparse(text)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if host.startswith("signin.") and "ebay" in host:
        return True
    if "signin.ebay" in host:
        return True
    if _ebay_host_ok(host) and any(marker in path for marker in _EBAY_AUTH_PATH_MARKERS):
        return True
    if "/splashui/" in path and ("challenge" in path or "captcha" in path):
        return True
    if path.startswith("/challenge"):
        return True
    if "captcha.ebay." in host:
        return True
    if path == "/sorry" or path.endswith("/sorry"):
        return True
    if path == "/sorry.html" or path.endswith("/sorry.html"):
        return True
    if "/srv/survey" in path:
        return True
    return False


def is_pardon_interstitial_at_marketplace_url(
    url: str | None,
    *,
    title: str | None = None,
    body: str | None = None,
) -> bool:
    """Incapsula-style interstitial body/title while URL still looks like ordinary eBay browsing."""
    if not url or is_ebay_marketplace_gate_url(url):
        return False
    try:
        parsed = urlparse(str(url))
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if not _ebay_host_ok(host):
        return False
    if is_exact_marketplace_gate_page_title(title):
        return True
    return "pardon our interruption" in str(body or "").lower()


def is_marketplace_gate_page(
    url: str | None,
    *,
    title: str | None = None,
    body: str | None = None,
) -> bool:
    if is_ebay_marketplace_gate_url(url):
        return True
    if is_exact_marketplace_gate_page_title(title):
        return True
    return is_pardon_interstitial_at_marketplace_url(url, title=title, body=body)