from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from dataclasses import replace
import os
from typing import Any

from .cache_writer import build_cache_payload
from .config import MarketEngineConfig
from .currency_conversion import CurrencyConversion, resolve_currency_conversion
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
from .scheduler import parse_market_allowlist
from .marketplace_ops_state import (
    get_active_cooldown,
    maybe_record_failure_cooldown,
)


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
        cooldown = get_active_cooldown(market)
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
        return self.client.claim_jobs(worker_id=self.config.worker_id, max_jobs=limit)

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
        try:
            price_key = self.client.get_price_key(job.price_key_id)
            if not price_key.id:
                raise ValueError(f"Market price key row missing id for job {job.id}")
            if not price_key.fingerprint:
                raise ValueError(f"Market price key row missing fingerprint for job {job.id}")
            self._assert_market_allowed_for_worker(price_key)
            self.logger(f"[market-engine] processing job={job.id} key={price_key.fingerprint}")
            provider_marketplace = getattr(self.provider, "marketplace_name", "ebay")
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
            return {
                "jobId": job.id,
                "priceKeyId": price_key.id,
                "snapshotId": str(snapshot["id"]),
                "cacheRowId": str(cache.get("id") or "") or None,
                "includedCount": pricing_stats.included_count,
                "rejectedCount": pricing_stats.rejected_count,
                "confidence": pricing_stats.confidence,
                "recommendedPrice": pricing_stats.recommended_price,
                "requestedMarketplace": f"EBAY_{price_key.market_country.upper()}",
                "marketCountry": provider_request.market_country,
                "sourceCurrency": provider_request.currency,
                "currency": price_key.currency.upper(),
                "marketplace": provider_request.provider_marketplace_id,
                "fallbackLevel": int(fallback_attempts[-1].get("fallbackLevel") or 0) if fallback_attempts else 0,
                "evidenceType": "completed_sale",
                "status": "completed",
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
            if price_key is not None:
                maybe_record_failure_cooldown(
                    market=str(price_key.market_country or ""),
                    message=str(exc),
                    diagnostics=(exc.diagnostics if isinstance(exc, ProviderError) else None),
                    now=now,
                )
            self.logger(f"[market-engine] job failed job={job.id}: {exc}")
            fail_job_error: str | None = None
            try:
                self.client.fail_job(job_id=job.id, error_message=str(exc))
            except Exception as fail_exc:
                fail_job_error = str(fail_exc)
                self.logger(f"[market-engine] fail_job rpc failed job={job.id}: {fail_job_error}")
            result = {
                "jobId": job.id,
                "priceKeyId": job.price_key_id,
                "status": "failed",
                "error": str(exc),
            }
            if provider_diagnostics:
                result["providerDiagnostics"] = provider_diagnostics
            if fail_job_error:
                result["failJobError"] = fail_job_error
            return result

    def run_once(self, *, max_jobs: int | None = None) -> list[dict[str, Any]]:
        jobs = self.claim_jobs(max_jobs=max_jobs)
        if not jobs:
            self.logger("[market-engine] no queued jobs claimed")
            return []
        return [self.run_job(job) for job in jobs]
