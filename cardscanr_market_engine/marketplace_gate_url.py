"""Detect real eBay captcha/sorry/sign-in surfaces (path/host only, not query text)."""
from __future__ import annotations

from urllib.parse import urlparse


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