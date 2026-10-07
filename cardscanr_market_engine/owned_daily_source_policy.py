"""Source-aware freshness policy for owned_daily eBay scheduling.

Authoritative concepts
----------------------
HAS_ANY_PRICE              current_market_price > 0
HAS_VERIFIED_LOCAL_PRICE   display/provider indicates verified eBay sold
HAS_RECENT_REFERENCE_PRICE reference-tier selected price exists
HAS_EBAY_VERIFIED_PRICE    alias of HAS_VERIFIED_LOCAL_PRICE
DUE_FOR_EBAY_VERIFICATION  owned_daily should enqueue an eBay browser job

Authoritative eBay-success freshness timestamp
---------------------------------------------
For verified-local rows: ``last_updated_at`` (the verified estimate clock).
``stale_after`` / ``next_refresh_due_at`` are cache TTL hints for the *selected*
price row (often a short reference TTL) and MUST NOT alone mark owned_daily
eBay work as P1_STALE_GT_24H.

For reference-only rows: there is no eBay-success freshness. The identity remains
due for first verified-local pricing even if the reference stamp is recent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from .bulk.price_semantics import is_reference_provider, is_verified_provider
from .price_source_precedence import (
    REFERENCE_DISPLAY_SOURCES,
    STRUCTURED_FALLBACK_SOURCES,
    VERIFIED_DISPLAY_SOURCES,
)
from .scheduler import _parse_utc, utc_iso
from .x11_chrome_focus import is_local_runtime_failure_message

SourceClass = Literal["none", "reference_only", "structured_fallback", "verified_local"]


def classify_owned_price_source(
    *,
    current_market_price: Any,
    display_price_source: Any = None,
    provider: Any = None,
) -> SourceClass:
    try:
        price = float(current_market_price) if current_market_price is not None else None
    except (TypeError, ValueError):
        price = None
    if price is None or price <= 0:
        return "none"
    source = str(display_price_source or "").strip().lower()
    prov = str(provider or "").strip().lower()

    # Explicit display attribution is authoritative.
    if source in REFERENCE_DISPLAY_SOURCES:
        return "reference_only"
    if source in STRUCTURED_FALLBACK_SOURCES:
        return "structured_fallback"
    if source in VERIFIED_DISPLAY_SOURCES:
        return "verified_local"

    # Provider heuristics when display_price_source is absent.
    if is_reference_provider(prov):
        return "reference_only"
    if is_verified_provider(prov):
        return "verified_local"

    # Legacy priced rows with neither display nor provider attribution: treat the
    # selected price clock (last_updated_at) as the success freshness basis — the
    # historical owned_daily behaviour before source-aware bands.
    if not source and not prov:
        return "verified_local"

    # Unknown named provider with a price: never above verified; due for eBay verify.
    return "reference_only"


@dataclass(frozen=True)
class OwnedDailyFreshnessView:
    source_class: SourceClass
    has_any_price: bool
    has_verified_local_price: bool
    has_reference_only_price: bool
    ebay_success_freshness_at: datetime | None
    scheduler_age_hours: float | None
    stale_after: datetime | None
    last_updated_at: datetime | None
    authoritative_timestamp_field: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceClass": self.source_class,
            "hasAnyPrice": self.has_any_price,
            "hasVerifiedLocalPrice": self.has_verified_local_price,
            "hasReferenceOnlyPrice": self.has_reference_only_price,
            "ebaySuccessFreshnessAt": utc_iso(self.ebay_success_freshness_at)
            if self.ebay_success_freshness_at
            else None,
            "schedulerAgeHours": self.scheduler_age_hours,
            "staleAfter": utc_iso(self.stale_after) if self.stale_after else None,
            "lastUpdatedAt": utc_iso(self.last_updated_at) if self.last_updated_at else None,
            "authoritativeTimestampField": self.authoritative_timestamp_field,
        }


def build_owned_daily_freshness_view(
    target: dict[str, Any],
    *,
    now: datetime,
) -> OwnedDailyFreshnessView:
    price_raw = target.get("current_market_price")
    try:
        price = float(price_raw) if price_raw is not None else None
    except (TypeError, ValueError):
        price = None
    source_class = classify_owned_price_source(
        current_market_price=price,
        display_price_source=target.get("display_price_source"),
        provider=target.get("provider"),
    )
    last_updated = _parse_utc(target.get("last_updated_at"))
    stale_after = _parse_utc(target.get("stale_after"))
    has_any = price is not None and price > 0
    has_verified = source_class == "verified_local"
    has_reference_only = source_class == "reference_only"

    if has_verified:
        ebay_success = last_updated
        field = "last_updated_at"
    elif source_class == "structured_fallback":
        # Structured intl fallback is not verified-local eBay sold for home market.
        ebay_success = None
        field = "none_pending_verified_local"
    elif has_reference_only:
        ebay_success = None
        field = "none_reference_only"
    else:
        ebay_success = None
        field = "none_unpriced"

    age = None
    if ebay_success is not None:
        age = max(0.0, (now - ebay_success).total_seconds() / 3600.0)
    elif last_updated is not None and has_any:
        # Informational age of the selected (non-verified) stamp — not eBay success.
        age = max(0.0, (now - last_updated).total_seconds() / 3600.0)

    return OwnedDailyFreshnessView(
        source_class=source_class,
        has_any_price=has_any,
        has_verified_local_price=has_verified,
        has_reference_only_price=has_reference_only,
        ebay_success_freshness_at=ebay_success,
        scheduler_age_hours=age,
        stale_after=stale_after,
        last_updated_at=last_updated,
        authoritative_timestamp_field=field,
    )


def classify_owned_daily_band(
    target: dict[str, Any],
    *,
    now: datetime,
    success_fresh_hours: int = 24,
) -> tuple[str, bool, str, OwnedDailyFreshnessView, dict[str, Any]]:
    """Return (band, due, reason_suffix, freshness_view, extra_details).

    Bands (owned_daily eBay enqueue):
      P0_NEVER_PRICED            — no current_market_price at all
      P0_NEEDS_VERIFIED_LOCAL    — has reference/fallback price, never verified eBay
      P2_FAILED_RETRY            — refresh_status=failed and retry due
      P1_STALE_GT_24H            — verified-local success age >= success_fresh_hours
      P3_APPROACHING_DUE         — verified-local within final 2h of fresh window
      FRESH_SKIP                 — verified-local still fresh
    """
    view = build_owned_daily_freshness_view(target, now=now)
    refresh_status = str(target.get("refresh_status") or "").strip().lower()
    next_due = _parse_utc(target.get("next_refresh_due_at"))
    failed_retry_due = refresh_status == "failed" and (next_due is None or next_due <= now)
    extras: dict[str, Any] = {
        "source_class": view.source_class,
        "ebay_success_freshness_at": utc_iso(view.ebay_success_freshness_at)
        if view.ebay_success_freshness_at
        else None,
        "authoritative_timestamp_field": view.authoritative_timestamp_field,
        "stale_after_ignored_for_band": True,
        "stale_after": utc_iso(view.stale_after) if view.stale_after else None,
    }

    if not view.has_any_price:
        # Honor failure-policy backoff (e.g. NO_PRICE_EVER_FOUND → next_refresh_due_at).
        # Without this, canaries loop forever on the same sparse P0 identity.
        if next_due is not None and next_due > now:
            return "P0_NEVER_PRICED", False, "p0_never_priced_backoff", view, extras
        return "P0_NEVER_PRICED", True, "p0_never_priced", view, extras

    if view.source_class in {"reference_only", "structured_fallback"}:
        # Recent reference must not FRESH_SKIP eBay verification.
        # Local X11/runtime failures may set refresh_status=failed but must not
        # demote this identity away from P0_NEEDS_VERIFIED_LOCAL.
        if next_due is not None and next_due > now and refresh_status == "failed":
            return (
                "P0_NEEDS_VERIFIED_LOCAL",
                False,
                "p0_needs_verified_local_backoff",
                view,
                extras,
            )
        return (
            "P0_NEEDS_VERIFIED_LOCAL",
            True,
            "p0_needs_verified_local",
            view,
            extras,
        )

    # Verified-local path — authoritative clock is last_updated_at only.
    ebay_success = view.ebay_success_freshness_at
    success_stale = ebay_success is None or ebay_success <= (
        now - timedelta(hours=success_fresh_hours)
    )
    approaching = (
        not success_stale
        and ebay_success is not None
        and ebay_success
        <= (now - timedelta(hours=max(1, success_fresh_hours - 2)))
    )

    # Local infrastructure failures (X11 focus, etc.) are machine-global, not
    # evidence that this printing is hard to price — do not escalate to P2.
    local_runtime_failure = is_local_runtime_failure_message(target.get("last_error_message"))
    extras["local_runtime_failure"] = local_runtime_failure

    if failed_retry_due and not local_runtime_failure:
        return "P2_FAILED_RETRY", True, "p2_failed_retry", view, extras
    if success_stale:
        return "P1_STALE_GT_24H", True, "p1_stale_gt_24h", view, extras
    if approaching:
        return "P3_APPROACHING_DUE", True, "p3_approaching_due", view, extras
    return "FRESH_SKIP", False, "fresh_lt_24h", view, extras


__all__ = [
    "OwnedDailyFreshnessView",
    "SourceClass",
    "build_owned_daily_freshness_view",
    "classify_owned_daily_band",
    "classify_owned_price_source",
]
