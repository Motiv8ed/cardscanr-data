"""Detect real eBay captcha/sorry/sign-in surfaces (path/host only, not query text)."""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Whole-document gate titles only (never substring match inside card listing titles).
EXACT_GATE_PAGE_TITLES = frozenset(
    {
        "access denied",
        "security measure",
        "pardon our interruption",
    }
)

_AUTH_PATH_SEGMENTS = frozenset({"signin", "login", "identity"})

_TITLE_SUFFIXES = (
    " | ebay.com.au",
    " - ebay.com.au",
    " | ebay",
    " - ebay",
)

_TITLE_PREFIXES = (
    "ebay.com.au | ",
    "ebay.com.au - ",
    "ebay | ",
    "ebay - ",
)

_WHITESPACE_RE = re.compile(r"[\s\xa0\u00a0\u202f]+")

PARDON_INTERRUPTION_INTERSTITIAL_MARKER = "pardon our interruption"


def _strip_trailing_title_punctuation(text: str) -> str:
    return text.rstrip(" .…\u2026-—:,;!\"")


def normalized_gate_page_title(title: str | None) -> str:
    text = str(title or "").strip()
    if not text:
        return ""
    text = _WHITESPACE_RE.sub(" ", text).strip()
    lower = text.lower()
    for prefix in _TITLE_PREFIXES:
        if lower.startswith(prefix):
            text = text[len(prefix) :].strip()
            text = _WHITESPACE_RE.sub(" ", text).strip()
            lower = text.lower()
    for suffix in _TITLE_SUFFIXES:
        if lower.endswith(suffix):
            text = text[: -len(suffix)].strip()
            text = _WHITESPACE_RE.sub(" ", text).strip()
            lower = text.lower()
    return _strip_trailing_title_punctuation(lower)


def _title_starts_with_pardon(norm_title: str) -> bool:
    core = _strip_trailing_title_punctuation(norm_title)
    if core == PARDON_INTERRUPTION_INTERSTITIAL_MARKER:
        return True
    return core.startswith(f"{PARDON_INTERRUPTION_INTERSTITIAL_MARKER} ")


def is_exact_marketplace_gate_page_title(title: str | None) -> bool:
    norm = normalized_gate_page_title(title)
    if norm in EXACT_GATE_PAGE_TITLES:
        return True
    return _title_starts_with_pardon(norm)


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


def _path_has_auth_gate_segment(path: str) -> bool:
    segments = {seg.lower() for seg in (path or "").split("/") if seg}
    return bool(segments & _AUTH_PATH_SEGMENTS)


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
    if _ebay_host_ok(host) and _path_has_auth_gate_segment(path):
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
    """Pardon only on exact interstitial title or a dedicated standalone interstitial body."""
    if not url or is_ebay_marketplace_gate_url(url):
        return False
    try:
        parsed = urlparse(str(url))
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if not _ebay_host_ok(host):
        return False
    title_norm = normalized_gate_page_title(title)
    if _title_starts_with_pardon(title_norm):
        return True
    if not title_norm:
        body_norm = normalized_gate_page_title(body)
        if body_norm == PARDON_INTERRUPTION_INTERSTITIAL_MARKER or _title_starts_with_pardon(body_norm):
            return True
    return False


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