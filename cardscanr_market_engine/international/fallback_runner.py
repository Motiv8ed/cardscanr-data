"""Execute bounded international eBay fallback searches for shared price keys."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from ..cache_writer import build_cache_payload
from ..config import MarketEngineConfig
from ..currency_conversion import CurrencyConversion, resolve_currency_conversion
from ..filters import filter_comps
from ..job_runner import (
    MarketPriceJobRunner,
    build_price_view_diagnostics,
    classify_comp_quality,
    convert_pricing_stats,
    dominant_rejection_reason,
    rejection_reason_counts,
    url_quality_counts,
    utc_iso,
)
from ..marketplaces import LocalMarketConfig, resolve_marketplace_config
from ..models import EvaluatedComp, MarketPriceKey, MarketPriceRefreshJob, PricingStats, ProviderRequest, ProviderResult
from ..price_movement_guard import evaluate_price_movement, movement_diagnostics
from ..pricing_stats import calculate_pricing_stats
from .fallback_eligibility import evaluate_international_fallback_eligibility
from .fx_freshness import (
    assert_fx_allows_international_conversion,
    evaluate_fx_freshness,
)
from .fx_cache import load_production_pair_rates
from .market_fallback_policy import market_display_name, parse_international_job_reason


INTERNATIONAL_TTL_HOURS = {
    "high": 24,
    "medium": 48,
    "low": 72,
}


def international_stale_after(*, confidence: str, now: datetime) -> datetime:
    hours = INTERNATIONAL_TTL_HOURS.get(str(confidence or "").lower(), 48)
    return now + timedelta(hours=hours)


def build_international_cache_payload(
    *,
    price_key: MarketPriceKey,
    provider_result: ProviderResult,
    pricing_stats: PricingStats,
    snapshot_id: str,
    refreshed_at: datetime,
    source_market_country: str,
    source_currency: str,
    source_stats: PricingStats,
    conversion: CurrencyConversion,
    fallback_reason: str,
    fx_freshness: dict[str, Any],
) -> dict[str, Any]:
    payload = build_cache_payload(
        price_key=price_key,
        provider_result=provider_result,
        pricing_stats=pricing_stats,
        snapshot_id=snapshot_id,
        refreshed_at=refreshed_at,
    )
    stale_after = international_stale_after(confidence=pricing_stats.confidence, now=refreshed_at)
    stale_iso = utc_iso(stale_after)
    payload.update(
        {
            "market_country": price_key.market_country.upper(),
            "currency": price_key.currency.upper(),
            "display_price_source": "international_estimate",
            "source_market_country": source_market_country.upper(),
            "source_currency": source_currency.upper(),
            "source_price": source_stats.recommended_price,
            "source_low_price": source_stats.low_price,
            "source_high_price": source_stats.high_price,
            "international_source_market": source_market_country.upper(),
            "international_fallback_at": utc_iso(refreshed_at),
            "international_fallback_reason": fallback_reason,
            "fx_rate": conversion.rate,
            "fx_rate_timestamp": conversion.rate_timestamp.isoformat().replace("+00:00", "Z"),
            "fx_rate_source": conversion.rate_source,
            "fx_provider_rate_date": fx_freshness.get("providerRateDate"),
            "fx_fetched_at": fx_freshness.get("fetchedAt") or fx_freshness.get("rateTimestamp"),
            "stale_after": stale_iso,
            "next_refresh_due_at": stale_iso,
            "verification_required": False,
            "verification_reason": None,
            "last_error_message": None,
        }
    )
    provider_result.raw_metadata["fxFreshness"] = fx_freshness
    return payload


class InternationalFallbackMixin:
    """International estimate path layered onto the home-market job runner."""

    def is_international_fallback_job(self, job: MarketPriceRefreshJob) -> bool:
        return parse_international_job_reason(job.reason) is not None

    def fetch_international_estimate_result(
        self,
        *,
        price_key: MarketPriceKey,
        provider_marketplace: str,
        now: datetime,
        target_market: str | None = None,
    ) -> tuple[
        ProviderRequest,
        ProviderResult,
        list[EvaluatedComp],
        PricingStats,
        PricingStats,
        CurrencyConversion,
        list[dict[str, Any]],
        str,
    ]:
        eligibility = evaluate_international_fallback_eligibility(price_key=price_key, cache=None, now=now)
        markets = list(eligibility.fallback_markets)
        if target_market:
            normalized = target_market.strip().upper()
            markets = [normalized] + [item for item in markets if item != normalized]
        if not markets:
            raise ValueError("international_fallback_exhausted:no_compatible_markets")

        attempts: list[dict[str, Any]] = []
        home_config = resolve_marketplace_config(
            market_country=price_key.market_country,
            currency=price_key.currency,
            marketplace=provider_marketplace,
        )
        for fallback_level, market_country in enumerate(markets, start=1):
            market_config = resolve_marketplace_config(
                market_country=market_country,
                currency=_market_currency(market_country),
                marketplace=provider_marketplace,
            )
            provider_key = replace(
                price_key,
                market_country=market_config.market_country.lower(),
                currency=market_config.currency.lower(),
            )
            provider_request = self.build_provider_request(price_key=provider_key, market_config=market_config)
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
                if source_stats.included_count <= 0 or source_stats.recommended_price is None:
                    continue
                same_currency = (
                    str(provider_request.currency).upper() == str(price_key.currency).upper()
                )
                if same_currency:
                    conversion = resolve_currency_conversion(
                        source_currency=provider_request.currency,
                        target_currency=price_key.currency,
                        rates={},
                        rate_source="same_currency",
                        now=now,
                    )
                    fx = evaluate_fx_freshness(same_currency=True, now=now)
                else:
                    rates, fx, _cache = load_production_pair_rates(now=now)
                    assert_fx_allows_international_conversion(fx)
                    conversion = resolve_currency_conversion(
                        source_currency=provider_request.currency,
                        target_currency=price_key.currency,
                        rates=rates,
                        rate_source="ECB",
                        now=now,
                    )
                    conversion = replace(
                        conversion,
                        rate_timestamp=fx.fetched_at or fx.rate_timestamp,
                    )
                # Never publish a newly converted user-facing international estimate
                # with FX outside the approved freshness window.
                assert_fx_allows_international_conversion(fx)
                display_stats = convert_pricing_stats(source_stats, conversion)
                provider_result.raw_metadata["internationalFallback"] = {
                    "homeMarket": home_config.market_country,
                    "sourceMarket": market_config.market_country,
                    "fxFreshness": {
                        "stale": fx.stale,
                        "health": fx.health,
                        "allowsConversion": fx.allows_conversion,
                        "ageHours": fx.age_hours,
                        "rateTimestamp": fx.rate_timestamp.isoformat().replace("+00:00", "Z"),
                        "providerRateDate": (
                            fx.provider_rate_date.isoformat() if fx.provider_rate_date else None
                        ),
                        "fetchedAt": (
                            fx.fetched_at.isoformat().replace("+00:00", "Z")
                            if fx.fetched_at
                            else None
                        ),
                        "source": fx.source,
                        "blockReason": fx.block_reason,
                    },
                }
                return (
                    provider_request,
                    provider_result,
                    evaluated_comps,
                    source_stats,
                    display_stats,
                    conversion,
                    attempts,
                    market_config.market_country,
                )
            except Exception as exc:
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
        raise ValueError("international_fallback_exhausted")

    def build_international_snapshot_payload(
        self,
        *,
        price_key: MarketPriceKey,
        provider_request: ProviderRequest,
        provider_result: ProviderResult,
        evaluated_comps: list[EvaluatedComp],
        pricing_stats: PricingStats,
        source_pricing_stats: PricingStats,
        currency_conversion: CurrencyConversion,
        fallback_attempts: list[dict[str, Any]],
        source_market_country: str,
        now: datetime,
    ) -> dict[str, Any]:
        fallback_level = int(fallback_attempts[-1].get("fallbackLevel") or 1) if fallback_attempts else 1
        source_name = market_display_name(source_market_country)
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
                "staleAfter": utc_iso(international_stale_after(confidence=pricing_stats.confidence, now=now)),
                "pricingPolicy": "international_market_estimate",
                "evidenceType": "completed_sale",
                "priceClass": "international_estimate",
                "requestedMarketplace": f"EBAY_{price_key.market_country.upper()}",
                "marketplaceActuallyUsed": provider_request.provider_marketplace_id,
                "sourceMarketCountry": source_market_country,
                "sourceMarketName": source_name,
                "fallbackLevel": fallback_level,
                "fallbackAttempts": fallback_attempts,
                "fallbackReason": "local_and_reference_insufficient",
                "originalCurrency": provider_request.currency,
                "displayCurrency": price_key.currency.upper(),
                "sourcePriceViews": build_price_view_diagnostics(source_pricing_stats),
                "currencyConversion": currency_conversion.metadata(
                    source_amount=source_pricing_stats.recommended_price,
                    converted_amount=pricing_stats.recommended_price,
                ),
                "shippingTreatment": "item_price_excluding_shipping",
                "priceViews": build_price_view_diagnostics(pricing_stats),
                "fetchedCount": len(provider_result.comps),
                "marketCountry": price_key.market_country.upper(),
                "currency": price_key.currency.upper(),
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
                "url_quality_counts": url_quality_counts(provider_result),
            },
        }

    def run_international_fallback_job(self, job: MarketPriceRefreshJob) -> dict[str, Any]:
        if not job.id:
            raise ValueError("Market refresh job is missing id")
        now = self.now_func()
        price_key = self.client.get_price_key(job.price_key_id)
        prior_cache = None
        if hasattr(self.client, "get_cache_row"):
            try:
                prior_cache = self.client.get_cache_row(price_key_id=price_key.id)
            except Exception:
                prior_cache = None
        eligibility = evaluate_international_fallback_eligibility(
            price_key=price_key,
            cache=prior_cache,
            now=now,
        )
        if not eligibility.eligible:
            self.client.fail_job(
                job_id=job.id,
                error_message=f"international_fallback_skipped:{eligibility.reason}",
                retryable=False,
                retry_delay_minutes=60,
            )
            return {
                "jobId": job.id,
                "priceKeyId": price_key.id,
                "status": "skipped",
                "reason": eligibility.reason,
            }

        # Fail closed on FX before any foreign-market browser search.
        fx_probe = evaluate_fx_freshness(now=now)
        if not fx_probe.allows_conversion:
            message = fx_probe.block_reason or "FX_RATE_STALE_NO_SAFE_CONVERSION"
            backoff = now + timedelta(hours=6)
            self.client.fail_job(
                job_id=job.id,
                error_message=message,
                retryable=True,
                retry_delay_minutes=6 * 60,
            )
            if hasattr(self.client, "mark_cache_failure"):
                self.client.mark_cache_failure(
                    price_key_id=price_key.id,
                    error_message=message,
                    next_refresh_due_at=backoff,
                    market_country=price_key.market_country,
                    currency=price_key.currency,
                )
            return {
                "jobId": job.id,
                "priceKeyId": price_key.id,
                "status": "failed",
                "error": message,
                "fxHealth": fx_probe.health,
            }

        reason_payload = parse_international_job_reason(job.reason) or {}
        target_market = reason_payload.get("targetMarket")
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
                source_market_country,
            ) = self.fetch_international_estimate_result(
                price_key=price_key,
                provider_marketplace=provider_marketplace,
                now=now,
                target_market=target_market,
            )
        except ValueError as exc:
            message = str(exc)
            backoff = now + timedelta(hours=24)
            self.client.fail_job(
                job_id=job.id,
                error_message=message,
                retryable=True,
                retry_delay_minutes=24 * 60,
            )
            if hasattr(self.client, "mark_cache_failure"):
                self.client.mark_cache_failure(
                    price_key_id=price_key.id,
                    error_message=message,
                    next_refresh_due_at=backoff,
                    market_country=price_key.market_country,
                    currency=price_key.currency,
                )
            return {"jobId": job.id, "priceKeyId": price_key.id, "status": "failed", "error": message}

        movement = evaluate_price_movement(
            old_price=(prior_cache or {}).get("current_market_price"),
            new_price=pricing_stats.recommended_price,
            included_count=int(pricing_stats.included_count or 0),
            confidence=pricing_stats.confidence,
        )
        provider_result.raw_metadata["priceMovement"] = movement_diagnostics(movement)
        if movement.action in {"pending_verification", "reject_weak"}:
            pricing_stats = replace(
                pricing_stats,
                recommended_price=(prior_cache or {}).get("current_market_price"),
            )
            provider_result.raw_metadata["priceMovement"]["cacheWriteMode"] = "preserve_prior_pending_verification"

        fx = evaluate_fx_freshness(
            now=now,
            same_currency=currency_conversion.source_currency
            == currency_conversion.target_currency,
        )
        assert_fx_allows_international_conversion(fx)
        snapshot_payload = self.build_international_snapshot_payload(
            price_key=price_key,
            provider_request=provider_request,
            provider_result=provider_result,
            evaluated_comps=evaluated_comps,
            pricing_stats=pricing_stats,
            source_pricing_stats=source_pricing_stats,
            currency_conversion=currency_conversion,
            fallback_attempts=fallback_attempts,
            source_market_country=source_market_country,
            now=now,
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
        cache_payload = build_international_cache_payload(
            price_key=price_key,
            provider_result=provider_result,
            pricing_stats=pricing_stats,
            snapshot_id=str(snapshot["id"]),
            refreshed_at=now,
            source_market_country=source_market_country,
            source_currency=provider_request.currency,
            source_stats=source_pricing_stats,
            conversion=currency_conversion,
            fallback_reason="local_and_reference_insufficient",
            fx_freshness={
                "stale": fx.stale,
                "ageHours": fx.age_hours,
                "rateTimestamp": fx.rate_timestamp.isoformat().replace("+00:00", "Z"),
                "providerRateDate": (
                    fx.provider_rate_date.isoformat() if fx.provider_rate_date else None
                ),
                "fetchedAt": (
                    fx.fetched_at.isoformat().replace("+00:00", "Z") if fx.fetched_at else None
                ),
                "source": fx.source,
                "health": fx.health,
            },
        )
        cache = self.client.upsert_cache(cache_payload)
        stale_after = international_stale_after(confidence=pricing_stats.confidence, now=now)
        self.client.complete_job(
            job_id=job.id,
            snapshot_id=str(snapshot["id"]),
            cache_updated_at=now,
            stale_after=stale_after,
            next_refresh_due_at=stale_after,
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
            "sourceMarketCountry": source_market_country,
            "sourceCurrency": provider_request.currency,
            "currency": price_key.currency.upper(),
            "marketplace": provider_request.provider_marketplace_id,
            "fallbackLevel": int(fallback_attempts[-1].get("fallbackLevel") or 1),
            "priceClass": "international_estimate",
            "status": "completed",
        }


def _market_currency(market_country: str) -> str:
    return {
        "AU": "AUD",
        "US": "USD",
        "GB": "GBP",
        "CA": "CAD",
    }[market_country.upper()]


class InternationalMarketPriceJobRunner(InternationalFallbackMixin, MarketPriceJobRunner):
    def run_job(self, job: MarketPriceRefreshJob) -> dict[str, Any]:
        if self.is_international_fallback_job(job):
            return self.run_international_fallback_job(job)
        return super().run_job(job)
