from __future__ import annotations

from datetime import datetime
import re
from typing import Any

import requests

from .models import MarketPriceKey, MarketPriceRefreshJob

UUID_PATTERN = re.compile(r"^[0-9a-fA-F-]{1,64}$")


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
        if not isinstance(rows, list):
            raise ValueError("claim_market_price_refresh_jobs returned unexpected payload shape")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"claimed job row at index {index} is not an object")
            if "id" not in row or "price_key_id" not in row:
                raise ValueError(f"claimed job row missing required fields at index {index}")
        return [MarketPriceRefreshJob.from_row(row) for row in rows]

    def get_price_key(self, price_key_id: str) -> MarketPriceKey:
        rows = self._table_get(
            "market_price_keys",
            params={"id": f"eq.{price_key_id}", "select": "*", "limit": 1},
        )
        if not rows:
            raise LookupError(f"Market price key not found: {price_key_id}")
        if "id" not in rows[0] or "fingerprint" not in rows[0]:
            raise ValueError(f"Market price key row missing required fields for id: {price_key_id}")
        return MarketPriceKey.from_row(rows[0])

    def get_or_create_price_key(
        self,
        *,
        game: str,
        card_name: str,
        normalized_card_name: str,
        set_name: str,
        set_code: str,
        collector_number: str,
        language: str,
        variant: str,
        condition: str,
        market_country: str,
        currency: str,
        fingerprint: str,
    ) -> str:
        key_id = self._rpc(
            "get_or_create_market_price_key",
            {
                "p_game": game,
                "p_card_name": card_name,
                "p_normalized_card_name": normalized_card_name,
                "p_set_name": set_name,
                "p_set_code": set_code,
                "p_collector_number": collector_number,
                "p_language": language,
                "p_variant": variant,
                "p_condition": condition,
                "p_market_country": market_country,
                "p_currency": currency,
                "p_fingerprint": fingerprint,
            },
        )
        if not key_id:
            raise ValueError("get_or_create_market_price_key returned an empty id")
        return str(key_id)

    def enqueue_refresh_job(
        self,
        *,
        price_key_id: str,
        reason: str,
        priority: int,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        row = self._rpc(
            "enqueue_market_price_refresh",
            {
                "p_price_key_id": price_key_id,
                "p_reason": reason,
                "p_priority": priority,
                "p_requested_by_user_id": None,
                "p_dedupe_key": dedupe_key,
            },
        )
        if isinstance(row, list):
            if not row:
                raise ValueError("enqueue_market_price_refresh returned an empty list")
            row = row[0]
        if not isinstance(row, dict):
            raise ValueError("enqueue_market_price_refresh returned unexpected payload shape")
        if "id" not in row or "status" not in row:
            raise ValueError("enqueue_market_price_refresh returned row missing id/status")
        return row

    def get_market_price_bundle(self, *, fingerprint: str, evidence_limit: int = 50) -> dict[str, Any] | None:
        bundle = self._rpc(
            "get_market_price_bundle",
            {"p_fingerprint": fingerprint, "p_evidence_limit": evidence_limit},
        )
        if bundle is None:
            return None
        if not isinstance(bundle, dict):
            raise ValueError("get_market_price_bundle returned unexpected payload shape")
        return bundle

    def list_missing_cache_keys(
        self,
        *,
        limit: int,
        min_popularity_score: int = 0,
        min_inventory_count: int = 0,
    ) -> list[dict[str, Any]]:
        return self._table_get(
            "market_price_keys",
            params={
                "select": (
                    "id,fingerprint,market_country,currency,popularity_score,inventory_count,last_seen_at,"
                    "market_price_cache!left(price_key_id,marketplace)"
                ),
                "market_price_cache.price_key_id": "is.null",
                "popularity_score": f"gte.{max(0, min_popularity_score)}",
                "inventory_count": f"gte.{max(0, min_inventory_count)}",
                "order": "last_seen_at.desc.nullslast,updated_at.desc",
                "limit": max(1, min(limit, 1000)),
            },
        )

    def list_stale_cache_keys(
        self,
        *,
        stale_before_iso: str,
        limit: int,
        min_popularity_score: int = 0,
        min_inventory_count: int = 0,
    ) -> list[dict[str, Any]]:
        rows = self._table_get(
            "market_price_cache",
            params={
                "select": (
                    "price_key_id,stale_after,current_market_price,recommended_price,last_updated_at,marketplace,"
                    "market_price_keys!inner(id,fingerprint,market_country,currency,popularity_score,inventory_count,last_seen_at)"
                ),
                "stale_after": f"lt.{stale_before_iso}",
                "market_price_keys.popularity_score": f"gte.{max(0, min_popularity_score)}",
                "market_price_keys.inventory_count": f"gte.{max(0, min_inventory_count)}",
                "order": "stale_after.asc",
                "limit": max(1, min(limit, 1000)),
            },
        )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            key = row.get("market_price_keys")
            if isinstance(key, list):
                key = key[0] if key else None
            if not isinstance(key, dict):
                continue
            normalized.append(
                {
                    "id": key.get("id"),
                    "fingerprint": key.get("fingerprint"),
                    "popularity_score": key.get("popularity_score"),
                    "inventory_count": key.get("inventory_count"),
                    "last_seen_at": key.get("last_seen_at"),
                    "market_country": key.get("market_country"),
                    "currency": key.get("currency"),
                    "marketplace": row.get("marketplace"),
                    "stale_after": row.get("stale_after"),
                    "current_market_price": row.get("current_market_price"),
                    "recommended_price": row.get("recommended_price"),
                    "last_updated_at": row.get("last_updated_at"),
                }
            )
        return normalized

    def list_cache_refresh_candidates(
        self,
        *,
        limit: int,
        min_popularity_score: int = 0,
        min_inventory_count: int = 0,
    ) -> list[dict[str, Any]]:
        rows = self._table_get(
            "market_price_cache",
            params={
                "select": (
                    "price_key_id,stale_after,current_market_price,recommended_price,last_updated_at,marketplace,"
                    "market_price_keys!inner(id,fingerprint,market_country,currency,popularity_score,inventory_count,last_seen_at)"
                ),
                "market_price_keys.popularity_score": f"gte.{max(0, min_popularity_score)}",
                "market_price_keys.inventory_count": f"gte.{max(0, min_inventory_count)}",
                "order": "last_updated_at.asc.nullsfirst,stale_after.asc.nullsfirst",
                "limit": max(1, min(limit, 1000)),
            },
        )
        normalized: list[dict[str, Any]] = []
        for row in rows:
            key = row.get("market_price_keys")
            if isinstance(key, list):
                key = key[0] if key else None
            if not isinstance(key, dict):
                continue
            normalized.append(
                {
                    "id": key.get("id"),
                    "fingerprint": key.get("fingerprint"),
                    "popularity_score": key.get("popularity_score"),
                    "inventory_count": key.get("inventory_count"),
                    "last_seen_at": key.get("last_seen_at"),
                    "market_country": key.get("market_country"),
                    "currency": key.get("currency"),
                    "marketplace": row.get("marketplace"),
                    "stale_after": row.get("stale_after"),
                    "current_market_price": row.get("current_market_price"),
                    "recommended_price": row.get("recommended_price"),
                    "last_updated_at": row.get("last_updated_at"),
                }
            )
        return normalized

    def get_active_jobs_for_keys(self, *, price_key_ids: list[str]) -> dict[str, dict[str, Any]]:
        clean_ids = [
            value.strip()
            for value in price_key_ids
            if str(value).strip() and UUID_PATTERN.fullmatch(value.strip()) is not None
        ]
        if not clean_ids:
            return {}
        in_filter = "(" + ",".join(clean_ids) + ")"
        rows = self._table_get(
            "market_price_refresh_jobs",
            params={
                "select": "id,price_key_id,status,priority,requested_at,reason",
                "status": "in.(queued,running)",
                "price_key_id": f"in.{in_filter}",
                "order": "requested_at.asc",
                "limit": min(len(clean_ids), 1000),
            },
        )
        active: dict[str, dict[str, Any]] = {}
        for row in rows:
            key_id = str(row.get("price_key_id", "")).strip()
            if key_id and key_id not in active:
                active[key_id] = row
        return active

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
