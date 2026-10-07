#!/usr/bin/env python3
"""Post-Sold page health: filter-state vs rendered-page health.

Separates:
  soldFilterStateVerified  — LH_Sold=1 after exact Sold interaction
  soldPageHealthVerified   — current rendered page is a healthy Sold results page
  x11SoldStateVerified     — BOTH required (capture-ready Sold result)

Marketplace Error Page | eBay is NOT a CDP target-absent failure and is NOT
automatically CAPTCHA/challenge unless challenge evidence is present.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlparse

# Canonical marketplace page classes (distinct from CAPTCHA / SORRY / 403).
EBAY_ERROR_PAGE = "EBAY_ERROR_PAGE"
MARKETPLACE_ERROR_PAGE = "MARKETPLACE_ERROR_PAGE"
TARGET_REJECTED_UNHEALTHY_PAGE = "TARGET_REJECTED_UNHEALTHY_PAGE"
EBAY_SORRY_PAGE = "EBAY_SORRY"
EBAY_CHALLENGE_PAGE = "EBAY_CHALLENGE"
EBAY_AUTH_REQUIRED = "EBAY_AUTH_REQUIRED"
EBAY_ACCESS_DENIED_403 = "EBAY_ACCESS_DENIED_403"
HEALTHY_SOLD_RESULTS = "HEALTHY_SOLD_RESULTS"
HEALTHY_SOLD_ZERO_RESULTS = "HEALTHY_SOLD_ZERO_RESULTS"
EBAY_AUTH_PATH_MARKERS = ("/signin/", "/signin", "/login/", "/login", "/identity/")

PHASE_PAGE_HEALTH_VERIFICATION = "PAGE_HEALTH_VERIFICATION"

# Capture / selection failure classes (do not collapse into CDP_TARGET_NOT_FOUND).
CDP_TARGET_ATTACH_FAILURE = "CDP_TARGET_ATTACH_FAILURE"


def is_ebay_error_page(
    title: str | None = None,
    url: str | None = None,
    body: str | None = None,
) -> bool:
    """True for marketplace Error Page | eBay (not CAPTCHA, not classic SORRY body alone)."""
    t = (title or "").strip().lower()
    u = (url or "").strip().lower()
    # Title is the authoritative Error Page signal (Rowlet / Iron Bundle class).
    if "error page" in t and "ebay" in t:
        return True
    # Dedicated /error path without challenge markers.
    if "/error" in u and "splashui/challenge" not in u and "captcha" not in t:
        return True
    _ = body  # body may reinforce but title/url suffice
    return False


def is_ebay_authentication_url(url: str | None) -> bool:
    """True when navigation left public browsing for eBay sign-in (not CAPTCHA)."""
    parsed = urlparse(str(url or "").strip())
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    if not host or "ebay." not in host:
        return False
    if host.startswith(("signin.", "login.")):
        return True
    path = parsed.path.lower()
    return any(marker in path for marker in EBAY_AUTH_PATH_MARKERS)


def marketplace_hostname_ok(url: str | None, *, expected_origin: str = "ebay.com.au") -> bool:
    u = str(url or "").lower()
    if "ebay." not in u:
        return False
    host = urlparse(u).hostname or ""
    if expected_origin.lower() in host:
        return True
    # Allow regional ebay hosts (ebay.com, ebay.co.uk, …).
    return host.endswith("ebay.com") or ".ebay." in host or host.startswith("ebay.")


def url_has_lh_sold(url: str | None) -> bool:
    return "lh_sold=1" in str(url or "").lower()


def _has_sold_results_structure(body: str | None, title: str | None) -> bool:
    """Ordinary Sold-results structure; zero listings may still be healthy."""
    b = str(body or "").lower()
    t = str(title or "").lower()
    if "sold items" in b or "sold listings" in b:
        return True
    if "sold " in b:  # sold dates / sold labels
        return True
    # Title often includes query + "for sale | eBay" or similar on healthy results.
    if "ebay" in t and "error page" not in t and "sorry" not in t[:80] and "captcha" not in t:
        # Without body, URL+title on /sch/ with LH_Sold is enough for health (X11 path).
        return True
    if "results" in b or "filter" in b:
        return True
    # Explicit zero-results wording still healthy.
    if any(
        m in b
        for m in (
            "no exact matches",
            "0 results",
            "did not match any items",
            "no matching items",
            "try checking your spelling",
        )
    ):
        return True
    return False


def _is_challenge_blob(title: str | None, url: str | None, body: str | None) -> bool:
    """Active challenge only — passive recaptcha iframe alone is non-blocking."""
    t = (title or "").lower()
    u = (url or "").lower()
    b = (body or "").lower()
    if "splashui/challenge" in u or "security measure" in t or "verify yourself" in t:
        return True
    if "verification challenge" in f"{t}\n{b}":
        return True
    # Title/URL captcha — active.
    if "captcha" in t or "captcha" in u:
        return True
    # Body: captcha wording without only being a passive google recaptcha iframe.
    if "captcha" in b:
        passive_only = "recaptcha" in b and "iframe" in b and "verify yourself" not in b
        if not passive_only:
            return True
    return False


def _is_classic_sorry_blob(title: str | None, url: str | None, body: str | None) -> bool:
    """Classic SORRY only — Error Page title/URL excluded."""
    t = (title or "").strip().lower()
    u = (url or "").strip().lower()
    b = (body or "").strip().lower()
    blob = f"{t}\n{b}"
    if _is_challenge_blob(title, url, body):
        return False
    if ("error page" in t and "ebay" in t) or "/error" in u:
        return False
    if "sorry" in blob and (
        "something went wrong" in blob or "looks like" in blob or "on our end" in blob
    ):
        return True
    if "something went wrong on our end" in blob:
        return True
    if t.startswith("sorry") and "ebay" in t:
        return True
    return False


def classify_marketplace_page_class(
    *,
    url: str | None,
    title: str | None,
    body: str | None = None,
    http_status: int | None = None,
) -> str | None:
    """Return a marketplace page class or None when not a known unhealthy class."""
    if http_status == 403:
        return EBAY_ACCESS_DENIED_403
    if is_ebay_authentication_url(url):
        return EBAY_AUTH_REQUIRED
    if _is_challenge_blob(title, url, body):
        return EBAY_CHALLENGE_PAGE
    if is_ebay_error_page(title=title, url=url, body=body):
        return EBAY_ERROR_PAGE
    if _is_classic_sorry_blob(title, url, body):
        return EBAY_SORRY_PAGE
    return None


def classify_sold_page_health(
    *,
    url: str | None,
    title: str | None,
    body: str | None = None,
    http_status: int | None = None,
    expected_origin: str = "ebay.com.au",
    require_body_structure: bool = False,
) -> dict[str, Any]:
    """Bounded positive health check for a post-Sold rendered page (read-only evidence)."""
    unhealthy = classify_marketplace_page_class(
        url=url, title=title, body=body, http_status=http_status
    )
    filter_ok = url_has_lh_sold(url)
    host_ok = marketplace_hostname_ok(url, expected_origin=expected_origin)
    top_level = bool(str(url or "").strip()) and not str(url or "").lower().startswith("about:blank")
    title_l = str(title or "").strip().lower()
    title_ok = bool(title_l) and "error page" not in title_l and "untitled" not in title_l

    reasons: list[str] = []
    if not top_level:
        reasons.append("missing_top_level_page")
    if not host_ok:
        reasons.append("hostname_mismatch")
    if not filter_ok:
        reasons.append("missing_lh_sold")
    if not title_ok:
        reasons.append("title_unhealthy_or_empty")
    if unhealthy:
        reasons.append(f"page_class:{unhealthy}")

    structure_ok = True
    page_class = HEALTHY_SOLD_RESULTS
    if require_body_structure or (body is not None and str(body).strip()):
        structure_ok = _has_sold_results_structure(body, title)
        if not structure_ok:
            reasons.append("sold_results_structure_missing")
        elif body is not None and not any(
            x in str(body).lower() for x in ("sold ", "/itm/", "au $", "$")
        ) and any(
            m in str(body).lower()
            for m in ("no exact matches", "0 results", "did not match any items", "no matching items")
        ):
            page_class = HEALTHY_SOLD_ZERO_RESULTS

    if unhealthy:
        page_class = unhealthy

    healthy = bool(
        top_level
        and host_ok
        and filter_ok
        and title_ok
        and unhealthy is None
        and structure_ok
    )
    if healthy and page_class not in {HEALTHY_SOLD_RESULTS, HEALTHY_SOLD_ZERO_RESULTS}:
        page_class = HEALTHY_SOLD_RESULTS

    return {
        "soldPageHealthVerified": healthy,
        "marketplacePageClass": page_class if healthy or unhealthy else None,
        "unhealthyClass": unhealthy,
        "hostnameOk": host_ok,
        "titleOk": title_ok,
        "topLevelPresent": top_level,
        "lhSold": filter_ok,
        "structureOk": structure_ok,
        "reasons": reasons,
        "ok": healthy,
    }


def _url_has_unexpected_filter(url: str | None, *, url_before: str | None = None) -> bool:
    u = str(url or "").lower()
    if url_has_lh_sold(u):
        return False
    before = str(url_before or "").lower()
    if u == before:
        return False
    markers = ("lh_prefloc=", "lh_itemcondition=", "lh_bin=", "lh_auction=", "lh_complete=")
    return any(m in u for m in markers) and not any(m in before for m in markers if m != "lh_complete=")


def evaluate_sold_verification(
    *,
    url: str | None,
    title: str | None,
    body: str | None = None,
    url_before: str | None = None,
    http_status: int | None = None,
    expected_origin: str = "ebay.com.au",
    require_body_structure: bool = False,
) -> dict[str, Any]:
    """Compose filter-state + page-health into x11SoldStateVerified semantics."""
    u0 = str(url or "").strip().lower()
    t0 = str(title or "").strip().lower()
    if u0.startswith("about:blank") or t0 == "about:blank":
        return {
            "soldFilterStateVerified": False,
            "soldPageHealthVerified": False,
            "x11SoldStateVerified": False,
            "verified": False,
            "terminal": "ABOUT_BLANK_ABORT",
            "marketplacePageClass": None,
            "phaseHint": "ABOUT_BLANK_ABORT",
            "captureReady": False,
        }

    filter_ok = url_has_lh_sold(url)
    health = classify_sold_page_health(
        url=url,
        title=title,
        body=body,
        http_status=http_status,
        expected_origin=expected_origin,
        require_body_structure=require_body_structure,
    )
    unhealthy = health.get("unhealthyClass")

    if unhealthy == EBAY_AUTH_REQUIRED:
        return {
            "soldFilterStateVerified": filter_ok,
            "soldPageHealthVerified": False,
            "x11SoldStateVerified": False,
            "verified": False,
            "terminal": EBAY_AUTH_REQUIRED,
            "marketplacePageClass": EBAY_AUTH_REQUIRED,
            "health": health,
            "captureReady": False,
            "phaseHint": PHASE_PAGE_HEALTH_VERIFICATION,
        }
    if unhealthy == EBAY_CHALLENGE_PAGE:
        return {
            "soldFilterStateVerified": filter_ok,
            "soldPageHealthVerified": False,
            "x11SoldStateVerified": False,
            "verified": False,
            "terminal": EBAY_CHALLENGE_PAGE,
            "marketplacePageClass": EBAY_CHALLENGE_PAGE,
            "health": health,
            "captureReady": False,
            "phaseHint": PHASE_PAGE_HEALTH_VERIFICATION,
        }
    if unhealthy == EBAY_ACCESS_DENIED_403:
        return {
            "soldFilterStateVerified": filter_ok,
            "soldPageHealthVerified": False,
            "x11SoldStateVerified": False,
            "verified": False,
            "terminal": EBAY_ACCESS_DENIED_403,
            "marketplacePageClass": EBAY_ACCESS_DENIED_403,
            "health": health,
            "captureReady": False,
            "phaseHint": PHASE_PAGE_HEALTH_VERIFICATION,
        }
    if unhealthy == EBAY_ERROR_PAGE:
        return {
            "soldFilterStateVerified": filter_ok,
            "soldPageHealthVerified": False,
            "x11SoldStateVerified": False,
            "verified": False,
            "terminal": EBAY_ERROR_PAGE,
            "marketplacePageClass": EBAY_ERROR_PAGE,
            "health": health,
            "captureReady": False,
            "phaseHint": PHASE_PAGE_HEALTH_VERIFICATION,
            "stableUnhealthyPending": True,
        }
    if unhealthy == EBAY_SORRY_PAGE:
        return {
            "soldFilterStateVerified": filter_ok,
            "soldPageHealthVerified": False,
            "x11SoldStateVerified": False,
            "verified": False,
            "terminal": EBAY_SORRY_PAGE,
            "marketplacePageClass": EBAY_SORRY_PAGE,
            "health": health,
            "captureReady": False,
            "phaseHint": PHASE_PAGE_HEALTH_VERIFICATION,
            "stableUnhealthyPending": True,
        }

    if not filter_ok:
        if _url_has_unexpected_filter(url, url_before=url_before):
            return {
                "soldFilterStateVerified": False,
                "soldPageHealthVerified": False,
                "x11SoldStateVerified": False,
                "verified": False,
                "terminal": "SOLD_UNEXPECTED_FILTER_TRANSITION",
                "marketplacePageClass": None,
                "health": health,
                "captureReady": False,
                "phaseHint": "SOLD_STATE_TRANSITION",
            }
        return {
            "soldFilterStateVerified": False,
            "soldPageHealthVerified": False,
            "x11SoldStateVerified": False,
            "verified": False,
            "terminal": None,
            "marketplacePageClass": None,
            "health": health,
            "captureReady": False,
            "phaseHint": "SOLD_STATE_VERIFICATION",
        }

    if not health.get("ok"):
        return {
            "soldFilterStateVerified": True,
            "soldPageHealthVerified": False,
            "x11SoldStateVerified": False,
            "verified": False,
            "terminal": None,
            "marketplacePageClass": health.get("marketplacePageClass"),
            "health": health,
            "captureReady": False,
            "phaseHint": PHASE_PAGE_HEALTH_VERIFICATION,
            "stableUnhealthyPending": True,
        }

    return {
        "soldFilterStateVerified": True,
        "soldPageHealthVerified": True,
        "x11SoldStateVerified": True,
        "verified": True,
        "terminal": "SOLD_STATE_VERIFIED",
        "marketplacePageClass": health.get("marketplacePageClass") or HEALTHY_SOLD_RESULTS,
        "health": health,
        "captureReady": True,
        "lhSold": True,
        "phaseHint": "SOLD_STATE_VERIFIED",
    }


def current_url_matches_card(
    *,
    url: str | None,
    expected_query: str | None,
    prior_query: str | None = None,
) -> dict[str, Any]:
    """INTER_CARD: same CDP targetId may persist; correlate via current URL/query."""
    try:
        nkw = unquote_plus((parse_qs(urlparse(str(url or "")).query).get("_nkw") or [""])[0]).lower()
    except Exception:
        nkw = ""
    exp = str(expected_query or "").lower().split()
    exp_tokens = {t for t in exp if len(t) > 1}
    got = {t for t in nkw.split() if len(t) > 1}
    overlap = len(exp_tokens & got) if exp_tokens else 0
    matches = overlap >= max(1, min(2, len(exp_tokens))) if exp_tokens else bool(nkw)
    prior_tokens = {t for t in str(prior_query or "").lower().split() if len(t) > 1}
    prior_overlap = len(prior_tokens & got) if prior_tokens else 0
    return {
        "nkw": nkw,
        "matchesCurrentQuery": matches,
        "overlapCurrent": overlap,
        "overlapPrior": prior_overlap,
        "stalePriorQuery": bool(prior_tokens) and prior_overlap >= max(1, min(2, len(prior_tokens))) and not matches,
        "validInterCardReuse": matches,
    }


def rejection_evidence_from_scored(
    scored: list[Any],
    *,
    expected_url: str | None,
    expected_query: str | None,
    fail_cls: str | None,
) -> dict[str, Any]:
    """Structured evidence when all targets are rejected (Rowlet-class)."""
    targets_seen: list[dict[str, Any]] = []
    expected_found = False
    expected_id = None
    expected_url_seen = None
    expected_title = None
    rejection_reasons: list[str] = []
    health_class = None
    for c in scored:
        d = c.to_dict() if hasattr(c, "to_dict") else dict(c)
        targets_seen.append(
            {
                "id": d.get("target_id") or d.get("targetId"),
                "url": d.get("url"),
                "title": d.get("title"),
                "score": d.get("score"),
                "reasons": d.get("reasons") or [],
            }
        )
        reasons = list(d.get("reasons") or [])
        rejection_reasons.extend(reasons)
        title_l = str(d.get("title") or "").lower()
        url_l = str(d.get("url") or "").lower()
        if "error_page" in reasons or "error page" in title_l:
            health_class = EBAY_ERROR_PAGE
            expected_found = True
            expected_id = d.get("target_id") or d.get("targetId")
            expected_url_seen = d.get("url")
            expected_title = d.get("title")
        elif "sorry_error" in reasons or ("sorry" in title_l and "ebay" in title_l):
            health_class = health_class or EBAY_SORRY_PAGE
            expected_found = True
            expected_id = expected_id or d.get("target_id") or d.get("targetId")
            expected_url_seen = expected_url_seen or d.get("url")
            expected_title = expected_title or d.get("title")
        elif "challenge" in reasons:
            health_class = health_class or EBAY_CHALLENGE_PAGE
            expected_found = True
        # Expected URL / nkw correlation even when score rejected.
        if expected_url and url_l and (
            "lh_sold=1" in url_l
            and (
                str(expected_url).lower().split("#")[0] == url_l.split("#")[0]
                or current_url_matches_card(url=url_l, expected_query=expected_query).get("matchesCurrentQuery")
            )
        ):
            expected_found = True
            expected_id = expected_id or d.get("target_id") or d.get("targetId")
            expected_url_seen = expected_url_seen or d.get("url")
            expected_title = expected_title or d.get("title")

    terminal = fail_cls
    if health_class == EBAY_ERROR_PAGE:
        terminal = MARKETPLACE_ERROR_PAGE
    elif health_class == EBAY_SORRY_PAGE:
        terminal = MARKETPLACE_ERROR_PAGE  # still marketplace; distinct page class in evidence
    elif health_class == EBAY_CHALLENGE_PAGE:
        terminal = TARGET_REJECTED_UNHEALTHY_PAGE

    return {
        "targetCount": len(targets_seen),
        "targetsSeen": targets_seen,
        "expectedTargetFound": expected_found,
        "expectedTargetId": expected_id,
        "expectedTargetUrl": expected_url_seen,
        "expectedTargetTitle": expected_title,
        "rejectionReasons": sorted(set(rejection_reasons)),
        "healthClassification": health_class,
        "terminalClass": terminal,
        "failureClass": terminal,
    }
