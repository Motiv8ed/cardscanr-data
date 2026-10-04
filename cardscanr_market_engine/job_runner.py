from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import os
import time
from typing import Any

from .cache_writer import build_cache_payload
from .config import MarketEngineConfig
from .currency_conversion import CurrencyConversion, resolve_currency_conversion
from .failure_policy import build_failure_policy, failure_policy_diagnostics
from .price_movement_guard import evaluate_price_movement, movement_diagnostics
from .filters import filter_comps
from .marketplaces import LocalMarketConfig, ebay_marketplace_fallback_order, resolve_marketplace_config
from .models import (
    EvaluatedComp,
    MarketPriceKey,
    MarketPriceRefreshJob,
    PricingStats,
    ProviderRequest,
    ProviderResult,
)
from .pricing_stats import calculate_pricing_stats
from .providers.errors import (
    ProviderAuthenticationRequiredError,
    ProviderBlockedError,
    ProviderError,
    ProviderMarketplaceMismatchError,
    ProviderUnsupportedMarketError,
    sanitize_provider_diagnostics,
)
from .navigation_runtime_context import (
    apply_context_to_environ,
    load_navigation_runtime_context,
    prepare_context_for_market,
)
from .scheduler import parse_market_allowlist
from .marketplace_ops_state import (
    get_active_cooldown,
    maybe_record_failure_cooldown,
)
from .atomic_json_state import AtomicStateError
from .ebay_availability import (
    EBAY_AVAILABILITY_COOLDOWN,
    EBAY_CHALLENGE_REQUIRED as AVAIL_CHALLENGE,
    begin_probe,
    browser_work_allowed,
    record_challenge,
    record_healthy_browser_check,
    record_sorry,
    release_probe_local_failure,
)
from .ebay_browser_work_gate import evaluate_ebay_browser_work_gate
from .demand_aware_policy import DEFAULT_DEMAND_AWARE_POLICY
from .demand_aware_scheduler import DemandIndex, evaluate_demand_aware_target, events_from_job_rows
from .owned_verified_local_execution import (
    PRICING_INTENT_OWNED_VERIFIED_LOCAL,
    evaluate_owned_verified_local_execution,
    job_requests_owned_verified_local,
)
from .owned_daily_outcomes import (
    ALTERNATE_EBAY_SURFACE,
    CHALLENGE_REQUIRED,
    CHECKED_NO_NEW_EXACT_EVIDENCE,
    EBAY_ACCESS_DENIED_403,
    FINALIZE_TIMEOUT_SAFE,
    NO_PRICE_EVER_FOUND,
    TEMPORARY_BROWSER_FAILURE,
    TEMPORARY_EBAY_SERVER_FAILURE,
    classify_completed_ebay_write,
    classify_exception_outcome,
    is_sparse_no_new_evidence_reason,
)
from .pipeline_phase_diagnostics import (
    build_provider_diagnostics_for_result,
    extract_pipeline_phases,
)
from .region_pricing_registry import is_region_dispatchable


def _phase_fields_from_provider_result(provider_result: Any | None) -> dict[str, Any]:
    """Authoritative top-level phase fields for job results (fail-closed consumers)."""
    meta = None
    if provider_result is not None:
        raw = getattr(provider_result, "raw_metadata", None)
        if isinstance(raw, dict):
            meta = raw
    phases = extract_pipeline_phases(meta)
    out: dict[str, Any] = {}
    if phases.get("postSoldCapturePhase") is not None:
        out["postSoldCapturePhase"] = phases.get("postSoldCapturePhase")
    if phases.get("parsePhase") is not None:
        out["parsePhase"] = phases.get("parsePhase")
    if phases.get("x11SoldStateVerified"):
        out["x11SoldStateVerified"] = True
    capture_meta = phases.get("persistedCaptureArtifact")
    if isinstance(capture_meta, dict):
        out["currentJobCapture"] = capture_meta
        out["persistedCaptureArtifact"] = capture_meta
    if isinstance(meta, dict) and isinstance(meta.get("desktopNav"), dict):
        out["desktopNav"] = meta.get("desktopNav")
    if isinstance(meta, dict) and meta.get("navMode"):
        out["navMode"] = meta.get("navMode")
    return out


def _phase_fields_from_diagnostics(provider_diagnostics: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(provider_diagnostics, dict):
        return {}
    nested = provider_diagnostics.get("diagnostics")
    phase_src = nested if isinstance(nested, dict) else provider_diagnostics
    phases = extract_pipeline_phases(phase_src if isinstance(phase_src, dict) else None)
    out: dict[str, Any] = {}
    if phases.get("postSoldCapturePhase") is not None:
        out["postSoldCapturePhase"] = phases.get("postSoldCapturePhase")
    if phases.get("parsePhase") is not None:
        out["parsePhase"] = phases.get("parsePhase")
    if phases.get("x11SoldStateVerified"):
        out["x11SoldStateVerified"] = True
    capture_meta = phases.get("persistedCaptureArtifact")
    if isinstance(capture_meta, dict):
        out["currentJobCapture"] = capture_meta
        out["persistedCaptureArtifact"] = capture_meta
    return out
from .owned_daily_pacing import OwnedDailyPacingController
from .gaming_resource_pause import GamingResourcePauseController


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def rejection_reason_counts(evaluated_comps: list[EvaluatedComp]) -> dict[str, int]:
    counts = Counter(str(item.rejection_reason) for item in evaluated_comps if item.rejection_reason)
    return {reason: int(count) for reason, count in counts.most_common()}


def dominant_rejection_reason(evaluated_comps: list[EvaluatedComp]) -> str | None:
    counts = rejection_reason_counts(evaluated_comps)
    if not counts:
        return None
    return next(iter(counts))


def url_quality_counts(provider_result: ProviderResult) -> dict[str, int]:
    summary = provider_result.raw_metadata.get("qualitySummary") or {}
    return {
        "direct_item_url_count": int(summary.get("direct_item_url_count") or 0),
        "generic_url_count": int(summary.get("generic_url_count") or 0),
        "missing_url_count": int(summary.get("missing_url_count") or 0),
    }


def build_price_view_diagnostics(pricing_stats: PricingStats) -> dict[str, Any]:
    return {
        "priceBasis": pricing_stats.price_basis,
        "priceReliability": pricing_stats.price_reliability,
        "landedPriceAvailable": pricing_stats.landed_recommended_price is not None,
        "itemPrice": {
            "median": pricing_stats.item_median_price,
            "average": pricing_stats.item_average_price,
            "low": pricing_stats.item_low_price,
            "high": pricing_stats.item_high_price,
            "recommended": pricing_stats.item_recommended_price,
        },
        "landedPrice": {
            "median": pricing_stats.landed_median_price,
            "average": pricing_stats.landed_average_price,
            "low": pricing_stats.landed_low_price,
            "high": pricing_stats.landed_high_price,
            "recommended": pricing_stats.landed_recommended_price,
        },
        "compatibilityFields": {
            "median_price": "item_median_price",
            "average_price": "item_average_price",
            "low_price": "item_low_price",
            "high_price": "item_high_price",
            "recommended_price": "item_recommended_price",
            "current_market_price": "item_recommended_price",
        },
    }


def convert_pricing_stats(pricing_stats: PricingStats, conversion: CurrencyConversion) -> PricingStats:
    if conversion.rate == 1:
        return pricing_stats
    return replace(
        pricing_stats,
        median_price=conversion.amount(pricing_stats.median_price),
        average_price=conversion.amount(pricing_stats.average_price),
        low_price=conversion.amount(pricing_stats.low_price),
        high_price=conversion.amount(pricing_stats.high_price),
        recommended_price=conversion.amount(pricing_stats.recommended_price),
        item_median_price=conversion.amount(pricing_stats.item_median_price),
        item_average_price=conversion.amount(pricing_stats.item_average_price),
        item_low_price=conversion.amount(pricing_stats.item_low_price),
        item_high_price=conversion.amount(pricing_stats.item_high_price),
        item_recommended_price=conversion.amount(pricing_stats.item_recommended_price),
        landed_median_price=conversion.amount(pricing_stats.landed_median_price),
        landed_average_price=conversion.amount(pricing_stats.landed_average_price),
        landed_low_price=conversion.amount(pricing_stats.landed_low_price),
        landed_high_price=conversion.amount(pricing_stats.landed_high_price),
        landed_recommended_price=conversion.amount(pricing_stats.landed_recommended_price),
        included_price_distribution=tuple(
            value for value in (conversion.amount(price) for price in pricing_stats.included_price_distribution) if value is not None
        ),
    )


def classify_comp_quality(item: EvaluatedComp, *, pricing_stats: PricingStats) -> dict[str, Any]:
    raw = item.comp.raw_metadata
    title = item.comp.title.lower()
    requested_card = str(raw.get("requestedCanonicalCardName") or raw.get("requestedCardName", "")).lower()
    requested_number = str(raw.get("requestedCollectorNumber", "")).lower()
    exact_card_match = bool(item.match_score >= 0.85 and requested_card and requested_card in title and requested_number and requested_number in title)
    item_median = pricing_stats.item_median_price or 0
    landed_median = pricing_stats.landed_median_price or 0
    possible_item_outlier = bool(item_median and (item.comp.sold_price > item_median * 1.8 or item.comp.sold_price < item_median * 0.55))
    possible_landed_outlier = bool(
        landed_median and (item.comp.total_price > landed_median * 1.8 or item.comp.total_price < landed_median * 0.55)
    )
    collector_number_match = bool(raw.get("collector_number_match", requested_number and requested_number in title))
    set_name_match = bool(raw.get("set_name_match", True))
    card_name_match = bool(raw.get("card_name_match", requested_card and requested_card in title))
    shipping_heavy = bool(item.comp.sold_price > 0 and item.comp.shipping_price > item.comp.sold_price)
    return {
        "exact_card_match": exact_card_match,
        "collector_number_match": collector_number_match,
        "set_name_match": set_name_match,
        "card_name_match": card_name_match,
        "likely_same_card": item.match_score >= 0.7,
        "variation_listing": item.rejection_reason == "price_range_or_variation_listing" or bool(raw.get("priceRangeListing")),
        "sealed_or_pack": item.rejection_reason == "sealed_product_for_single_card_request" or bool(raw.get("likely_sealed")),
        "graded_when_raw": item.rejection_reason == "graded_for_raw_request" or bool(raw.get("likely_graded")),
        "currency_mismatch": item.rejection_reason == "currency_mismatch",
        "possible_outlier_item_price": possible_item_outlier,
        "possible_outlier_landed_price": possible_landed_outlier,
        "shipping_heavy": shipping_heavy,
        "price_outlier_warning": possible_item_outlier or possible_landed_outlier,
        "url_quality": raw.get("url_quality", "unknown"),
        "requested_variant": raw.get("requested_variant", "raw"),
        "detected_variant": raw.get("detected_variant", "unknown"),
        "variant_match": raw.get("variant_match", True),
        "variant_warning": raw.get("variant_warning"),
        "why_included": (
            "passed_title_currency_variant_and_outlier_filters" if item.included_in_estimate else None
        ),
        "included": item.included_in_estimate,
    }


class MarketPriceJobRunner:
    def __init__(
        self,
        *,
        client: Any,
        provider: Any,
        config: MarketEngineConfig,
        now_func: Any = utc_now,
        logger: Any = print,
    ) -> None:
        self.client = client
        self.provider = provider
        self.config = config
        self.now_func = now_func
        self.logger = logger

    def _assert_market_allowed_for_worker(self, price_key: MarketPriceKey) -> None:
        market = str(price_key.market_country or "").strip().upper()
        if market and not is_region_dispatchable(market):
            raise ProviderUnsupportedMarketError(
                f"BLOCKED_NEEDS_PROVIDER: {market} has no verified-local browser provider",
                diagnostics={
                    "marketCountry": market,
                    "provider": "NONE",
                    "workerState": "BLOCKED",
                },
            )
        allowed_raw = os.getenv("MARKET_WORKER_ALLOWED_MARKETS")
        if allowed_raw is None:
            allowed_raw = "AU,US,GB,CA"
        allowed = parse_market_allowlist(allowed_raw)
        # Empty string must mean "no deferred markets". On Windows, an unset var
        # falls back to none; do not treat blank as the old GB,CA default.
        deferred_raw = os.getenv("MARKET_WORKER_DEFERRED_CHALLENGE_MARKETS")
        if deferred_raw is None:
            deferred_raw = ""
        if deferred_raw.strip().upper() in {"", "NONE", "OFF", "DISABLE", "DISABLED"}:
            deferred = []
        else:
            deferred = parse_market_allowlist(deferred_raw)
        if deferred and market in deferred:
            raise ProviderBlockedError(
                "MARKETPLACE_CHALLENGE_REQUIRED: marketplace challenge unresolved; "
                "authentication was not attempted and challenge pages are not retried",
                diagnostics={
                    "providerOutcome": "marketplace_challenge_deferred",
                    "operationalStatus": "MARKETPLACE_CHALLENGE_REQUIRED",
                    "marketCountry": market,
                    "currency": str(price_key.currency or "").upper(),
                    "retryable": True,
                },
            )
        if allowed and market and market not in allowed:
            raise ProviderUnsupportedMarketError(
                f"Worker market allowlist excludes {market}",
                diagnostics={
                    "marketCountry": market,
                    "allowedMarkets": allowed,
                },
            )
        try:
            cooldown = get_active_cooldown(market)
        except AtomicStateError as exc:
            raise ProviderBlockedError(
                f"CHALLENGE_REQUIRED: marketplace ops state unreadable ({exc}); refusing browser work",
                diagnostics={
                    "providerOutcome": "marketplace_ops_state_unreadable",
                    "operationalStatus": "CHALLENGE_REQUIRED",
                    "marketCountry": market,
                    "retryable": True,
                },
            ) from exc
        if cooldown is not None:
            status = cooldown.reason if cooldown.reason in {"AUTH_REQUIRED", "CHALLENGE_REQUIRED"} else "DEFERRED"
            raise ProviderBlockedError(
                f"{status}: marketplace temporarily deferred until {utc_iso(cooldown.until)}; "
                "manual session restore may be required",
                diagnostics={
                    "providerOutcome": "marketplace_ops_cooldown",
                    "operationalStatus": status,
                    "marketCountry": market,
                    "cooldownUntil": utc_iso(cooldown.until),
                    "cooldownReason": cooldown.reason,
                    "retryable": True,
                },
            )
        # Global eBay availability + incident ledger + cooldown (authoritative gate).
        allow_probe = str(getattr(self, "_ebay_probe_mode", "") or "").lower() in {"1", "true", "yes"}
        gate = evaluate_ebay_browser_work_gate(market=market or "AU", now=None, for_probe=allow_probe)
        if not gate.allowed:
            primary = gate.reason_codes[0] if gate.reason_codes else "EBAY_BROWSER_WORK_DENIED"
            raise ProviderBlockedError(
                f"{primary}: eBay browser work deferred ({','.join(gate.reason_codes)})",
                diagnostics={
                    "providerOutcome": (
                        "marketplace_ops_cooldown"
                        if any(c.startswith("MARKETPLACE_COOLDOWN:") for c in gate.reason_codes)
                        else (
                            "ebay_availability_cooldown"
                            if EBAY_AVAILABILITY_COOLDOWN in gate.reason_codes
                            else "ebay_availability_halt"
                        )
                    ),
                    "operationalStatus": primary,
                    "marketCountry": market,
                    "ebayBrowserWorkGate": gate.to_dict(),
                    "ebayAvailabilityState": gate.availability_state,
                    "activeChallengeCount": gate.active_challenge_count,
                    "retryable": AVAIL_CHALLENGE not in gate.reason_codes
                    and not any(c.startswith("ACTIVE_CHALLENGE_INCIDENTS:") for c in gate.reason_codes),
                },
            )
        # Preserve begin_probe behaviour when gate allows probe mode.
        allowed_browser, avail_reason, avail_snap = browser_work_allowed(
            for_probe=allow_probe, market=market or "AU"
        )
        if allow_probe and avail_snap.state == "PROBE_REQUIRED" and allowed_browser:
            begin_probe(market=market or "AU")
        elif not allowed_browser:
            # Defensive: gate should have caught this already.
            until = utc_iso(avail_snap.next_probe_at) if avail_snap.next_probe_at else None
            raise ProviderBlockedError(
                f"{avail_reason}: eBay browser work deferred"
                + (f" until {until}" if until else ""),
                diagnostics={
                    "providerOutcome": "ebay_availability_cooldown"
                    if avail_reason == EBAY_AVAILABILITY_COOLDOWN
                    else "ebay_availability_halt",
                    "operationalStatus": avail_reason,
                    "marketCountry": market,
                    "ebayAvailabilityState": avail_snap.state,
                    "nextProbeAt": until,
                    "consecutiveSorryEvents": avail_snap.consecutive_sorry_events,
                    "retryable": avail_reason != AVAIL_CHALLENGE,
                },
            )

    def marketplace_attempts(self, price_key: MarketPriceKey, provider_marketplace: str) -> tuple[LocalMarketConfig, ...]:
        # Home-market only. Cross-marketplace comps must never populate another
        # market's canonical cache, even if MARKET_EBAY_FALLBACK_MARKETPLACES is set.
        # That env remains parsed for diagnostics/compat but is not used for lookups.
        _ = self.config.ebay_fallback_marketplaces
        return ebay_marketplace_fallback_order(
            requested_market_country=price_key.market_country,
            requested_currency=price_key.currency,
            marketplace=provider_marketplace,
            configured_order=(),
        )

    def build_provider_request(
        self,
        *,
        price_key: MarketPriceKey,
        market_config: LocalMarketConfig,
    ) -> ProviderRequest:
        return ProviderRequest(
            price_key=price_key,
            market_country=market_config.market_country,
            currency=market_config.currency,
            marketplace=market_config.marketplace,
            provider_marketplace_id=market_config.provider_marketplace_id,
            provider_domain=market_config.provider_domain,
            search_locale=market_config.search_locale,
            display_name=market_config.display_name,
            market_config=market_config,
        )

    def fetch_fallback_result(
        self,
        *,
        price_key: MarketPriceKey,
        provider_marketplace: str,
        now: datetime,
    ) -> tuple[ProviderRequest, ProviderResult, list[EvaluatedComp], PricingStats, PricingStats, CurrencyConversion, list[dict[str, Any]]]:
        attempts: list[dict[str, Any]] = []
        first_error: Exception | None = None
        first_no_evidence_result: tuple[
            ProviderRequest,
            ProviderResult,
            list[EvaluatedComp],
            PricingStats,
            PricingStats,
            CurrencyConversion,
            list[dict[str, Any]],
        ] | None = None
        home_config = resolve_marketplace_config(
            market_country=price_key.market_country,
            currency=price_key.currency,
            marketplace=provider_marketplace,
        )
        for fallback_level, market_config in enumerate(self.marketplace_attempts(price_key, provider_marketplace)):
            if market_config.provider_marketplace_id != home_config.provider_marketplace_id:
                # Defense in depth: never accept a foreign marketplace for this cache key.
                attempts.append(
                    {
                        "fallbackLevel": fallback_level,
                        "providerMarketplaceId": market_config.provider_marketplace_id,
                        "marketCountry": market_config.market_country,
                        "currency": market_config.currency,
                        "skipped": True,
                        "skipReason": "cross_marketplace_lookup_disabled",
                    }
                )
                continue
            provider_key = replace(
                price_key,
                market_country=market_config.market_country.lower(),
                currency=market_config.currency.lower(),
            )
            provider_request = self.build_provider_request(
                price_key=provider_key,
                market_config=market_config,
            )
            try:
                provider_result = self.provider.fetch_comps(provider_request)
                evaluated_comps = filter_comps(provider_key, provider_result.comps)
                source_stats = calculate_pricing_stats(evaluated_comps, now=now, config=self.config)
                attempts.append(
                    {
                        "fallbackLevel": fallback_level,
                        "providerMarketplaceId": provider_request.provider_marketplace_id,
                        "marketCountry": provider_request.market_country,
                        "currency": provider_request.currency,
                        "acceptedComparableCount": source_stats.included_count,
                        "rejectedComparableCount": source_stats.rejected_count,
                        "recommendedPriceAvailable": source_stats.recommended_price is not None,
                        "confidence": source_stats.confidence,
                        "noReliablePriceReason": source_stats.no_reliable_price_reason,
                    }
                )
                if source_stats.included_count <= 0:
                    if first_no_evidence_result is None:
                        conversion = resolve_currency_conversion(
                            source_currency=provider_request.currency,
                            target_currency=price_key.currency,
                            rates=self.config.currency_rates,
                            rate_source=self.config.currency_rate_source,
                            now=now,
                        )
                        first_no_evidence_result = (
                            provider_request,
                            provider_result,
                            evaluated_comps,
                            source_stats,
                            source_stats,
                            conversion,
                            list(attempts),
                        )
                    continue
                conversion = resolve_currency_conversion(
                    source_currency=provider_request.currency,
                    target_currency=price_key.currency,
                    rates=self.config.currency_rates,
                    rate_source=self.config.currency_rate_source,
                    now=now,
                )
                if conversion.source_currency.upper() != home_config.currency.upper():
                    raise ValueError(
                        "Refusing to cache a price whose source currency does not match "
                        f"the requested market ({home_config.currency})"
                    )
                if conversion.rate != 1:
                    raise ValueError(
                        "Refusing cross-currency conversion into a different pricing market cache"
                    )
                display_stats = convert_pricing_stats(source_stats, conversion)
                return (
                    provider_request,
                    provider_result,
                    evaluated_comps,
                    source_stats,
                    display_stats,
                    conversion,
                    attempts,
                )
            except (
                ProviderBlockedError,
                ProviderAuthenticationRequiredError,
                ProviderMarketplaceMismatchError,
                ProviderUnsupportedMarketError,
            ):
                raise
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                attempts.append(
                    {
                        "fallbackLevel": fallback_level,
                        "providerMarketplaceId": market_config.provider_marketplace_id,
                        "marketCountry": market_config.market_country,
                        "currency": market_config.currency,
                        "error": str(exc),
                    }
                )
                continue
        if first_no_evidence_result is not None:
            return first_no_evidence_result
        if first_error is not None:
            raise first_error
        raise ValueError("Currently no eBay pricing available")

    def claim_jobs(self, *, max_jobs: int | None = None) -> list[MarketPriceRefreshJob]:
        limit = max_jobs or self.config.max_jobs_per_run
        allowed = parse_market_allowlist(os.getenv("MARKET_WORKER_ALLOWED_MARKETS", ""))
        market = allowed[0] if len(allowed) == 1 else None
        return self.client.claim_jobs(
            worker_id=self.config.worker_id,
            max_jobs=limit,
            market_country=market,
        )

    def build_snapshot_payload(
        self,
        *,
        price_key: MarketPriceKey,
        provider_request: ProviderRequest,
        provider_result: ProviderResult,
        evaluated_comps: list[EvaluatedComp],
        pricing_stats: PricingStats,
        now: datetime,
        requested_price_key: MarketPriceKey | None = None,
        source_pricing_stats: PricingStats | None = None,
        currency_conversion: CurrencyConversion | None = None,
        fallback_attempts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        requested_key = requested_price_key or price_key
        source_stats = source_pricing_stats or pricing_stats
        conversion = currency_conversion or resolve_currency_conversion(
            source_currency=provider_request.currency,
            target_currency=requested_key.currency,
            rates=self.config.currency_rates,
            rate_source=self.config.currency_rate_source,
            now=now,
        )
        fallback_level = 0
        if fallback_attempts:
            fallback_level = int(fallback_attempts[-1].get("fallbackLevel") or 0)
        return {
            "price_key_id": price_key.id,
            "provider": provider_result.provider_name,
            "marketplace": provider_result.marketplace,
            "query_used": provider_result.query_used,
            "median_price": pricing_stats.median_price,
            "low_price": pricing_stats.low_price,
            "average_price": pricing_stats.average_price,
            "high_price": pricing_stats.high_price,
            "recommended_price": pricing_stats.recommended_price,
            "sample_size": pricing_stats.sample_size,
            "confidence": pricing_stats.confidence,
            "included_count": pricing_stats.included_count,
            "rejected_count": pricing_stats.rejected_count,
            "diagnostics_json": {
                "providerFingerprint": provider_result.provider_fingerprint,
                "pricingAsOf": utc_iso(now),
                "staleAfter": utc_iso(pricing_stats.stale_after),
                "pricingPolicy": "ebay_home_marketplace_only",
                "evidenceType": "completed_sale",
                "requestedMarketplace": f"EBAY_{requested_key.market_country.upper()}",
                "marketplaceActuallyUsed": provider_request.provider_marketplace_id,
                "fallbackLevel": fallback_level,
                "fallbackAttempts": fallback_attempts or [],
                "originalCurrency": provider_request.currency,
                "displayCurrency": requested_key.currency.upper(),
                "sourcePriceViews": build_price_view_diagnostics(source_stats),
                "currencyConversion": conversion.metadata(
                    source_amount=source_stats.recommended_price,
                    converted_amount=pricing_stats.recommended_price,
                ),
                "shippingTreatment": "total_cost_including_shipping_where_available; item_value_excluding_shipping_displayed_separately",
                "priceViews": build_price_view_diagnostics(pricing_stats),
                "fetchedCount": len(provider_result.comps),
                "marketCountry": provider_request.market_country,
                "currency": provider_request.currency,
                "marketplace": provider_request.marketplace,
                "providerMarketplaceId": provider_request.provider_marketplace_id,
                "providerDomain": provider_request.provider_domain,
                "searchLocale": provider_request.search_locale,
                "marketDisplayName": provider_request.display_name,
                "includedListingIds": [
                    item.comp.source_listing_id for item in evaluated_comps if item.included_in_estimate
                ],
                "rejectedReasons": {
                    item.comp.source_listing_id: item.rejection_reason
                    for item in evaluated_comps
                    if item.rejection_reason
                },
                "rejectionReasonCounts": rejection_reason_counts(evaluated_comps),
                "dominantRejectionReason": dominant_rejection_reason(evaluated_comps),
                "price_spread_ratio": pricing_stats.price_spread_ratio,
                "confidence_warnings": list(pricing_stats.confidence_warnings),
                "included_price_distribution": list(pricing_stats.included_price_distribution),
                "no_reliable_price_reason": pricing_stats.no_reliable_price_reason,
                "price_reliability": pricing_stats.price_reliability,
                "clean_recent_comp_count": pricing_stats.clean_recent_comp_count,
                "clean_stale_comp_count": pricing_stats.clean_stale_comp_count,
                "oldest_clean_comp_date": utc_iso(pricing_stats.oldest_clean_comp_date) if pricing_stats.oldest_clean_comp_date else None,
                "newest_clean_comp_date": utc_iso(pricing_stats.newest_clean_comp_date) if pricing_stats.newest_clean_comp_date else None,
                "sold_listing_recency_threshold_days": pricing_stats.sold_listing_recency_threshold_days,
                "query_attempts": provider_result.raw_metadata.get("queryAttempts") or [],
                "query_attempts_used": provider_result.raw_metadata.get("queryAttemptsUsed"),
                "query_stop_reason": provider_result.raw_metadata.get("queryStopReason"),
                "final_price_basis": pricing_stats.price_basis,
                "url_quality_counts": url_quality_counts(provider_result),
            },
        }

    def build_evidence_rows(
        self,
        *,
        price_key: MarketPriceKey,
        snapshot_id: str,
        provider_request: ProviderRequest,
        provider_result: ProviderResult,
        evaluated_comps: list[EvaluatedComp],
        pricing_stats: PricingStats,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in evaluated_comps:
            rows.append(
                {
                    "price_key_id": price_key.id,
                    "snapshot_id": snapshot_id,
                    "provider": provider_result.provider_name,
                    "marketplace": provider_result.marketplace,
                    "title": item.comp.title,
                    "sold_price": item.comp.sold_price,
                    "shipping_price": item.comp.shipping_price,
                    "total_price": item.comp.total_price,
                    "currency": item.comp.currency,
                    "sold_date": utc_iso(item.comp.sold_date) if item.comp.sold_date is not None else None,
                    "listing_url": item.comp.listing_url,
                    "condition_text": item.comp.condition_text,
                    "match_score": item.match_score,
                    "included_in_estimate": item.included_in_estimate,
                    "rejection_reason": item.rejection_reason,
                    "raw_json": {
                        "sourceListingId": item.comp.source_listing_id,
                        "providerFingerprint": provider_result.provider_fingerprint,
                        "marketCountry": provider_request.market_country,
                        "currency": provider_request.currency,
                        "marketplace": provider_request.marketplace,
                        "providerMarketplaceId": provider_request.provider_marketplace_id,
                        "providerDomain": provider_request.provider_domain,
                        "searchLocale": provider_request.search_locale,
                        "marketDisplayName": provider_request.display_name,
                        "compQuality": classify_comp_quality(item, pricing_stats=pricing_stats),
                        **item.comp.raw_metadata,
                    },
                }
            )
        return rows

    def run_job(self, job: MarketPriceRefreshJob) -> dict[str, Any]:
        if not job.id:
            raise ValueError("Market refresh job is missing id")
        if not job.price_key_id:
            raise ValueError(f"Market refresh job {job.id} is missing price_key_id")
        now = self.now_func()
        price_key: MarketPriceKey | None = None
        prior_price: float | None = None
        prior_cache: dict[str, Any] | None = None
        try:
            price_key = self.client.get_price_key(job.price_key_id)
            if not price_key.id:
                raise ValueError(f"Market price key row missing id for job {job.id}")
            if not price_key.fingerprint:
                raise ValueError(f"Market price key row missing fingerprint for job {job.id}")
            # Prefer already-fresh no-op before marketplace allow/cooldown gates so
            # intentional skips never consume provider capacity or fail on ops cooldowns.
            if hasattr(self.client, "get_cache_row"):
                try:
                    prior_cache = self.client.get_cache_row(price_key_id=price_key.id)
                except Exception:
                    prior_cache = None
            force = "force" in str(job.reason or "").lower()
            # Owned verified-local intent: reason contains owned_daily: (scheduler or
            # reliability harness that embeds the scheduler reason).
            is_owned_verified_local_intent = job_requests_owned_verified_local(reason=job.reason)
            is_owned_daily = is_owned_verified_local_intent
            due_raw = (prior_cache or {}).get("next_refresh_due_at") or (prior_cache or {}).get("stale_after")
            due = None
            if due_raw:
                try:
                    due = datetime.fromisoformat(str(due_raw).replace("Z", "+00:00"))
                except ValueError:
                    due = None
            last_success_raw = (prior_cache or {}).get("last_updated_at")
            prior_price_raw = (prior_cache or {}).get("current_market_price")
            try:
                prior_price = float(prior_price_raw) if prior_price_raw is not None else None
            except (TypeError, ValueError):
                prior_price = None

            # Source-aware owned verified-local gate — MUST run before generic due skip.
            # A recent REFERENCE-ONLY last_updated_at / next_refresh_due_at must not
            # produce skipped_already_fresh for P0_NEEDS_VERIFIED_LOCAL work.
            if not force and is_owned_verified_local_intent:
                demand_hours = int(DEFAULT_DEMAND_AWARE_POLICY.normal_verified_ttl_hours)
                demand_index = getattr(self, "_demand_index", None)
                if demand_index is None:
                    if hasattr(self.client, "list_recent_user_demand_jobs"):
                        try:
                            demand_index = DemandIndex(
                                events_from_job_rows(
                                    self.client.list_recent_user_demand_jobs(hours=168) or []
                                )
                            )
                        except Exception:
                            demand_index = DemandIndex([])
                    else:
                        demand_index = DemandIndex([])
                    self._demand_index = demand_index
                demand_row = evaluate_demand_aware_target(
                    {
                        **(prior_cache or {}),
                        "market_price_key_id": price_key.id,
                        "fingerprint": price_key.fingerprint,
                        "market_country": getattr(price_key, "market_country", None)
                        or (prior_cache or {}).get("market_country"),
                        "current_market_price": prior_price,
                        "display_price_source": (prior_cache or {}).get("display_price_source"),
                        "provider": (prior_cache or {}).get("provider"),
                        "last_updated_at": last_success_raw,
                        "next_refresh_due_at": (prior_cache or {}).get("next_refresh_due_at"),
                        "stale_after": (prior_cache or {}).get("stale_after"),
                        "refresh_status": (prior_cache or {}).get("refresh_status"),
                        "last_error_message": (prior_cache or {}).get("last_error_message"),
                    },
                    now=now,
                    demand_index=demand_index,
                )
                demand_hours = int(demand_row.freshness_threshold_hours)
                owned_exec = evaluate_owned_verified_local_execution(
                    {
                        **(prior_cache or {}),
                        "current_market_price": prior_price,
                        "display_price_source": (prior_cache or {}).get("display_price_source"),
                        "provider": (prior_cache or {}).get("provider"),
                        "last_updated_at": last_success_raw,
                        "next_refresh_due_at": (prior_cache or {}).get("next_refresh_due_at"),
                        "stale_after": (prior_cache or {}).get("stale_after"),
                        "refresh_status": (prior_cache or {}).get("refresh_status"),
                        "last_error_message": (prior_cache or {}).get("last_error_message"),
                    },
                    now=now,
                    success_fresh_hours=demand_hours,
                    pricing_intent=PRICING_INTENT_OWNED_VERIFIED_LOCAL,
                )
                if not owned_exec.should_execute:
                    self.logger(
                        f"[market-engine] skipped_already_fresh job={job.id} "
                        f"reason={owned_exec.reason_code} band={owned_exec.scheduler_band} "
                        f"sourceClass={owned_exec.source_class} "
                        f"verifiedAt={owned_exec.successful_verified_at}"
                    )
                    if hasattr(self.client, "cancel_job"):
                        self.client.cancel_job(job_id=job.id, reason="skipped_already_fresh")
                    return {
                        "jobId": job.id,
                        "priceKeyId": price_key.id,
                        "status": "skipped_already_fresh",
                        "outcomeClass": "owned_daily_fresh_noop",
                        "ownedDailyOutcome": "already_fresh_noop",
                        "executionEligibility": owned_exec.to_dict(),
                        "lastUpdatedAt": owned_exec.successful_verified_at,
                        "nextRefreshDueAt": owned_exec.next_eligible_at,
                    }
                self.logger(
                    f"[market-engine] owned_verified_local_execute job={job.id} "
                    f"reason={owned_exec.reason_code} band={owned_exec.scheduler_band} "
                    f"sourceClass={owned_exec.source_class} cacheDue={due_raw}"
                )
            elif not force and due is not None and due > now:
                # Generic (non-owned) cache freshness — unchanged for manual/API/reference jobs.
                self.logger(f"[market-engine] skipped_already_fresh job={job.id} due={due_raw}")
                if hasattr(self.client, "cancel_job"):
                    self.client.cancel_job(
                        job_id=job.id,
                        reason="skipped_already_fresh",
                    )
                else:
                    self.client.fail_job(
                        job_id=job.id,
                        error_message="skipped_already_fresh",
                        retryable=False,
                        retry_delay_minutes=max(
                            1, int((due - now).total_seconds() // 60) or 1
                        ),
                    )
                return {
                    "jobId": job.id,
                    "priceKeyId": price_key.id,
                    "status": "skipped_already_fresh",
                    "outcomeClass": "already_fresh_noop",
                    "nextRefreshDueAt": due.isoformat().replace("+00:00", "Z"),
                }

            os.environ["CARDSCANR_JOB_ID"] = str(job.id)
            os.environ["CARDSCANR_PRICE_KEY_ID"] = str(price_key.id)
            os.environ.setdefault("CARDSCANR_CAPTURE_ORIGIN", "LIVE_BROWSER_CAPTURE")
            if getattr(price_key, "fingerprint", None):
                os.environ["CARDSCANR_FINGERPRINT"] = str(price_key.fingerprint)
            nav_ctx = load_navigation_runtime_context()
            nav_ctx.current_job_id = str(job.id)
            nav_ctx.current_price_key_id = str(price_key.id)
            if getattr(price_key, "fingerprint", None):
                nav_ctx.current_fingerprint = str(price_key.fingerprint)
            prepare_context_for_market(
                nav_ctx,
                market=str(getattr(price_key, "market_country", "") or "AU"),
                currency=str(getattr(price_key, "currency", "") or ""),
            )
            apply_context_to_environ(nav_ctx)

            self._assert_market_allowed_for_worker(price_key)
            self.logger(f"[market-engine] processing job={job.id} key={price_key.fingerprint}")
            provider_marketplace = getattr(self.provider, "marketplace_name", "ebay")
            try:
                (
                    provider_request,
                    provider_result,
                    evaluated_comps,
                    source_pricing_stats,
                    pricing_stats,
                    currency_conversion,
                    fallback_attempts,
                ) = self.fetch_fallback_result(
                    price_key=price_key,
                    provider_marketplace=provider_marketplace,
                    now=now,
                )
            finally:
                for _env_key in ("CARDSCANR_JOB_ID", "CARDSCANR_FINGERPRINT"):
                    os.environ.pop(_env_key, None)
            movement = evaluate_price_movement(
                old_price=(prior_cache or {}).get("current_market_price"),
                new_price=pricing_stats.recommended_price,
                included_count=int(pricing_stats.included_count or 0),
                confidence=pricing_stats.confidence,
                prior_confidence=(prior_cache or {}).get("confidence"),
                prior_included_count=(prior_cache or {}).get("included_count")
                or (prior_cache or {}).get("sample_size"),
            )
            provider_result.raw_metadata["priceMovement"] = movement_diagnostics(movement)
            if movement.action == "pending_verification" and pricing_stats.recommended_price is not None:
                # Keep prior trusted price in cache; still store snapshot/evidence for audit.
                pricing_stats = replace(
                    pricing_stats,
                    recommended_price=(prior_cache or {}).get("current_market_price"),
                    median_price=pricing_stats.median_price,
                )
                provider_result.raw_metadata["priceMovement"]["cacheWriteMode"] = "preserve_prior_pending_verification"
            elif movement.action == "reject_weak":
                pricing_stats = replace(
                    pricing_stats,
                    recommended_price=(prior_cache or {}).get("current_market_price"),
                )
                provider_result.raw_metadata["priceMovement"]["cacheWriteMode"] = "preserve_prior_reject_weak"

            # Never write $0 / null as a successful refresh when a prior good price exists.
            new_price = pricing_stats.recommended_price
            try:
                new_price_f = float(new_price) if new_price is not None else None
            except (TypeError, ValueError):
                new_price_f = None
            prior_good = prior_price is not None and prior_price > 0
            sparse_reason = str(getattr(pricing_stats, "no_reliable_price_reason", None) or "")
            if (new_price_f is None or new_price_f <= 0) and prior_good:
                # Sold search + identity filter ran; insufficient exact comps.
                # This is a healthy CHECKED_NO_NEW_EXACT_EVIDENCE outcome, not a browser failure.
                next_due = now + timedelta(hours=24 if is_owned_daily else max(1, int(self.config.no_comps_hours)))
                if hasattr(self.client, "mark_cache_checked_no_new_evidence"):
                    self.client.mark_cache_checked_no_new_evidence(
                        price_key_id=price_key.id,
                        next_refresh_due_at=next_due,
                        market_country=price_key.market_country,
                        currency=price_key.currency,
                        outcome_message=CHECKED_NO_NEW_EXACT_EVIDENCE,
                    )
                if hasattr(self.client, "cancel_job"):
                    self.client.cancel_job(
                        job_id=job.id,
                        reason=CHECKED_NO_NEW_EXACT_EVIDENCE,
                    )
                else:
                    self.client.fail_job(
                        job_id=job.id,
                        error_message=CHECKED_NO_NEW_EXACT_EVIDENCE,
                        retryable=True,
                        retry_delay_minutes=max(1, int((next_due - now).total_seconds() // 60)),
                    )
                self.logger(
                    f"[market-engine] {CHECKED_NO_NEW_EXACT_EVIDENCE} job={job.id} "
                    f"reason={sparse_reason or 'no_recommended_price'} retained={prior_price}"
                )
                try:
                    record_healthy_browser_check(
                        now=now,
                        from_probe=bool(getattr(self, "_ebay_probe_mode", False)),
                    )
                except Exception as avail_exc:
                    self.logger(f"[market-engine] ebay availability healthy update failed: {avail_exc}")
                _diag = build_provider_diagnostics_for_result(provider_result)
                return {
                    "jobId": job.id,
                    "priceKeyId": price_key.id,
                    "status": "checked_no_new_exact_evidence",
                    "ownedDailyOutcome": CHECKED_NO_NEW_EXACT_EVIDENCE,
                    "outcomeClass": CHECKED_NO_NEW_EXACT_EVIDENCE,
                    "noReliablePriceReason": sparse_reason or None,
                    "includedCount": int(pricing_stats.included_count or 0),
                    "rejectedCount": int(pricing_stats.rejected_count or 0),
                    "retainedPrice": prior_price,
                    "lastUpdatedAt": (prior_cache or {}).get("last_updated_at"),
                    "nextRefreshDueAt": utc_iso(next_due),
                    "lastGoodRetained": True,
                    "providerDiagnostics": _diag,
                    **_phase_fields_from_provider_result(provider_result),
                }
            if new_price_f is None or new_price_f <= 0:
                raise ValueError("no_reliable_price:refusing_zero_or_null_cache_write")
            provider_result.raw_metadata["displayCurrency"] = price_key.currency.upper()
            provider_result.raw_metadata["requestedMarketplace"] = f"EBAY_{price_key.market_country.upper()}"
            provider_result.raw_metadata["marketplaceActuallyUsed"] = provider_request.provider_marketplace_id
            snapshot_payload = self.build_snapshot_payload(
                price_key=price_key,
                provider_request=provider_request,
                provider_result=provider_result,
                evaluated_comps=evaluated_comps,
                pricing_stats=pricing_stats,
                now=now,
                requested_price_key=price_key,
                source_pricing_stats=source_pricing_stats,
                currency_conversion=currency_conversion,
                fallback_attempts=fallback_attempts,
            )
            snapshot = self.client.insert_snapshot(snapshot_payload)
            evidence_rows = self.build_evidence_rows(
                price_key=price_key,
                snapshot_id=str(snapshot["id"]),
                provider_request=provider_request,
                provider_result=provider_result,
                evaluated_comps=evaluated_comps,
                pricing_stats=pricing_stats,
            )
            self.client.insert_evidence(evidence_rows)
            cache_payload = build_cache_payload(
                price_key=price_key,
                provider_result=provider_result,
                pricing_stats=pricing_stats,
                snapshot_id=str(snapshot["id"]),
                refreshed_at=now,
            )
            cache = self.client.upsert_cache(cache_payload)
            self.client.complete_job(
                job_id=job.id,
                snapshot_id=str(snapshot["id"]),
                cache_updated_at=now,
                stale_after=pricing_stats.stale_after,
                next_refresh_due_at=pricing_stats.stale_after,
            )
            write_outcome = classify_completed_ebay_write(
                prior_price=prior_price,
                new_price=float(new_price_f),
            )
            try:
                record_healthy_browser_check(
                    now=now,
                    from_probe=bool(getattr(self, "_ebay_probe_mode", False)),
                )
            except Exception as avail_exc:
                self.logger(f"[market-engine] ebay availability healthy update failed: {avail_exc}")
            _diag = build_provider_diagnostics_for_result(provider_result)
            _phase = _phase_fields_from_provider_result(provider_result)
            return {
                "jobId": job.id,
                "priceKeyId": price_key.id,
                "snapshotId": str(snapshot["id"]),
                "cacheRowId": str(cache.get("id") or "") or None,
                "includedCount": pricing_stats.included_count,
                "rejectedCount": pricing_stats.rejected_count,
                "confidence": pricing_stats.confidence,
                "recommendedPrice": pricing_stats.recommended_price,
                "sampleCount": pricing_stats.included_count,
                "priorPrice": prior_price,
                "resultingPrice": float(new_price_f) if new_price_f is not None else None,
                "displayPriceSource": cache.get("display_price_source") if isinstance(cache, dict) else None,
                "provider": (provider_result.provider_name if provider_result is not None else None),
                "refreshStatus": "completed",
                "verifiedSuccessFreshness": utc_iso(now),
                "requestedMarketplace": f"EBAY_{price_key.market_country.upper()}",
                "marketCountry": provider_request.market_country,
                "sourceCurrency": provider_request.currency,
                "currency": price_key.currency.upper(),
                "marketplace": provider_request.provider_marketplace_id,
                "fallbackLevel": int(fallback_attempts[-1].get("fallbackLevel") or 0) if fallback_attempts else 0,
                "evidenceType": "completed_sale",
                "status": "completed",
                "ownedDailyOutcome": write_outcome,
                "outcomeClass": write_outcome,
                # Authoritative pipeline phases for harness/reporting (fail-closed consumers).
                "providerDiagnostics": _diag,
                **_phase,
            }
        except Exception as exc:
            provider_diagnostics: dict[str, Any] | None = None
            if isinstance(exc, ProviderError):
                provider_diagnostics = sanitize_provider_diagnostics(
                    {
                        "providerErrorCode": exc.error_code,
                        "retryable": exc.retryable,
                        "diagnostics": exc.diagnostics,
                    }
                )
            elif "provider_result" in locals() and provider_result is not None:
                # Preserve capture/parse phase acknowledgement even when a later
                # local write/finalize failure is not a ProviderError.
                try:
                    provider_diagnostics = build_provider_diagnostics_for_result(provider_result)
                except Exception:
                    provider_diagnostics = None
            error_message = str(exc)
            owned_outcome = classify_exception_outcome(
                exc if isinstance(exc, Exception) else error_message,
                diagnostics=(exc.diagnostics if isinstance(exc, ProviderError) else None),
            )
            # Programming invariants must not create marketplace cooldown.
            if price_key is not None and owned_outcome != "UNACCOUNTED_SEARCH_URL_NAVIGATION":
                maybe_record_failure_cooldown(
                    market=str(price_key.market_country or ""),
                    message=str(exc),
                    diagnostics=(exc.diagnostics if isinstance(exc, ProviderError) else None),
                    now=now,
                )
            self.logger(f"[market-engine] job failed job={job.id}: {exc}")
            # Worker-wide eBay availability circuit (do not re-open on cooldown skip).
            try:
                diag = (exc.diagnostics if isinstance(exc, ProviderError) else None) or {}
                operational = str(diag.get("operationalStatus") or "")
                if owned_outcome == "UNACCOUNTED_SEARCH_URL_NAVIGATION":
                    release_probe_local_failure(now=now, reference=error_message)
                elif operational in {EBAY_AVAILABILITY_COOLDOWN, AVAIL_CHALLENGE} or str(
                    diag.get("providerOutcome") or ""
                ) in {"ebay_availability_cooldown", "ebay_availability_halt", "marketplace_ops_cooldown"}:
                    pass
                elif owned_outcome == FINALIZE_TIMEOUT_SAFE or str(diag.get("terminal") or "") == FINALIZE_TIMEOUT_SAFE:
                    # Local post-Sold CDP hang — do not treat as eBay SORRY or healthy.
                    release_probe_local_failure(now=now, reference=error_message)
                elif owned_outcome == "POST_SOLD_CAPTURE_FAILURE" or str(diag.get("reason") or "") in {
                    "post_sold_capture_failed",
                    "cdp_post_sold_capture_failed",
                }:
                    # Local capture failure after X11 Sold verified — retain last-good; no SORRY breaker.
                    release_probe_local_failure(now=now, reference=error_message)
                elif owned_outcome == ALTERNATE_EBAY_SURFACE or str(diag.get("reason") or "") in {
                    "ebay_live_results",
                    "alternate_ebay_surface",
                    "sold_unavailable_on_alternate_surface",
                }:
                    # Alternate eBay surface — retain last-good; do not trip SORRY breaker.
                    release_probe_local_failure(now=now, reference=error_message)
                elif owned_outcome in {
                    "LOCAL_SEARCH_SURFACE_STATE_LEAK",
                    "LOCAL_SEARCH_SURFACE_RECOVERY_FAILED",
                } or str(diag.get("reason") or "") in {
                    "search_origin_ebay_live",
                    "ebay_live_surface_at_submit_gate",
                    "live_leak_after_type_unrecoverable",
                    "ordinary_marketplace_surface_unrecoverable",
                }:
                    # Local Live-scope leak — retain last-good; do not trip SORRY breaker.
                    release_probe_local_failure(now=now, reference=error_message)
                elif (
                    bool(getattr(self, "_ebay_probe_mode", False))
                    and owned_outcome not in {TEMPORARY_EBAY_SERVER_FAILURE, CHALLENGE_REQUIRED, EBAY_ACCESS_DENIED_403}
                    and operational not in {EBAY_AVAILABILITY_COOLDOWN, AVAIL_CHALLENGE}
                ):
                    # Probe slot must not stay in_flight after local/browser failures.
                    release_probe_local_failure(now=now, reference=error_message)
                elif owned_outcome == CHALLENGE_REQUIRED or operational == AVAIL_CHALLENGE:
                    cooldown_row = None
                    try:
                        if price_key is not None:
                            cooldown_row = get_active_cooldown(str(price_key.market_country or ""))
                    except Exception:
                        cooldown_row = None
                    record_challenge(
                        now=now,
                        reference=error_message,
                        market=(price_key.market_country if price_key is not None else None),
                        incident_id=(cooldown_row.incident_id if cooldown_row is not None else None),
                    )
                elif owned_outcome == TEMPORARY_EBAY_SERVER_FAILURE or owned_outcome == EBAY_ACCESS_DENIED_403 or str(
                    diag.get("reason") or ""
                ) in {
                    "ebay_sorry_error_page",
                    "ebay_error_page",
                    "ebay_access_denied_403",
                    "marketplace_error_page",
                } or str(diag.get("failureClass") or "").upper() in {
                    "MARKETPLACE_ERROR_PAGE",
                    "EBAY_ERROR_PAGE",
                    "TARGET_REJECTED_UNHEALTHY_PAGE",
                }:
                    record_sorry(
                        now=now,
                        reference=error_message,
                        market=(price_key.market_country if price_key is not None else None),
                        from_probe=bool(getattr(self, "_ebay_probe_mode", False)),
                    )
            except Exception as avail_exc:
                self.logger(f"[market-engine] ebay availability failure update failed: {avail_exc}")
            # Legacy refuse-zero path: treat as sparse check when prior good exists.
            if (
                owned_outcome != CHECKED_NO_NEW_EXACT_EVIDENCE
                and prior_price is not None
                and prior_price > 0
                and (
                    "refusing_zero_price_overwrite" in error_message.lower()
                    or is_sparse_no_new_evidence_reason(error_message)
                )
            ):
                owned_outcome = CHECKED_NO_NEW_EXACT_EVIDENCE
            if owned_outcome == CHECKED_NO_NEW_EXACT_EVIDENCE and prior_price is not None and prior_price > 0:
                next_due = now + timedelta(hours=24)
                try:
                    if hasattr(self.client, "mark_cache_checked_no_new_evidence"):
                        self.client.mark_cache_checked_no_new_evidence(
                            price_key_id=job.price_key_id,
                            next_refresh_due_at=next_due,
                            market_country=(price_key.market_country if price_key is not None else None),
                            currency=(price_key.currency if price_key is not None else None),
                        )
                    if hasattr(self.client, "cancel_job"):
                        self.client.cancel_job(job_id=job.id, reason=CHECKED_NO_NEW_EXACT_EVIDENCE)
                except Exception as checked_exc:
                    self.logger(f"[market-engine] checked_no_new_evidence finalize failed job={job.id}: {checked_exc}")
                return {
                    "jobId": job.id,
                    "priceKeyId": job.price_key_id,
                    "status": "checked_no_new_exact_evidence",
                    "ownedDailyOutcome": CHECKED_NO_NEW_EXACT_EVIDENCE,
                    "outcomeClass": CHECKED_NO_NEW_EXACT_EVIDENCE,
                    "error": error_message,
                    "retainedPrice": prior_price,
                    "lastGoodRetained": True,
                    "nextRefreshDueAt": utc_iso(next_due),
                    **({"providerDiagnostics": provider_diagnostics} if provider_diagnostics else {}),
                    **_phase_fields_from_diagnostics(provider_diagnostics),
                }
            consecutive = 1
            try:
                consecutive = 1 + int(
                    self.client.count_recent_same_failures(
                        price_key_id=job.price_key_id,
                        error_message=error_message,
                    )
                )
            except Exception as count_exc:
                self.logger(f"[market-engine] failure count lookup failed job={job.id}: {count_exc}")
            policy = build_failure_policy(
                exc if isinstance(exc, Exception) else error_message,
                now=now,
                consecutive_same_failures=consecutive,
            )
            fail_job_error: str | None = None
            try:
                self.client.fail_job(
                    job_id=job.id,
                    error_message=error_message,
                    retryable=policy.retryable,
                    retry_delay_minutes=max(1, int(policy.backoff.total_seconds() // 60)),
                )
            except Exception as fail_exc:
                fail_job_error = str(fail_exc)
                self.logger(f"[market-engine] fail_job rpc failed job={job.id}: {fail_job_error}")
            try:
                desktop_nav = {}
                if isinstance(provider_diagnostics, dict):
                    nested = provider_diagnostics.get("diagnostics") or provider_diagnostics
                    if isinstance(nested, dict):
                        desktop_nav = nested.get("desktopNav") or nested.get("linuxNav") or {}
                search_submitted = bool(
                    (desktop_nav or {}).get("searchSuccess")
                    or (isinstance(provider_diagnostics, dict) and provider_diagnostics.get("searchSubmissionStarted"))
                )
                local_pre_submit = (
                    owned_outcome == TEMPORARY_BROWSER_FAILURE
                    and not search_submitted
                    and is_local_runtime_failure_message(error_message)
                )
                if local_pre_submit and hasattr(self.client, "mark_cache_local_runtime_failure"):
                    self.client.mark_cache_local_runtime_failure(
                        price_key_id=job.price_key_id,
                        error_message=error_message,
                        market_country=(price_key.market_country if price_key is not None else None),
                        currency=(price_key.currency if price_key is not None else None),
                    )
                else:
                    self.client.mark_cache_failure(
                        price_key_id=job.price_key_id,
                        error_message=error_message,
                        next_refresh_due_at=policy.next_refresh_due_at,
                        market_country=(price_key.market_country if price_key is not None else None),
                        currency=(price_key.currency if price_key is not None else None),
                    )
            except Exception as cache_exc:
                self.logger(f"[market-engine] mark_cache_failure failed job={job.id}: {cache_exc}")
            if owned_outcome == NO_PRICE_EVER_FOUND or (
                (prior_price is None or prior_price <= 0)
                and "no_reliable_price" in error_message.lower()
            ):
                owned_outcome = NO_PRICE_EVER_FOUND
            result = {
                "jobId": job.id,
                "priceKeyId": job.price_key_id,
                "status": "failed",
                "error": error_message,
                "ownedDailyOutcome": owned_outcome,
                "outcomeClass": owned_outcome,
                "failurePolicy": failure_policy_diagnostics(policy),
                "consecutiveSameFailures": consecutive,
                "lastGoodRetained": bool(prior_price is not None and prior_price > 0),
            }
            if provider_diagnostics:
                result["providerDiagnostics"] = provider_diagnostics
                result.update(_phase_fields_from_diagnostics(provider_diagnostics))
                nested = provider_diagnostics.get("diagnostics")
                if not isinstance(nested, dict):
                    nested = provider_diagnostics if isinstance(provider_diagnostics, dict) else {}
                for key in (
                    "errorType",
                    "errorMessage",
                    "failureStage",
                    "failureClass",
                    "navigationFailureClass",
                    "exceptionLocation",
                    "runtimeMode",
                    "tracebackTail",
                    "childExitCode",
                    "childStderrSummary",
                    "expectedPriorTargetId",
                    "searchSubmissionStarted",
                ):
                    if nested.get(key) is not None:
                        result[key] = nested.get(key)
                if not result.get("errorMessage"):
                    result["errorMessage"] = error_message
                if not result.get("errorType") and isinstance(exc, Exception):
                    result["errorType"] = type(exc).__name__
            if fail_job_error:
                result["failJobError"] = fail_job_error
            return result

    def run_once(self, *, max_jobs: int | None = None) -> list[dict[str, Any]]:
        gaming = GamingResourcePauseController()
        if gaming.should_block_new_jobs():
            self.logger(
                f"[market-engine] gaming pause active ({gaming.block_reason()}); "
                "not claiming new pricing jobs (queue preserved)"
            )
            return []
        jobs = self.claim_jobs(max_jobs=max_jobs)
        if not jobs:
            self.logger("[market-engine] no queued jobs claimed")
            return []
        # eBay browser: never burst multiple market checks without inter-job pacing.
        # Prefer max_jobs=1 from the worker; if a batch is claimed, pace between jobs.
        provider_name = str(getattr(self.provider, "provider_name", "") or "").strip().lower()
        if provider_name != "ebay_browser" or len(jobs) <= 1:
            results: list[dict[str, Any]] = []
            for job in jobs:
                # Already claimed before pause race — finish this card, then stop.
                if results and gaming.should_block_new_jobs():
                    self.logger("[market-engine] gaming pause after card boundary; stopping claim batch")
                    break
                gaming.mark_job_started(str(job.price_key_id))
                try:
                    results.append(self.run_job(job))
                finally:
                    gaming.mark_job_finished()
            return results

        pacing = OwnedDailyPacingController()
        results = []
        for index, job in enumerate(jobs):
            if gaming.should_block_new_jobs():
                self.logger(
                    f"[market-engine] gaming pause; skipping remaining {len(jobs) - index} claimed jobs"
                )
                break
            if pacing.state.browser_halted:
                self.logger(
                    f"[market-engine] browser halted ({pacing.state.halt_reason}); "
                    f"skipping remaining {len(jobs) - index} claimed jobs this cycle"
                )
                break
            t0 = time.monotonic()
            gaming.mark_job_started(str(job.price_key_id))
            try:
                result = self.run_job(job)
            finally:
                gaming.mark_job_finished()
            pacing.record_check_duration(time.monotonic() - t0)
            outcome = str(result.get("ownedDailyOutcome") or result.get("outcomeClass") or "").strip()
            pacing.observe_outcome(outcome, last_good_retained=bool(result.get("lastGoodRetained")))
            results.append(result)
            if outcome == CHALLENGE_REQUIRED:
                break
            if gaming.should_block_new_jobs():
                self.logger("[market-engine] gaming pause after card; no further jobs this cycle")
                break
            if index + 1 < len(jobs):
                delay = pacing.next_delay_seconds(more_jobs_pending=True)
                self.logger(f"[market-engine] paced_cooldown={delay}s before next claimed job")
                time.sleep(delay)
        return results
