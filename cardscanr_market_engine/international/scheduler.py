"""Bounded scheduler for international pricing fallback jobs."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..config import MarketEngineConfig
from ..models import MarketPriceKey
from ..supabase_client import SupabaseMarketEngineClient
from .fallback_eligibility import evaluate_international_fallback_eligibility


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class InternationalFallbackSchedulerConfig:
    max_candidates: int
    max_enqueues: int
    dry_run: bool

    @classmethod
    def from_env(cls) -> "InternationalFallbackSchedulerConfig":
        def _int(name: str, default: int) -> int:
            return max(1, int(os.getenv(name, str(default)).strip() or default))

        dry_run = os.getenv("INTERNATIONAL_FALLBACK_DRY_RUN", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return cls(
            max_candidates=_int("INTERNATIONAL_FALLBACK_MAX_CANDIDATES", 50),
            max_enqueues=_int("INTERNATIONAL_FALLBACK_MAX_ENQUEUES", 5),
            dry_run=dry_run,
        )


class InternationalFallbackScheduler:
    def __init__(
        self,
        *,
        client: SupabaseMarketEngineClient,
        engine_config: MarketEngineConfig,
        config: InternationalFallbackSchedulerConfig | None = None,
    ) -> None:
        self.client = client
        self.engine_config = engine_config
        self.config = config or InternationalFallbackSchedulerConfig.from_env()

    def _load_candidates(self) -> list[dict[str, Any]]:
        if hasattr(self.client, "list_international_fallback_candidates"):
            return self.client.list_international_fallback_candidates(
                limit=self.config.max_candidates,
            )
        return self.client.list_cache_refresh_candidates(
            limit=self.config.max_candidates,
            min_popularity_score=0,
            min_inventory_count=0,
        )

    def run(self) -> dict[str, Any]:
        now = utc_now()
        candidates = self._load_candidates()
        enqueued: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in candidates:
            key_id = str(row.get("id") or "").strip()
            if not key_id:
                continue
            key = MarketPriceKey.from_row(
                {
                    "id": key_id,
                    "game": row.get("game") or "pokemon",
                    "card_name": row.get("card_name") or "",
                    "normalized_card_name": row.get("normalized_card_name") or "",
                    "set_name": row.get("set_name") or "",
                    "set_code": row.get("set_code"),
                    "collector_number": row.get("collector_number") or "",
                    "language": row.get("language") or "en",
                    "variant": row.get("variant") or "raw",
                    "condition": row.get("condition") or "raw",
                    "market_country": row.get("market_country") or "AU",
                    "currency": row.get("currency") or "AUD",
                    "fingerprint": row.get("fingerprint") or "",
                }
            )
            cache = {
                "current_market_price": row.get("current_market_price"),
                "recommended_price": row.get("recommended_price"),
                "display_price_source": row.get("display_price_source"),
                "verification_required": row.get("verification_required"),
                "provider": row.get("provider"),
                "next_refresh_due_at": row.get("next_refresh_due_at"),
                "stale_after": row.get("stale_after"),
                "last_error_message": row.get("last_error_message"),
            }
            eligibility = evaluate_international_fallback_eligibility(
                price_key=key,
                cache=cache,
                now=now,
            )
            if not eligibility.eligible:
                skipped.append(
                    {
                        "priceKeyId": key.id,
                        "reason": eligibility.reason,
                        "fallbackMarkets": list(eligibility.fallback_markets),
                    }
                )
                continue
            target_market = eligibility.fallback_markets[0]
            reason = f"international_fallback:{target_market}"
            if self.config.dry_run:
                enqueued.append(
                    {
                        "priceKeyId": key.id,
                        "reason": reason,
                        "dryRun": True,
                    }
                )
            else:
                job = self.client.enqueue_refresh_job(
                    price_key_id=key.id,
                    reason=reason,
                    priority=95,
                )
                enqueued.append(
                    {
                        "priceKeyId": key.id,
                        "reason": reason,
                        "jobId": job.get("id"),
                        "status": job.get("status"),
                    }
                )
            if len(enqueued) >= self.config.max_enqueues:
                break
        return {
            "checkedAtUtc": utc_iso(now),
            "dryRun": self.config.dry_run,
            "candidatesScanned": len(candidates),
            "enqueued": len(enqueued),
            "skipped": len(skipped),
            "enqueuedJobs": enqueued,
            "skippedSamples": skipped[:20],
        }
