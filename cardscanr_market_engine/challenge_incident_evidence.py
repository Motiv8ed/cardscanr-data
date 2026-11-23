"""Privacy-bounded, failure-time evidence for eBay pricing challenge incidents.

The existing worker must stop on an eBay gate. We retain the policy reason
and exact gate category so that subsequent review can distinguish a genuine
gate page from a code-only signal without recreating the navigation.
No browser interaction, scraping, screenshot or marketplace request occurs here.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlsplit

from .marketplace_gate_url import (
    is_ebay_marketplace_gate_url,
    is_exact_marketplace_gate_page_title,
    normalized_gate_page_title,
)

EVIDENCE_SCHEMA = "pricing_challenge_evidence_v1"
_REASON_ALLOWLIST = frozenset({
    "INTER_CARD_CHALLENGE_TARGET",
    "CHALLENGE_REQUIRED",
    "EBAY_CHALLENGE_REQUIRED",
    "EBAY_CHALLENGE",
    "EBAY_AUTH_REQUIRED",
    "INTER_CARD_PRIOR_TARGET_NOT_CORRELATED",
    "INTER_CARD_EXPECTED_PRIOR_TARGET",
    "INTER_CARD_UNKNOWN_EBAY_TARGET",
    "INTER_CARD_MULTIPLE_TOP_LEVEL_EBAY_TARGETS",
    "INTER_CARD_PRIOR_CARD_NOT_TERMINAL",
    "COLD_START_UNEXPECTED_EBAY_TARGET",
})
_GATE_TITLE_ALLOWLIST = frozenset({
    "access denied",
    "security measure",
    "pardon our interruption",
    "verify yourself",
    "verify yourself to continue",
    "verify you are human",
    "sign in to continue",
})
_KNOWN_HOSTS = frozenset({
    "ebay.com", "ebay.com.au", "ebay.co.uk", "ebay.ca",
    "ebay.de", "ebay.fr", "ebay.it", "ebay.es",
})
_ALLOWED_ORIGINS = frozenset({
    "ebay_challenge", "ebay_marketplace", "auxiliary",
    "advertising", "blank_or_browser", "challenge",
})


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _safe_codes(value: Any) -> list[str]:
    raw = value if isinstance(value, (list, tuple, set)) else [value]
    found: set[str] = set()
    for item in list(raw)[:32]:
        for part in str(item or "").split(",")[:8]:
            code = part.strip().upper()
            if code in _REASON_ALLOWLIST:
                found.add(code)
    return sorted(found)[:16]


def _nested_dict(container: dict[str, Any], *parts: str) -> dict[str, Any]:
    current: Any = container
    for part in parts:
        current = current.get(part) if isinstance(current, dict) else None
    return current if isinstance(current, dict) else {}


def _policy_from_diag(diag: dict[str, Any]) -> dict[str, Any]:
    candidates = (
        _nested_dict(diag, "targetPolicy"),
        _nested_dict(diag, "diagnostics", "targetPolicy"),
        _nested_dict(diag, "search", "targetPolicy"),
        _nested_dict(diag, "desktopNav", "search", "targetPolicy"),
        _nested_dict(diag, "desktopNav", "search", "diagnostics", "targetPolicy"),
    )
    return next((item for item in candidates if item), {})


def _url_class(url: Any) -> dict[str, Any]:
    """Never persist full URL, query, fragment, item IDs or credentials."""
    raw = url if isinstance(url, str) else ""
    result: dict[str, Any] = {
        "present": bool(raw),
        "urlSha256": _hash(raw) if raw else None,
        "hostClass": "UNKNOWN",
        "pathClass": "UNKNOWN",
        "gateUrlObserved": False,
    }
    if not raw or len(raw) > 8192:
        return result
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        host_root = host.removeprefix("www.")
    except ValueError:
        return result
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return result
    known = host_root in _KNOWN_HOSTS or host_root in {
        "signin.ebay.com", "signin.ebay.com.au", "captcha.ebay.com",
        "captcha.ebay.com.au",
    }
    if not known:
        result["hostClass"] = "OTHER"
        return result
    # Canonical and subdomain classes only: avoid arbitrary user-controlled host text.
    result["hostClass"] = (
        "EBAY_SIGNIN" if host_root.startswith("signin.ebay.")
        else "EBAY_CAPTCHA" if host_root.startswith("captcha.ebay.")
        else "EBAY_MARKETPLACE"
    )
    path = (parsed.path or "").lower()
    if "/splashui/" in path and ("challenge" in path or "captcha" in path):
        category = "SPLASHUI_CHALLENGE"
    elif path.startswith("/challenge"):
        category = "CHALLENGE"
    elif "/signin" in path or "/identity" in path or "/login" in path:
        category = "SIGN_IN"
    elif path in ("/sorry", "/sorry.html"):
        category = "SORRY"
    elif path.startswith("/sch/"):
        category = "SEARCH_RESULTS"
    elif path.startswith("/itm/"):
        category = "ITEM_PAGE"
    else:
        category = "OTHER_EBAY"
    result["pathClass"] = category
    # Gate detection must require an authentic eBay host; a foreign /challenge
    # URL must not prove that eBay challenged the worker.
    result["gateUrlObserved"] = bool(
        is_ebay_marketplace_gate_url(raw)
        and result["hostClass"] in ("EBAY_MARKETPLACE", "EBAY_SIGNIN", "EBAY_CAPTCHA")
    )
    return result


def _title_class(title: Any) -> dict[str, Any]:
    raw = title if isinstance(title, str) else ""
    canonical = normalized_gate_page_title(raw)
    # Only a recognised, generic eBay gate title is stored in plain text.
    exact = canonical in _GATE_TITLE_ALLOWLIST or (
        is_exact_marketplace_gate_page_title(raw)
        and canonical.startswith("pardon our interruption")
    )
    return {
        "present": bool(raw),
        "titleSha256": _hash(raw) if raw else None,
        # Preserve only canonical generic wording, never free-form appended text.
        "gateTitle": ("pardon our interruption" if canonical.startswith(
            "pardon our interruption") else canonical) if exact else None,
        "gateTitleObserved": exact,
    }


def _target_evidence(item: dict[str, Any]) -> dict[str, Any]:
    url = _url_class(item.get("url"))
    title = _title_class(item.get("title"))
    cdp_type = str(item.get("type") or "").lower()
    origin = str(item.get("originClass") or item.get("origin_class") or "").lower()
    top = item.get("topLevel", item.get("top_level", cdp_type == "page"))
    is_page = cdp_type == "page" and top is True
    return {
        "cdpType": cdp_type if cdp_type in ("page", "iframe", "service_worker", "worker")
        else "OTHER",
        "topLevel": bool(is_page),
        "originClass": origin if origin in _ALLOWED_ORIGINS else "UNKNOWN",
        "reasonCodes": _safe_codes(item.get("reason") or item.get("reasonCode")),
        "blocking": item.get("blocking") is True,
        **url,
        **title,
        "directGateEvidence": bool(
            is_page and url["hostClass"].startswith("EBAY_")
            and (url["gateUrlObserved"] or title["gateTitleObserved"])
        ),
    }


def build_challenge_incident_evidence(
    diagnostics: dict[str, Any] | None,
    *,
    category: str,
) -> dict[str, Any]:
    """Return a fixed schema of observations, with no raw URLs or page titles.

    Evidence quality is descriptive only, never a gate-unlock decision. Even a
    code-only challenge must remain stopped pending authorised human review.
    """
    diag = diagnostics if isinstance(diagnostics, dict) else {}
    policy = _policy_from_diag(diag)
    nav = _nested_dict(diag, "desktopNav", "search")
    codes: set[str] = set()
    for value in (
        diag.get("resultCode"),
        policy.get("reasonCodes"),
        policy.get("reason_codes"),
        nav.get("resultCode"),
        _nested_dict(diag, "diagnostics").get("resultCode"),
    ):
        codes.update(_safe_codes(value))
    classified = policy.get("classified")
    if not isinstance(classified, list):
        classified = []
    # Scan every bounded target before choosing the compact receipt sample.
    # A real gate can be target 9/10 among auxiliary CDP workers; truncating to
    # the first eight previously mislabeled that incident CODE_ONLY_UNCORROBORATED.
    scanned = [
        _target_evidence(row) for row in classified[:128] if isinstance(row, dict)
    ]
    direct_first = [row for row in scanned if row["directGateEvidence"]]
    challenge_code = [
        row for row in scanned if not row["directGateEvidence"]
        and "INTER_CARD_CHALLENGE_TARGET" in row["reasonCodes"]
    ]
    remainder = [
        row for row in scanned if not row["directGateEvidence"]
        and "INTER_CARD_CHALLENGE_TARGET" not in row["reasonCodes"]
    ]
    target_evidence = (direct_first + challenge_code + remainder)[:8]
    # Prefer observed navigation URL over a pre-search URL when both exist.
    nav_url = diag.get("url") or nav.get("url") or diag.get("urlBeforeSearch")
    nav_title = diag.get("title") or nav.get("title") or diag.get("titleBeforeSearch")
    nav_obs = {**_url_class(nav_url), **_title_class(nav_title)}
    nav_direct = bool(
        nav_obs["hostClass"].startswith("EBAY_")
        and (nav_obs["gateUrlObserved"] or nav_obs["gateTitleObserved"])
    )
    direct = nav_direct or any(row["directGateEvidence"] for row in scanned)
    code_only = bool(codes) and not direct
    return {
        "schema": EVIDENCE_SCHEMA,
        "failureCategory": category if category in (
            "CHALLENGE_REQUIRED", "AUTH_REQUIRED", "TRANSIENT_EBAY",
        ) else "OTHER",
        "reasonCodes": sorted(codes)[:16],
        "policyPresent": bool(policy),
        "policyClassifiedTargetCount": len(classified),
        "targetEvidence": target_evidence,
        "navigation": {**nav_obs, "directGateEvidence": nav_direct},
        "providerReportedChallenge": diag.get("challenge") is True,
        "evidenceQuality": (
            "GATE_PAGE_OBSERVED" if direct
            else "CODE_ONLY_UNCORROBORATED" if code_only
            else "NO_DIRECT_PAGE_EVIDENCE"
        ),
        "automaticResumeAllowed": False,
        "source": "failure_time_provider_diagnostics",
    }
