"""Customer-facing market price source precedence.

CardScanR rule: verified eBay sold estimates are PRIMARY. Reference/static
providers may fill gaps and store secondary observations, but must not silently
overwrite a valid higher-tier selected market estimate merely because they
refreshed later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from .bulk.price_semantics import (
    REFERENCE_PROVIDERS,
    VERIFIED_PROVIDERS,
    is_reference_provider,
    is_verified_provider,
)

SourceTier = Literal[1, 2, 3, 4, 5]

# Tier 1/2: verified eBay sold. Tier 3: structured market fallback (intl).
# Tier 4: reference/static. Tier 5: unavailable.
TIER_FRESH_EBAY = 1
TIER_STALE_EBAY = 2
TIER_STRUCTURED_FALLBACK = 3
TIER_REFERENCE = 4
TIER_UNAVAILABLE = 5

VERIFIED_DISPLAY_SOURCES = frozenset(
    {"verified_au", "verified_local", "local_verified", "market"}
)
STRUCTURED_FALLBACK_SOURCES = frozenset({"international_estimate"})
REFERENCE_DISPLAY_SOURCES = frozenset({"reference", "pending_verification"})

# How long a valid eBay sold estimate remains the selected primary even if stale.
DEFAULT_EBAY_PRIMARY_MAX_AGE_HOURS = 168  # 7 days


@dataclass(frozen=True)
class PriceObservation:
    """One provider observation candidate for selection."""

    provider: str | None
    price: float | None
    observed_at: datetime | None
    confidence: str | None = None
    sample_size: int | None = None
    marketplace: str | None = None
    display_source: str | None = None
    snapshot_id: str | None = None
    is_invalidated: bool = False


@dataclass(frozen=True)
class SelectedMarketPrice:
    tier: SourceTier
    price: float | None
    provider: str | None
    marketplace: str | None
    display_source: str
    confidence: str | None
    sample_size: int | None
    observed_at: datetime | None
    snapshot_id: str | None
    freshness: Literal["fresh", "stale", "unavailable"]
    source_disclosure: str | None
    reason: str
    diagnostics: dict[str, Any]


def _parse_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value is False:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def provider_tier(
    *,
    provider: str | None,
    display_source: str | None = None,
    observed_at: datetime | None = None,
    now: datetime | None = None,
    ebay_primary_max_age_hours: int = DEFAULT_EBAY_PRIMARY_MAX_AGE_HOURS,
    is_invalidated: bool = False,
) -> SourceTier:
    """Return precedence tier for a provider observation (lower is better)."""
    if is_invalidated:
        return TIER_UNAVAILABLE
    now = now or datetime.now(timezone.utc)
    source = str(display_source or "").strip().lower()
    # Explicit structured-fallback attribution wins over provider name (intl uses ebay_browser).
    if source in STRUCTURED_FALLBACK_SOURCES:
        return TIER_STRUCTURED_FALLBACK
    if is_verified_provider(provider) or source in VERIFIED_DISPLAY_SOURCES:
        if observed_at is None:
            return TIER_STALE_EBAY
        age = now - observed_at
        if age <= timedelta(hours=max(1, int(ebay_primary_max_age_hours))):
            # Within documented primary window: fresh if <24h else stale-but-primary.
            if age <= timedelta(hours=24):
                return TIER_FRESH_EBAY
            return TIER_STALE_EBAY
        return TIER_UNAVAILABLE  # too old to remain automatic primary
    if str(provider or "").startswith("international"):
        return TIER_STRUCTURED_FALLBACK
    if is_reference_provider(provider) or source in REFERENCE_DISPLAY_SOURCES:
        return TIER_REFERENCE
    if source == "unavailable" or not provider:
        return TIER_UNAVAILABLE
    # Unknown non-verified providers are treated as reference-tier, never above eBay.
    return TIER_REFERENCE


def display_source_for_tier(tier: SourceTier, *, market_country: str | None = None) -> str:
    if tier in {TIER_FRESH_EBAY, TIER_STALE_EBAY}:
        if str(market_country or "").strip().upper() == "AU":
            return "verified_au"
        return "verified_local"
    if tier == TIER_STRUCTURED_FALLBACK:
        return "international_estimate"
    if tier == TIER_REFERENCE:
        return "reference"
    return "unavailable"


def disclosure_for_selection(selection: SelectedMarketPrice) -> str | None:
    if selection.tier in {TIER_FRESH_EBAY, TIER_STALE_EBAY}:
        return None
    if selection.tier == TIER_STRUCTURED_FALLBACK:
        return "eBay sold data unavailable. Showing international market estimate."
    if selection.tier == TIER_REFERENCE:
        provider = selection.provider or "reference"
        return f"eBay sold data unavailable. Showing {provider} reference estimate."
    return "No reliable market estimate available."


def select_customer_market_price(
    observations: list[PriceObservation],
    *,
    now: datetime | None = None,
    ebay_primary_max_age_hours: int = DEFAULT_EBAY_PRIMARY_MAX_AGE_HOURS,
    market_country: str | None = None,
) -> SelectedMarketPrice:
    """Deterministically select the customer-facing market estimate."""
    now = now or datetime.now(timezone.utc)
    usable: list[tuple[SourceTier, PriceObservation, datetime]] = []
    for obs in observations:
        price = _float_or_none(obs.price)
        if price is None:
            continue
        observed_at = obs.observed_at
        tier = provider_tier(
            provider=obs.provider,
            display_source=obs.display_source,
            observed_at=observed_at,
            now=now,
            ebay_primary_max_age_hours=ebay_primary_max_age_hours,
            is_invalidated=obs.is_invalidated,
        )
        if tier == TIER_UNAVAILABLE:
            continue
        sort_ts = observed_at or datetime(1970, 1, 1, tzinfo=timezone.utc)
        usable.append((tier, obs, sort_ts))

    if not usable:
        return SelectedMarketPrice(
            tier=TIER_UNAVAILABLE,
            price=None,
            provider=None,
            marketplace=None,
            display_source="unavailable",
            confidence=None,
            sample_size=None,
            observed_at=None,
            snapshot_id=None,
            freshness="unavailable",
            source_disclosure="No reliable market estimate available.",
            reason="no_usable_observation",
            diagnostics={"candidateCount": len(observations)},
        )

    usable.sort(key=lambda item: (item[0], -item[2].timestamp()))
    tier, obs, _ = usable[0]
    observed_at = obs.observed_at
    freshness: Literal["fresh", "stale", "unavailable"] = "fresh"
    if tier == TIER_STALE_EBAY:
        freshness = "stale"
    elif observed_at is not None and (now - observed_at) > timedelta(hours=24):
        freshness = "stale"
    display_source = display_source_for_tier(tier, market_country=market_country)
    selected = SelectedMarketPrice(
        tier=tier,
        price=_float_or_none(obs.price),
        provider=obs.provider,
        marketplace=obs.marketplace,
        display_source=display_source,
        confidence=obs.confidence,
        sample_size=obs.sample_size,
        observed_at=observed_at,
        snapshot_id=obs.snapshot_id,
        freshness=freshness,
        source_disclosure=None,
        reason=f"selected_tier_{tier}",
        diagnostics={
            "candidateCount": len(observations),
            "usableCount": len(usable),
            "ebayPrimaryMaxAgeHours": ebay_primary_max_age_hours,
        },
    )
    return SelectedMarketPrice(
        tier=selected.tier,
        price=selected.price,
        provider=selected.provider,
        marketplace=selected.marketplace,
        display_source=selected.display_source,
        confidence=selected.confidence,
        sample_size=selected.sample_size,
        observed_at=selected.observed_at,
        snapshot_id=selected.snapshot_id,
        freshness=selected.freshness,
        source_disclosure=disclosure_for_selection(selected),
        reason=selected.reason,
        diagnostics=selected.diagnostics,
    )


def can_proposed_replace_selected(
    *,
    current_provider: str | None,
    current_price: Any,
    current_display_source: str | None,
    current_observed_at: Any,
    proposed_provider: str | None,
    proposed_price: Any,
    proposed_display_source: str | None = None,
    proposed_observed_at: Any = None,
    now: datetime | None = None,
    ebay_primary_max_age_hours: int = DEFAULT_EBAY_PRIMARY_MAX_AGE_HOURS,
    current_invalidated: bool = False,
) -> tuple[bool, str]:
    """Write-guard helper: False means keep current selected estimate."""
    now = now or datetime.now(timezone.utc)
    current = PriceObservation(
        provider=current_provider,
        price=_float_or_none(current_price),
        observed_at=_parse_utc(current_observed_at),
        display_source=current_display_source,
        is_invalidated=current_invalidated,
    )
    proposed = PriceObservation(
        provider=proposed_provider,
        price=_float_or_none(proposed_price),
        observed_at=_parse_utc(proposed_observed_at) or now,
        display_source=proposed_display_source,
    )
    selected = select_customer_market_price(
        [current, proposed],
        now=now,
        ebay_primary_max_age_hours=ebay_primary_max_age_hours,
    )
    proposed_tier = provider_tier(
        provider=proposed.provider,
        display_source=proposed.display_source,
        observed_at=proposed.observed_at,
        now=now,
        ebay_primary_max_age_hours=ebay_primary_max_age_hours,
    )
    if selected.price is None:
        return False, "no_selected_price"
    if proposed.price is None:
        return False, "proposed_invalid"
    # Allow replace only when proposed is the selected winner.
    same_provider = str(selected.provider or "").lower() == str(proposed.provider or "").lower()
    same_price = abs(float(selected.price) - float(proposed.price)) < 0.001
    if same_provider and same_price and selected.tier == proposed_tier:
        return True, selected.reason
    return False, f"blocked_by_tier_{selected.tier}_keeps_{selected.provider}"


def observations_from_cache_and_snapshots(
    *,
    cache: dict[str, Any] | None,
    snapshots: list[dict[str, Any]],
    now: datetime | None = None,
) -> list[PriceObservation]:
    """Build selection candidates from cache row + snapshot history."""
    now = now or datetime.now(timezone.utc)
    out: list[PriceObservation] = []
    if cache:
        out.append(
            PriceObservation(
                provider=str(cache.get("provider") or "") or None,
                price=_float_or_none(cache.get("current_market_price")),
                observed_at=_parse_utc(cache.get("last_updated_at")),
                confidence=str(cache.get("confidence") or "") or None,
                sample_size=int(cache.get("sample_size") or 0) or None,
                marketplace=str(cache.get("marketplace") or "") or None,
                display_source=str(cache.get("display_price_source") or "") or None,
                snapshot_id=str(cache.get("latest_snapshot_id") or "") or None,
            )
        )
        ref_price = _float_or_none(cache.get("reference_price"))
        if ref_price is not None:
            out.append(
                PriceObservation(
                    provider=str(cache.get("reference_provider") or "reference") or "reference",
                    price=ref_price,
                    observed_at=_parse_utc(cache.get("reference_updated_at")) or now,
                    confidence=str(cache.get("confidence") or "") or None,
                    sample_size=1,
                    marketplace="REFERENCE",
                    display_source="reference",
                )
            )
    for snap in snapshots:
        out.append(
            PriceObservation(
                provider=str(snap.get("provider") or "") or None,
                price=_float_or_none(snap.get("recommended_price") if snap.get("recommended_price") is not None else snap.get("median_price")),
                observed_at=_parse_utc(snap.get("created_at")),
                confidence=str(snap.get("confidence") or "") or None,
                sample_size=int(snap.get("sample_size") or 0) or None,
                marketplace=str(snap.get("marketplace") or "") or None,
                display_source=(
                    "reference"
                    if is_reference_provider(str(snap.get("provider") or ""))
                    else ("verified_local" if is_verified_provider(str(snap.get("provider") or "")) else None)
                ),
                snapshot_id=str(snap.get("id") or "") or None,
            )
        )
    return out


__all__ = [
    "DEFAULT_EBAY_PRIMARY_MAX_AGE_HOURS",
    "PriceObservation",
    "REFERENCE_PROVIDERS",
    "SelectedMarketPrice",
    "SourceTier",
    "TIER_FRESH_EBAY",
    "TIER_REFERENCE",
    "TIER_STALE_EBAY",
    "TIER_STRUCTURED_FALLBACK",
    "TIER_UNAVAILABLE",
    "VERIFIED_PROVIDERS",
    "can_proposed_replace_selected",
    "display_source_for_tier",
    "observations_from_cache_and_snapshots",
    "provider_tier",
    "select_customer_market_price",
]
