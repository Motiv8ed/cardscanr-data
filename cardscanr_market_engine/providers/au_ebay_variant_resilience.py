"""AU live eBay: query-variant pacing, generic Error Page retry/fallback, cap truth."""
from __future__ import annotations

import os
import random
import re
from typing import Any

from ..models import ProviderRequest, ProviderResult
from .errors import ProviderTemporaryError
from .sold_page_health import (
    EBAY_ERROR_PAGE,
    _is_challenge_blob,
    _is_classic_sorry_blob,
    classify_marketplace_page_class,
    is_ebay_error_page,
    is_tiny_ebay_generic_error_body,
)

LIVE_EBAY_NAVIGATION_COUNT = "liveEbayNavigationCount"


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


def _has_hard_stop_markers(
    *,
    title: str | None,
    url: str | None,
    body: str | None = None,
    challenge: bool = False,
    http_status: int | None = None,
) -> bool:
    """Classic sorry/challenge/captcha/429 — never generic-error retry/fallback."""
    if challenge:
        return True
    t = str(title or "").lower()
    u = str(url or "").lower()
    b = str(body or "").lower()
    blob = f"{t}\n{u}\n{b}"
    if re.search(r"\b429\b", blob) or "too many requests" in blob:
        return True
    if _is_challenge_blob(title=title, url=url, body=body):
        return True
    if "splashui/challenge" in u:
        return True
    if "verify yourself" in t or "security measure" in t:
        return True
    # Error Page | eBay + tiny "something went wrong" body is generic even if the headline says Sorry.
    if _is_classic_sorry_blob(title=title, url=url, body=body):
        return True
    if is_ebay_error_page(title=title, url=url, body=body):
        return False
    return False


def _has_positive_generic_error_markers(
    *,
    title: str | None,
    url: str | None,
    body: str | None = None,
    http_status: int | None = None,
    reason: str | None = None,
) -> bool:
    if is_ebay_error_page(title=title, url=url, body=body):
        return True
    if is_tiny_ebay_generic_error_body(body):
        return True
    if http_status == 403 and is_tiny_ebay_generic_error_body(body):
        return True
    if str(reason or "").strip().lower() in {"ebay_error_page", "marketplace_error_page"}:
        return True
    page_class = classify_marketplace_page_class(
        url=url, title=title, body=body, http_status=http_status
    )
    return page_class == EBAY_ERROR_PAGE


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
    """Generic Error Page: positive title/body markers; not classic sorry/challenge."""
    _ = (error, sorry)
    if _has_hard_stop_markers(
        title=title,
        url=url,
        body=body,
        challenge=challenge,
        http_status=http_status,
    ):
        return False
    return _has_positive_generic_error_markers(
        title=title,
        url=url,
        body=body,
        http_status=http_status,
        reason=reason,
    )


def is_generic_error_page_failure(exc: ProviderTemporaryError) -> bool:
    diag = dict(getattr(exc, "diagnostics", {}) or {})
    nested = diag.get("desktopNav") if isinstance(diag.get("desktopNav"), dict) else {}
    search = nested.get("search") if isinstance(nested.get("search"), dict) else {}
    sold = nested.get("sold") if isinstance(nested.get("sold"), dict) else {}
    title = str(
        diag.get("title")
        or nested.get("title")
        or sold.get("title")
        or search.get("title")
        or ""
    )
    url = str(diag.get("url") or nested.get("url") or sold.get("url") or search.get("url") or "")
    body = str(diag.get("body") or sold.get("body") or sold.get("bodySample") or "")
    reason = str(diag.get("reason") or diag.get("failureClass") or nested.get("error") or "")
    return is_generic_error_page_nav(
        title=title,
        url=url,
        body=body,
        error=str(nested.get("error") or sold.get("error") or reason or exc),
        sorry=bool(nested.get("sorry") or sold.get("sorry") or search.get("sorry")),
        challenge=bool(nested.get("challenge") or sold.get("challenge") or search.get("challenge")),
        http_status=diag.get("httpStatus") if isinstance(diag.get("httpStatus"), int) else None,
        reason=reason,
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
    return 0


def merge_live_navigation_into_diagnostics(
    diagnostics: dict[str, Any] | None,
    *,
    navigation_count: int,
) -> dict[str, Any]:
    merged = dict(diagnostics or {})
    merged[LIVE_EBAY_NAVIGATION_COUNT] = max(0, int(navigation_count))
    return merged


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
    "merge_live_navigation_into_diagnostics",
    "is_generic_error_page_failure",
    "is_generic_error_page_nav",
    "is_tiny_ebay_generic_error_body",
    "min_comparable_count_for_variant_stop",
    "should_stop_query_variants_after_healthy_capture",
    "sold_state_verification_blocked",
    "variant_inter_attempt_delay_seconds",
]