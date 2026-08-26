"""Orchestrate bulk/reference refresh into shared market_price_cache."""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..config import MarketEngineConfig
from ..currency_conversion import resolve_currency_conversion
from ..models import MarketPriceKey
from ..refresh_policy import RefreshCooldownConfig, calculate_refresh_policy
from ..supabase_client import SupabaseMarketEngineClient
from .coverage_diagnostic import CoverageProbeResult, aggregate_coverage, classify_key
from .display_price_policy import DisplayPriceDecision, decide_display_price
from .price_semantics import ReferencePriceObservation
from .set_id_aliases import is_synthetic_set_code, resolve_static_set_id, resolve_tcgdex_set_id
from .static_price_index import lookup_static_reference
from .sync_lock import acquire_bulk_sync_lock, release_bulk_sync_lock
from .tcgdex_client import TcgdexRunCache, lookup_tcgdex_reference
from .verification_router import VerificationRouteDecision, route_verification


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class BulkRefreshCounters:
    keys_scanned: int = 0
    keys_matched: int = 0
    keys_updated: int = 0
    keys_unchanged: int = 0
    keys_quarantined: int = 0
    keys_unresolved: int = 0
    keys_ambiguous: int = 0
    verification_enqueued: int = 0
    errors: int = 0
    error_details: list[dict[str, Any]] = field(default_factory=list)
    provider_hits: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class BulkRefreshConfig:
    dry_run: bool
    max_keys: int
    enable_live_tcgdex: bool
    verification_budget_per_run: int
    high_value_threshold: float
    reference_fresh_hours: int

    @classmethod
    def from_env(cls) -> "BulkRefreshConfig":
        def _bool(name: str, default: bool) -> bool:
            raw = os.getenv(name, "true" if default else "false").strip().lower()
            return raw in {"1", "true", "yes", "y", "on"}

        def _int(name: str, default: int) -> int:
            return max(1, int(os.getenv(name, str(default)).strip() or default))

        return cls(
            dry_run=_bool("BULK_REFERENCE_DRY_RUN", False),
            max_keys=_int("BULK_REFERENCE_MAX_KEYS_PER_RUN", 5000),
            enable_live_tcgdex=_bool("BULK_REFERENCE_ENABLE_LIVE_TCGDEX", True),
            verification_budget_per_run=_int("BULK_REFERENCE_VERIFICATION_BUDGET", 25),
            high_value_threshold=float(os.getenv("BULK_REFERENCE_HIGH_VALUE_THRESHOLD", "50")),
            reference_fresh_hours=_int("BULK_REFERENCE_FRESH_HOURS", 24),
        )


class BulkReferenceRefreshRunner:
    def __init__(
        self,
        *,
        client: SupabaseMarketEngineClient,
        engine_config: MarketEngineConfig,
        refresh_config: BulkRefreshConfig | None = None,
        now_func: Callable[[], datetime] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.engine_config = engine_config
        self.refresh_config = refresh_config or BulkRefreshConfig.from_env()
        self.now_func = now_func or (lambda: datetime.now(timezone.utc))
        self.logger = logger or (lambda msg: None)
        self.cooldown_config = RefreshCooldownConfig.from_env()
        self.tcgdex_cache = TcgdexRunCache()

    def _lookup_reference(self, key: MarketPriceKey) -> ReferencePriceObservation | None:
        obs = lookup_static_reference(
            game=key.game,
            language=key.language,
            set_code=key.set_code,
            collector_number=key.collector_number,
            card_name=key.card_name,
            normalized_card_name=key.normalized_card_name,
            variant=key.variant,
        )
        if obs is not None:
            return obs
        if not self.refresh_config.enable_live_tcgdex:
            return None
        return lookup_tcgdex_reference(
            language=key.language,
            set_code=key.set_code,
            collector_number=key.collector_number,
            card_name=key.card_name,
            normalized_card_name=key.normalized_card_name,
            variant=key.variant,
            cache=self.tcgdex_cache,
        )

    def _convert_price(self, observation: ReferencePriceObservation, target_currency: str, now: datetime) -> float:
        conversion = resolve_currency_conversion(
            source_currency=observation.source_currency,
            target_currency=target_currency,
            rates=self.engine_config.currency_rates,
            rate_source=self.engine_config.currency_rate_source,
            now=now,
        )
        converted = conversion.amount(observation.market_price)
        if converted is None:
            raise ValueError(f"Unable to convert {observation.source_currency} to {target_currency}")
        return converted

    def _build_snapshot_payload(
        self,
        *,
        key: MarketPriceKey,
        observation: ReferencePriceObservation,
        display: DisplayPriceDecision,
        now: datetime,
    ) -> dict[str, Any]:
        return {
            "price_key_id": key.id,
            "provider": observation.provider,
            "marketplace": "REFERENCE",
            "query_used": f"bulk_reference:{observation.provider}",
            "median_price": display.reference_price,
            "low_price": observation.low_price,
            "average_price": display.reference_price,
            "high_price": observation.high_price,
            "recommended_price": display.reference_price,
            "sample_size": 1,
            "confidence": display.confidence,
            "included_count": 1 if observation.is_usable else 0,
            "rejected_count": 0,
            "diagnostics_json": {
                "bulkReference": True,
                "priceEvidenceKind": "reference",
                "displayPriceSource": display.display_source,
                "mappingStatus": observation.mapping_status,
                "sourceMarket": observation.source_market,
                "sourceCurrency": observation.source_currency,
                "sourceRecordId": observation.source_record_id,
                "displayDecision": display.action,
                "verificationRequired": display.verification_required,
                "verificationReason": display.verification_reason,
                "priceMovement": (
                    {
                        "action": display.movement.action,
                        "reason": display.movement.reason,
                    }
                    if display.movement
                    else None
                ),
                **(observation.diagnostics or {}),
                **(display.diagnostics or {}),
            },
        }

    def _build_cache_payload(
        self,
        *,
        key: MarketPriceKey,
        observation: ReferencePriceObservation,
        display: DisplayPriceDecision,
        snapshot_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        policy = calculate_refresh_policy(
            cache_row={
                "current_market_price": display.display_price,
                "recommended_price": display.reference_price,
                "last_updated_at": utc_iso(now),
            },
            price_key_row={
                "popularity_score": key.raw.get("popularity_score"),
                "inventory_count": key.raw.get("inventory_count"),
            },
            active_job=None,
            now=now,
            config=self.cooldown_config,
        )
        stale_after = policy.cooldown_until or (now + timedelta(hours=self.refresh_config.reference_fresh_hours))
        return {
            "price_key_id": key.id,
            "current_market_price": display.display_price,
            "median_price": display.reference_price,
            "low_price": observation.low_price,
            "average_price": display.reference_price,
            "high_price": observation.high_price,
            "recommended_price": display.reference_price,
            "sample_size": 1,
            "confidence": display.confidence,
            "provider": display.provider or observation.provider,
            "marketplace": display.marketplace or "REFERENCE",
            "market_country": key.market_country.upper(),
            "currency": key.currency.upper(),
            "last_updated_at": utc_iso(now),
            "stale_after": utc_iso(stale_after),
            "next_refresh_due_at": utc_iso(stale_after),
            "refresh_status": "completed",
            "latest_snapshot_id": snapshot_id,
            "last_error_message": None,
            "reference_price": display.reference_price,
            "reference_provider": display.reference_provider,
            "reference_updated_at": utc_iso(now),
            "display_price_source": display.display_source,
            "verification_required": display.verification_required,
            "verification_reason": display.verification_reason,
        }

    def process_key(
        self,
        *,
        key: MarketPriceKey,
        prior_cache: dict[str, Any] | None,
        counters: BulkRefreshCounters,
        verification_budget: list[int],
    ) -> dict[str, Any]:
        counters.keys_scanned += 1
        now = self.now_func()
        if is_synthetic_set_code(key.set_code, key.set_name):
            return {"status": "skipped_synthetic", "priceKeyId": key.id}
        observation = self._lookup_reference(key)
        if observation is None:
            counters.keys_unresolved += 1
            return {"status": "unresolved", "priceKeyId": key.id}
        if observation.mapping_status == "ambiguous":
            counters.keys_ambiguous += 1
            counters.keys_quarantined += 1
            return {"status": "ambiguous", "priceKeyId": key.id}
        if not observation.is_usable:
            counters.keys_unresolved += 1
            return {"status": "unusable", "priceKeyId": key.id}

        counters.keys_matched += 1
        counters.provider_hits[observation.provider] = counters.provider_hits.get(observation.provider, 0) + 1
        converted = self._convert_price(observation, key.currency, now)
        display = decide_display_price(
            prior_cache=prior_cache,
            observation=observation,
            converted_price=converted,
            target_currency=key.currency,
            now=now,
        )
        value_signal = max(
            float(prior_cache.get("current_market_price") or 0) if prior_cache else 0.0,
            float(display.display_price or 0),
            converted,
        )
        route = route_verification(
            prior_cache=prior_cache,
            observation=observation,
            display=display,
            value_signal=value_signal,
            high_value_threshold=self.refresh_config.high_value_threshold,
            now=now,
        )

        result = {
            "status": display.action,
            "priceKeyId": key.id,
            "provider": observation.provider,
            "mappingStatus": observation.mapping_status,
            "displaySource": display.display_source,
            "verificationRequired": display.verification_required,
            "verificationRoute": route.reason,
        }

        if display.action in {"pending_verification", "reject_reference"}:
            counters.keys_quarantined += 1
        elif display.action == "no_change":
            counters.keys_unchanged += 1
            return result
        elif display.action in {"apply_reference", "preserve_verified"}:
            counters.keys_updated += 1

        if self.refresh_config.dry_run:
            return result

        snapshot_id = str(uuid.uuid4())
        snapshot_payload = self._build_snapshot_payload(key=key, observation=observation, display=display, now=now)
        snapshot_payload["id"] = snapshot_id
        self.client.insert_snapshot(snapshot_payload)
        cache_payload = self._build_cache_payload(
            key=key,
            observation=observation,
            display=display,
            snapshot_id=snapshot_id,
            now=now,
        )
        self.client.upsert_cache(cache_payload)

        if route.should_verify and verification_budget[0] > 0:
            try:
                self.client.enqueue_refresh_job(
                    price_key_id=key.id,
                    reason=f"bulk_verify:{route.reason}",
                    priority=route.priority,
                    dedupe_key=f"bulk_verify:{key.id}",
                )
                verification_budget[0] -= 1
                counters.verification_enqueued += 1
            except Exception as exc:
                counters.errors += 1
                self.logger(f"[bulk-reference] verify enqueue failed key={key.id}: {exc}")

        return result

    def list_keys(self) -> list[MarketPriceKey]:
        rows = self.client._table_get(
            "market_price_keys",
            params={
                "select": "*",
                "order": "last_seen_at.desc.nullslast,updated_at.desc",
                "limit": str(self.refresh_config.max_keys),
            },
        )
        return [MarketPriceKey.from_row(row) for row in rows]

    def load_cache_map(self, key_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not key_ids:
            return {}
        in_filter = "(" + ",".join(key_ids) + ")"
        rows = self.client._table_get(
            "market_price_cache",
            params={
                "select": (
                    "price_key_id,current_market_price,recommended_price,provider,marketplace,"
                    "reference_price,reference_provider,reference_updated_at,display_price_source,"
                    "verification_required,verification_reason,confidence,last_updated_at"
                ),
                "price_key_id": f"in.{in_filter}",
                "limit": str(len(key_ids)),
            },
        )
        return {str(row["price_key_id"]): row for row in rows if row.get("price_key_id")}

    def run(self) -> dict[str, Any]:
        lock = acquire_bulk_sync_lock()
        if not lock.acquired:
            return {
                "status": "skipped",
                "reason": "overlap_lock",
                "lockHolderPid": lock.holder_pid,
                "lockStartedAtUtc": lock.started_at_utc,
            }
        started = time.monotonic()
        now = self.now_func()
        counters = BulkRefreshCounters()
        try:
            return self._run_locked(started=started, now=now, counters=counters)
        finally:
            release_bulk_sync_lock()

    def _run_locked(self, *, started: float, now: datetime, counters: BulkRefreshCounters) -> dict[str, Any]:
        keys = self.list_keys()
        cache_map = self.load_cache_map([key.id for key in keys])
        verification_budget = [self.refresh_config.verification_budget_per_run]
        samples: list[dict[str, Any]] = []

        coverage_rows: list[CoverageProbeResult] = []

        if self.refresh_config.enable_live_tcgdex:
            unique_sets: set[tuple[str, str]] = set()
            for key in keys:
                resolved = resolve_static_set_id(key.set_code)
                if resolved:
                    unique_sets.add((key.language, resolve_tcgdex_set_id(key.set_code) or resolved))
            for language, set_id in sorted(unique_sets):
                self.tcgdex_cache.preload_set(language=language, set_id=set_id)

        for key in keys:
            try:
                outcome = self.process_key(
                    key=key,
                    prior_cache=cache_map.get(key.id),
                    counters=counters,
                    verification_budget=verification_budget,
                )
                if len(samples) < 25:
                    samples.append(outcome)
                prior = cache_map.get(key.id)
                converted = None
                if outcome.get("provider"):
                    try:
                        obs = self._lookup_reference(key)
                        if obs and obs.is_usable:
                            converted = self._convert_price(obs, key.currency, now)
                    except Exception:
                        converted = None
                coverage_rows.append(
                    classify_key(
                        key,
                        tcgdx_cache=self.tcgdex_cache,
                        prior_cache=prior,
                        converted_price=converted,
                    )
                )
            except Exception as exc:
                counters.errors += 1
                detail = {
                    "priceKeyId": key.id,
                    "setCode": key.set_code,
                    "stage": "process_key",
                    "error": str(exc),
                }
                counters.error_details.append(detail)
                self.logger(f"[bulk-reference] key={key.id} error={exc}")

        elapsed_ms = int((time.monotonic() - started) * 1000)
        keys_per_hour = 0.0
        if elapsed_ms > 0:
            keys_per_hour = round((counters.keys_scanned / elapsed_ms) * 3_600_000, 2)

        coverage = aggregate_coverage(coverage_rows)
        report = {
            "status": "success",
            "dryRun": self.refresh_config.dry_run,
            "startedAtUtc": utc_iso(now),
            "durationMs": elapsed_ms,
            "keysScanned": counters.keys_scanned,
            "keysMatched": counters.keys_matched,
            "keysUpdated": counters.keys_updated,
            "keysUnchanged": counters.keys_unchanged,
            "keysQuarantined": counters.keys_quarantined,
            "keysUnresolved": counters.keys_unresolved,
            "keysAmbiguous": counters.keys_ambiguous,
            "verificationEnqueued": counters.verification_enqueued,
            "errors": counters.errors,
            "errorDetails": counters.error_details,
            "providerHits": counters.provider_hits,
            "bulkKeysPerHour": keys_per_hour,
            "coverage": coverage,
            "sampleOutcomes": samples,
        }

        if not self.refresh_config.dry_run and hasattr(self.client, "record_provider_sync_run"):
            try:
                self.client.record_provider_sync_run(
                    provider="bulk_reference",
                    status="success" if counters.errors == 0 else "failed",
                    counters=report,
                    duration_ms=elapsed_ms,
                )
            except Exception as exc:
                self.logger(f"[bulk-reference] sync state write failed: {exc}")

        return report
