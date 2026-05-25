from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from .models import MarketPriceKey, MarketPriceRefreshJob


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone().isoformat().replace("+00:00", "Z")


class SupabaseMarketEngineClient:
    def __init__(self, *, supabase_url: str, service_role_key: str, timeout_seconds: int = 30) -> None:
        self.supabase_url = supabase_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Content-Type": "application/json",
            }
        )

    def _rpc(self, name: str, payload: dict[str, Any]) -> Any:
        response = self.session.post(
            f"{self.supabase_url}/rest/v1/rpc/{name}",
            json=payload,
            headers={"Prefer": "return=representation"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def _table_get(self, table: str, *, params: dict[str, Any]) -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.supabase_url}/rest/v1/{table}",
            params=params,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return list(response.json())

    def _table_post(
        self,
        table: str,
        payload: dict[str, Any] | list[dict[str, Any]],
        *,
        prefer: str = "return=representation",
        on_conflict: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        response = self.session.post(
            f"{self.supabase_url}/rest/v1/{table}",
            params=params,
            json=payload,
            headers={"Prefer": prefer},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return data
        return [data]

    def claim_jobs(self, *, worker_id: str, max_jobs: int) -> list[MarketPriceRefreshJob]:
        rows = self._rpc(
            "claim_market_price_refresh_jobs",
            {"p_worker_id": worker_id, "p_max_jobs": max_jobs},
        )
        return [MarketPriceRefreshJob.from_row(row) for row in rows]

    def get_price_key(self, price_key_id: str) -> MarketPriceKey:
        rows = self._table_get(
            "market_price_keys",
            params={"id": f"eq.{price_key_id}", "select": "*", "limit": 1},
        )
        if not rows:
            raise LookupError(f"Market price key not found: {price_key_id}")
        return MarketPriceKey.from_row(rows[0])

    def insert_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._table_post("market_price_snapshots", payload)[0]

    def insert_evidence(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        return self._table_post("market_sold_listing_evidence", rows)

    def upsert_cache(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._table_post(
            "market_price_cache",
            payload,
            prefer="resolution=merge-duplicates,return=representation",
            on_conflict="price_key_id",
        )[0]

    def complete_job(
        self,
        *,
        job_id: str,
        snapshot_id: str,
        cache_updated_at: datetime,
        stale_after: datetime,
        next_refresh_due_at: datetime,
    ) -> dict[str, Any]:
        return self._rpc(
            "complete_market_price_refresh_job",
            {
                "p_job_id": job_id,
                "p_snapshot_id": snapshot_id,
                "p_cache_updated_at": _iso_or_none(cache_updated_at),
                "p_stale_after": _iso_or_none(stale_after),
                "p_next_refresh_due_at": _iso_or_none(next_refresh_due_at),
            },
        )

    def fail_job(self, *, job_id: str, error_message: str) -> dict[str, Any]:
        return self._rpc(
            "fail_market_price_refresh_job",
            {
                "p_job_id": job_id,
                "p_error_message": error_message[:1000],
                "p_retryable": True,
                "p_retry_delay_minutes": 15,
                "p_max_attempts": 3,
            },
        )
