from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .cache_writer import build_cache_payload
from .config import MarketEngineConfig
from .filters import filter_comps
from .models import EvaluatedComp, MarketPriceKey, MarketPriceRefreshJob, PricingStats, ProviderResult
from .pricing_stats import calculate_pricing_stats


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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

    def claim_jobs(self, *, max_jobs: int | None = None) -> list[MarketPriceRefreshJob]:
        limit = max_jobs or self.config.max_jobs_per_run
        return self.client.claim_jobs(worker_id=self.config.worker_id, max_jobs=limit)

    def build_snapshot_payload(
        self,
        *,
        price_key: MarketPriceKey,
        provider_result: ProviderResult,
        evaluated_comps: list[EvaluatedComp],
        pricing_stats: PricingStats,
        now: datetime,
    ) -> dict[str, Any]:
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
                "fetchedCount": len(provider_result.comps),
                "includedListingIds": [
                    item.comp.source_listing_id for item in evaluated_comps if item.included_in_estimate
                ],
                "rejectedReasons": {
                    item.comp.source_listing_id: item.rejection_reason
                    for item in evaluated_comps
                    if item.rejection_reason
                },
            },
        }

    def build_evidence_rows(
        self,
        *,
        price_key: MarketPriceKey,
        snapshot_id: str,
        provider_result: ProviderResult,
        evaluated_comps: list[EvaluatedComp],
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
                    "sold_date": utc_iso(item.comp.sold_date),
                    "listing_url": item.comp.listing_url,
                    "condition_text": item.comp.condition_text,
                    "match_score": item.match_score,
                    "included_in_estimate": item.included_in_estimate,
                    "rejection_reason": item.rejection_reason,
                    "raw_json": {
                        "sourceListingId": item.comp.source_listing_id,
                        "providerFingerprint": provider_result.provider_fingerprint,
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
        try:
            price_key = self.client.get_price_key(job.price_key_id)
            if not price_key.id:
                raise ValueError(f"Market price key row missing id for job {job.id}")
            if not price_key.fingerprint:
                raise ValueError(f"Market price key row missing fingerprint for job {job.id}")
            self.logger(f"[market-engine] processing job={job.id} key={price_key.fingerprint}")
            provider_result = self.provider.fetch_comps(price_key)
            evaluated_comps = filter_comps(price_key, provider_result.comps)
            pricing_stats = calculate_pricing_stats(evaluated_comps, now=now, config=self.config)
            snapshot_payload = self.build_snapshot_payload(
                price_key=price_key,
                provider_result=provider_result,
                evaluated_comps=evaluated_comps,
                pricing_stats=pricing_stats,
                now=now,
            )
            snapshot = self.client.insert_snapshot(snapshot_payload)
            evidence_rows = self.build_evidence_rows(
                price_key=price_key,
                snapshot_id=str(snapshot["id"]),
                provider_result=provider_result,
                evaluated_comps=evaluated_comps,
            )
            self.client.insert_evidence(evidence_rows)
            cache_payload = build_cache_payload(
                price_key=price_key,
                provider_result=provider_result,
                pricing_stats=pricing_stats,
                snapshot_id=str(snapshot["id"]),
                refreshed_at=now,
            )
            self.client.upsert_cache(cache_payload)
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
                "includedCount": pricing_stats.included_count,
                "rejectedCount": pricing_stats.rejected_count,
                "confidence": pricing_stats.confidence,
                "recommendedPrice": pricing_stats.recommended_price,
                "status": "completed",
            }
        except Exception as exc:
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
            if fail_job_error:
                result["failJobError"] = fail_job_error
            return result

    def run_once(self, *, max_jobs: int | None = None) -> list[dict[str, Any]]:
        jobs = self.claim_jobs(max_jobs=max_jobs)
        if not jobs:
            self.logger("[market-engine] no queued jobs claimed")
            return []
        return [self.run_job(job) for job in jobs]
