"""AU live eBay: query-variant pacing, generic Error Page retry/fallback, cap truth."""
from __future__ import annotations

import os
import random
import re
from typing import Any

from ..models import ProviderRequest, ProviderResult
from .errors import ProviderTemporaryError
from .sold_page_health import (
    EBAY_ACCESS_DENIED_403,
    EBAY_ERROR_PAGE,
    EBAY_SORRY_PAGE,
    classify_marketplace_page_class,
    is_ebay_error_page,
    is_tiny_ebay_generic_error_body,
)

LIVE_EBAY_NAVIGATION_COUNT = "liveEbayNavigationCount"

_GENERIC_SORRY_MARKERS = (
    "sorry",
    "challenge",
    "captcha",
    "verify yourself",
    "429",
    "too many requests",
)


def au_variant_resilience_enabled(*, market_country: str | None) -> bool:
    return str(market_country or "").upper() == "AU"


def sold_state_verification_blocked(
    *,
    title: str | None = None,
    url: str | None = None,
    body: str | None = None,
    http_status: int | None = None,
) -> bool:
    """LH_Sold=1 alone must never verify when the page is a generic Error Page."""
    t = (title or "").strip()
    if t == "Error Page | eBay" or is_ebay_error_page(title=title, url=url, body=body):
        return True
    if is_tiny_ebay_generic_error_body(body):
        return True
    page_class = classify_marketplace_page_class(
        url=url, title=title, body=body, http_status=http_status
    )
    return page_class == EBAY_ERROR_PAGE


def _jitter_seconds(lo: int, hi: int) -> float:
    return float(random.randint(lo, hi))


def variant_inter_attempt_delay_seconds() -> float:
    lo = int(os.getenv("AU_EBAY_VARIANT_GAP_SECONDS_MIN", "45"))
    hi = int(os.getenv("AU_EBAY_VARIANT_GAP_SECONDS_MAX", "90"))
    if hi < lo:
        hi = lo
    return _jitter_seconds(lo, hi)


def generic_error_page_retry_delay_seconds() -> float:
    lo = int(os.getenv("AU_EBAY_GENERIC_ERROR_RETRY_SECONDS_MIN", "30"))
    hi = int(os.getenv("AU_EBAY_GENERIC_ERROR_RETRY_SECONDS_MAX", "60"))
    if hi < lo:
        hi = lo
    return _jitter_seconds(lo, hi)


def min_comparable_count_for_variant_stop() -> int:
    return max(1, int(os.getenv("AU_EBAY_MIN_COMPARABLE_COUNT", "2")))


def _blob_has_disallowed_markers(*parts: str | None) -> bool:
    blob = "\n".join(str(p or "") for p in parts).lower()
    if re.search(r"\b429\b", blob):
        return True
    return any(marker in blob for marker in _GENERIC_SORRY_MARKERS if marker != "429")


def is_generic_error_page_nav(
    *,
    title: str | None,
    url: str | None,
    body: str | None = None,
    error: str | None = None,
    sorry: bool = False,
    challenge: bool = False,
    http_status: int | None = None,
    reason: str | None = None,
) -> bool:
    """Generic Error Page only — not classic SORRY/challenge/captcha/429."""
    if challenge:
        return False
    if _blob_has_disallowed_markers(title, body, error, reason) and not is_ebay_error_page(
        title=title, url=url, body=body
    ):
        # Classic SORRY body still present — not generic error-only retry class.
        if EBAY_SORRY_PAGE == classify_marketplace_page_class(
            url=url, title=title, body=body, http_status=http_status
        ):
            return False
    page_class = classify_marketplace_page_class(
        url=url, title=title, body=body, http_status=http_status
    )
    if page_class == EBAY_ACCESS_DENIED_403 and is_tiny_ebay_generic_error_body(body):
        if not _blob_has_disallowed_markers(title, body, error):
            return True
    if page_class == EBAY_ERROR_PAGE:
        if sorry and not is_ebay_error_page(title=title, url=url, body=body):
            return False
        if _blob_has_disallowed_markers(title, body, error, reason):
            # Error-page title with explicit sorry/challenge text → no generic-only retry.
            if "sorry" in str(title or "").lower() or "sorry" in str(body or "").lower():
                return False
            if "captcha" in str(title or "").lower() or "challenge" in str(body or "").lower():
                return False
        return True
    if str(reason or "").strip().lower() in {"ebay_error_page", "marketplace_error_page"}:
        return not _blob_has_disallowed_markers(title, body, error, reason)
    if is_ebay_error_page(title=title, url=url, body=body):
        return not _blob_has_disallowed_markers(title, body, error, reason)
    return False


def is_generic_error_page_failure(exc: ProviderTemporaryError) -> bool:
    diag = dict(getattr(exc, "diagnostics", {}) or {})
    nested = diag.get("desktopNav") if isinstance(diag.get("desktopNav"), dict) else {}
    search = nested.get("search") if isinstance(nested.get("search"), dict) else {}
    sold = nested.get("sold") if isinstance(nested.get("sold"), dict) else {}
    title = str(
        nested.get("title")
        or sold.get("title")
        or search.get("title")
        or diag.get("title")
        or ""
    )
    url = str(nested.get("url") or sold.get("url") or search.get("url") or diag.get("url") or "")
    body = str(diag.get("body") or sold.get("body") or "")
    return is_generic_error_page_nav(
        title=title,
        url=url,
        body=body,
        error=str(diag.get("reason") or exc),
        sorry=bool(nested.get("sorry") or search.get("sorry")),
        challenge=bool(nested.get("challenge") or search.get("challenge")),
        reason=str(diag.get("reason") or diag.get("failureClass") or ""),
    )


def attempt_has_healthy_verified_sold_capture(result: ProviderResult) -> bool:
    meta = result.raw_metadata if isinstance(result.raw_metadata, dict) else {}
    stage = meta.get("stageTimings") if isinstance(meta.get("stageTimings"), dict) else {}
    if meta.get("x11SoldStateVerified") or stage.get("x11SoldStateVerified"):
        if sold_state_verification_blocked(
            title=str((meta.get("desktopNav") or {}).get("title") or ""),
            url=str((meta.get("desktopNav") or {}).get("url") or ""),
        ):
            return False
        return True
    desktop = meta.get("desktopNav") if isinstance(meta.get("desktopNav"), dict) else {}
    if not desktop and isinstance(stage.get("desktopNav"), dict):
        desktop = stage.get("desktopNav")  # type: ignore[assignment]
    if desktop.get("SOLD_STATE_VERIFIED") and not desktop.get("sorry") and not desktop.get("challenge"):
        if sold_state_verification_blocked(
            title=str(desktop.get("title") or ""),
            url=str(desktop.get("url") or ""),
            body=str(desktop.get("bodyChars") or ""),
        ):
            return False
        return True
    post = str(meta.get("postSoldCapturePhase") or stage.get("postSoldCapturePhase") or "")
    return post == "POST_SOLD_CAPTURE_READY"


def comparable_included_count(result: ProviderResult, request: ProviderRequest) -> int:
    meta = result.raw_metadata if isinstance(result.raw_metadata, dict) else {}
    for key in ("cleanIncludedCount", "clean_included_count", "includedCount"):
        raw = meta.get(key)
        if raw is not None:
            try:
                return max(0, int(raw))
            except (TypeError, ValueError):
                pass
    progress = meta.get("attemptProgress")
    if isinstance(progress, list) and progress:
        try:
            return max(0, int(progress[-1].get("cleanIncludedCount") or 0))
        except (TypeError, ValueError, AttributeError):
            pass
    return max(0, len(result.comps))


def should_stop_query_variants_after_healthy_capture(
    *,
    last_healthy: tuple[Any, ProviderResult] | None,
    request: ProviderRequest,
) -> bool:
    if last_healthy is None:
        return False
    _query, result = last_healthy
    if not attempt_has_healthy_verified_sold_capture(result):
        return False
    return comparable_included_count(result, request) >= min_comparable_count_for_variant_stop()


def additional_submission_slots_from_navigation_count(navigation_count: int) -> int:
    count = max(0, int(navigation_count or 0))
    if count <= 1:
        return 0
    return count - 1


def extra_live_navigations_from_metadata(meta: dict[str, Any] | None) -> int:
    if not isinstance(meta, dict):
        return 0
    count = int(meta.get(LIVE_EBAY_NAVIGATION_COUNT) or 0)
    return additional_submission_slots_from_navigation_count(count)


def annotate_variant_fallback_metadata(
    result: ProviderResult,
    *,
    failed_query_text: str,
    failure_reason: str,
) -> ProviderResult:
    meta = dict(result.raw_metadata or {})
    meta["auVariantGenericErrorFallback"] = True
    meta["auVariantFallbackFromQuery"] = failed_query_text
    meta["auVariantFallbackReason"] = failure_reason
    return ProviderResult(
        provider_name=result.provider_name,
        marketplace=result.marketplace,
        provider_fingerprint=result.provider_fingerprint,
        query_used=result.query_used,
        comps=result.comps,
        raw_metadata=meta,
    )


__all__ = [
    "LIVE_EBAY_NAVIGATION_COUNT",
    "additional_submission_slots_from_navigation_count",
    "annotate_variant_fallback_metadata",
    "attempt_has_healthy_verified_sold_capture",
    "au_variant_resilience_enabled",
    "comparable_included_count",
    "extra_live_navigations_from_metadata",
    "generic_error_page_retry_delay_seconds",
    "is_generic_error_page_failure",
    "is_generic_error_page_nav",
    "is_tiny_ebay_generic_error_body",
    "min_comparable_count_for_variant_stop",
    "should_stop_query_variants_after_healthy_capture",
    "sold_state_verification_blocked",
    "variant_inter_attempt_delay_seconds",
]