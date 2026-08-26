from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

import requests

from .models import MarketPriceKey, MarketPriceRefreshJob

UUID_PATTERN = re.compile(r"^[0-9a-fA-F-]{1,64}$")
MAX_ERROR_BODY_CHARS = 4000


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone().isoformat().replace("+00:00", "Z")


def _response_body_for_error(response: requests.Response) -> str:
    try:
        text = response.text
    except Exception:
        return "<unavailable>"
    if len(text) > MAX_ERROR_BODY_CHARS:
        return text[:MAX_ERROR_BODY_CHARS] + "...<truncated>"
    return text


def _payload_keys(payload: dict[str, Any]) -> list[str]:
    return sorted(str(key) for key in payload.keys())


class SupabaseRpcError(requests.HTTPError):
    def __init__(self, *, rpc_name: str, response: requests.Response, payload: dict[str, Any]) -> None:
        self.rpc_name = rpc_name
        self.status_code = response.status_code
        self.response_body = _response_body_for_error(response)
        self.payload_keys = _payload_keys(payload)
        message = (
            f"Supabase RPC '{rpc_name}' failed with status_code={self.status_code}; "
            f"response_body={self.response_body!r}; payload_keys={self.payload_keys}"
        )
        super().__init__(message, response=response)


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
        if response.status_code >= 400:
            raise SupabaseRpcError(rpc_name=name, response=response, payload=payload)
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

    def _table_patch(self, table: str, payload: dict[str, Any], *, params: dict[str, Any]) -> list[dict[str, Any]]:
        response = self.session.patch(
            f"{self.supabase_url}/rest/v1/{table}",
            params=params,
            json=payload,
            headers={"Prefer": "return=representation"},
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
        canonical_name_en: str | None = None,
        original_name_ja: str | None = None,
        aliases: list[str] | None = None,
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
                "p_canonical_name_en": canonical_name_en,
                "p_original_name_ja": original_name_ja,
                "p_aliases": aliases or [],
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

    def request_market_price_refresh(
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
        reason: str = "live_ebay_write_smoke",
        force_refresh: bool = False,
        canonical_name_en: str | None = None,
        original_name_ja: str | None = None,
        aliases: list[str] | None = None,
    ) -> dict[str, Any]:
        result = self._rpc(
            "request_market_price_refresh",
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
                "p_reason": reason,
                "p_force_refresh": force_refresh,
                "p_canonical_name_en": canonical_name_en,
                "p_original_name_ja": original_name_ja,
                "p_aliases": aliases or [],
            },
        )
        if not isinstance(result, dict):
            raise ValueError("request_market_price_refresh returned unexpected payload shape")
        return result

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
        """Return price keys that have no market_price_cache row.

        PostgREST anti-join must filter the embed itself (`market_price_cache=is.null`).
        Filtering `market_price_cache.price_key_id=is.null` incorrectly returns keys that
        already have cache rows (empty embed because no row has a null PK/FK).
        """
        rows = self._table_get(
            "market_price_keys",
            params={
                "select": (
                    "id,fingerprint,market_country,currency,popularity_score,inventory_count,last_seen_at,"
                    "market_price_cache!left(price_key_id,marketplace)"
                ),
                "market_price_cache": "is.null",
                "popularity_score": f"gte.{max(0, min_popularity_score)}",
                "inventory_count": f"gte.{max(0, min_inventory_count)}",
                "order": "last_seen_at.desc.nullslast,updated_at.desc",
                "limit": max(1, min(limit * 2, 1000)),
            },
        )
        missing: list[dict[str, Any]] = []
        for row in rows:
            embed = row.get("market_price_cache")
            if embed in (None, [], {}):
                missing.append(row)
            if len(missing) >= max(1, min(limit, 1000)):
                break
        return missing

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
        due_before_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return cache rows that need refresh: missing price, failed, or overdue."""
        now_iso = due_before_iso or _iso_or_none(datetime.now(timezone.utc))
        fetch_limit = max(1, min(limit, 1000))
        select = (
            "price_key_id,stale_after,next_refresh_due_at,current_market_price,recommended_price,"
            "last_updated_at,marketplace,refresh_status,last_error_message,provider,display_price_source,"
            "verification_required,verification_reason,reference_price,reference_provider,"
            "market_price_keys!inner(id,fingerprint,market_country,currency,popularity_score,inventory_count,last_seen_at)"
        )
        base_params = {
            "select": select,
            "market_price_keys.popularity_score": f"gte.{max(0, min_popularity_score)}",
            "market_price_keys.inventory_count": f"gte.{max(0, min_inventory_count)}",
            "limit": fetch_limit,
        }
        queries = [
            {
                **base_params,
                "current_market_price": "is.null",
                "or": f"(next_refresh_due_at.is.null,next_refresh_due_at.lte.{now_iso})",
                "order": "last_updated_at.asc.nullsfirst",
            },
            {
                **base_params,
                "refresh_status": "eq.failed",
                "or": f"(next_refresh_due_at.is.null,next_refresh_due_at.lte.{now_iso})",
                "order": "last_updated_at.asc.nullsfirst",
            },
            {
                **base_params,
                "next_refresh_due_at": f"lte.{now_iso}",
                "order": "next_refresh_due_at.asc.nullsfirst",
            },
            {
                **base_params,
                "next_refresh_due_at": "is.null",
                "stale_after": f"lte.{now_iso}",
                "order": "stale_after.asc.nullsfirst",
            },
            {
                **base_params,
                "next_refresh_due_at": "is.null",
                "stale_after": "is.null",
                "order": "last_updated_at.asc.nullsfirst",
            },
        ]
        by_id: dict[str, dict[str, Any]] = {}
        for params in queries:
            for row in self._table_get("market_price_cache", params=params):
                key = row.get("market_price_keys")
                if isinstance(key, list):
                    key = key[0] if key else None
                if not isinstance(key, dict):
                    continue
                key_id = str(key.get("id") or "").strip()
                if not key_id or key_id in by_id:
                    continue
                by_id[key_id] = {
                    "id": key.get("id"),
                    "fingerprint": key.get("fingerprint"),
                    "popularity_score": key.get("popularity_score"),
                    "inventory_count": key.get("inventory_count"),
                    "last_seen_at": key.get("last_seen_at"),
                    "market_country": key.get("market_country"),
                    "currency": key.get("currency"),
                    "marketplace": row.get("marketplace"),
                    "stale_after": row.get("stale_after"),
                    "next_refresh_due_at": row.get("next_refresh_due_at"),
                    "current_market_price": row.get("current_market_price"),
                    "recommended_price": row.get("recommended_price"),
                    "last_updated_at": row.get("last_updated_at"),
                    "refresh_status": row.get("refresh_status"),
                    "last_error_message": row.get("last_error_message"),
                    "provider": row.get("provider"),
                    "display_price_source": row.get("display_price_source"),
                    "verification_required": row.get("verification_required"),
                    "verification_reason": row.get("verification_reason"),
                    "reference_price": row.get("reference_price"),
                    "reference_provider": row.get("reference_provider"),
                }
                if len(by_id) >= fetch_limit:
                    break
            if len(by_id) >= fetch_limit:
                break

        def _sort_key(item: dict[str, Any]) -> tuple[int, float]:
            has_price = item.get("current_market_price") is not None
            failed = str(item.get("refresh_status") or "").lower() == "failed"
            due = item.get("next_refresh_due_at") or item.get("stale_after") or ""
            rank = 0 if not has_price else 1 if failed else 2
            try:
                due_ts = datetime.fromisoformat(str(due).replace("Z", "+00:00")).timestamp()
            except Exception:
                due_ts = float("inf")
            return (rank, due_ts)

        return sorted(by_id.values(), key=_sort_key)[:fetch_limit]

    def count_refresh_queue_depth(self) -> int:
        """Count queued+running refresh jobs for scheduler watermarks."""
        rows = self._table_get(
            "market_price_refresh_jobs",
            params={
                "select": "id",
                "status": "in.(queued,running)",
                "limit": "1000",
            },
        )
        return len(rows)

    def get_cache_row(self, *, price_key_id: str) -> dict[str, Any] | None:
        rows = self._table_get(
            "market_price_cache",
            params={
                "select": (
                    "price_key_id,current_market_price,recommended_price,next_refresh_due_at,"
                    "stale_after,latest_snapshot_id,confidence,sample_size,refresh_status"
                ),
                "price_key_id": f"eq.{price_key_id}",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

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

    def claim_specific_refresh_job(self, *, job_id: str, worker_id: str) -> MarketPriceRefreshJob | None:
        if UUID_PATTERN.fullmatch(str(job_id).strip()) is None:
            raise ValueError("job_id must be a UUID")
        rows = self._table_patch(
            "market_price_refresh_jobs",
            {
                "status": "running",
                "worker_id": worker_id,
                "locked_at": _iso_or_none(datetime.now().astimezone()),
                "started_at": _iso_or_none(datetime.now().astimezone()),
                "error_message": None,
            },
            params={"id": f"eq.{job_id}", "status": "eq.queued", "select": "*"},
        )
        if not rows:
            return None
        return MarketPriceRefreshJob.from_row(rows[0])

    def get_refresh_job(self, *, job_id: str) -> MarketPriceRefreshJob | None:
        rows = self._table_get(
            "market_price_refresh_jobs",
            params={"id": f"eq.{job_id}", "select": "*", "limit": 1},
        )
        return MarketPriceRefreshJob.from_row(rows[0]) if rows else None

    def count_live_scheduler_jobs_today(self) -> int:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self._table_get(
            "market_price_refresh_jobs",
            params={
                "select": "id",
                "reason": "eq.live_ebay_scheduler",
                "requested_at": f"gte.{_iso_or_none(today)}",
                "limit": 1000,
            },
        )
        return len(rows)

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

    def fail_job(
        self,
        *,
        job_id: str,
        error_message: str,
        retryable: bool = True,
        retry_delay_minutes: int = 15,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        return self._rpc(
            "fail_market_price_refresh_job",
            {
                "p_job_id": job_id,
                "p_error_message": error_message[:1000],
                "p_retryable": bool(retryable),
                "p_retry_delay_minutes": max(1, int(retry_delay_minutes)),
                "p_max_attempts": max(1, int(max_attempts)),
            },
        )

    def mark_cache_failure(
        self,
        *,
        price_key_id: str,
        error_message: str,
        next_refresh_due_at: datetime,
        market_country: str | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        """Record a failed attempt and push next eligibility into the future."""
        payload = {
            "price_key_id": price_key_id,
            "refresh_status": "failed",
            "last_error_message": (error_message or "")[:1000] or None,
            "next_refresh_due_at": _iso_or_none(next_refresh_due_at),
            "updated_at": _iso_or_none(datetime.now(timezone.utc)),
        }
        if market_country:
            payload["market_country"] = str(market_country).upper()
        if currency:
            payload["currency"] = str(currency).upper()
        return self.upsert_cache(payload)

    def record_provider_sync_run(
        self,
        *,
        provider: str,
        status: str,
        counters: dict[str, Any],
        duration_ms: int,
    ) -> dict[str, Any]:
        payload = {
            "provider": provider,
            "finished_at": _iso_or_none(datetime.now(timezone.utc)),
            "status": status,
            "keys_scanned": int(counters.get("keysScanned") or 0),
            "keys_matched": int(counters.get("keysMatched") or 0),
            "keys_updated": int(counters.get("keysUpdated") or 0),
            "keys_unchanged": int(counters.get("keysUnchanged") or 0),
            "keys_quarantined": int(counters.get("keysQuarantined") or 0),
            "keys_unresolved": int(counters.get("keysUnresolved") or 0),
            "keys_ambiguous": int(counters.get("keysAmbiguous") or 0),
            "verification_enqueued": int(counters.get("verificationEnqueued") or 0),
            "errors": int(counters.get("errors") or 0),
            "duration_ms": int(duration_ms),
            "bulk_keys_per_hour": counters.get("bulkKeysPerHour"),
            "diagnostics_json": counters,
        }
        rows = self._table_post("market_price_provider_sync_runs", payload)
        return rows[0] if rows else payload

    def count_recent_same_failures(
        self,
        *,
        price_key_id: str,
        error_message: str,
        lookback_hours: int = 168,
    ) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
        rows = self._table_get(
            "market_price_refresh_jobs",
            params={
                "select": "id,error_message,status,completed_at",
                "price_key_id": f"eq.{price_key_id}",
                "status": "eq.failed",
                "completed_at": f"gte.{_iso_or_none(cutoff)}",
                "order": "completed_at.desc",
                "limit": "50",
            },
        )
        needle = (error_message or "").strip().lower()
        count = 0
        for row in rows:
            message = str(row.get("error_message") or "").strip().lower()
            if needle and needle in message:
                count += 1
            elif not needle and message:
                count += 1
        return count

    def recover_abandoned_refresh_jobs(
        self,
        *,
        stale_after_minutes: int = 90,
        max_jobs: int = 25,
    ) -> list[dict[str, Any]]:
        """Fail running jobs whose lock is older than the stale threshold."""
        try:
            rows = self._rpc(
                "recover_abandoned_market_price_refresh_jobs",
                {
                    "p_stale_after_minutes": max(15, int(stale_after_minutes)),
                    "p_max_jobs": max(1, min(int(max_jobs), 100)),
                },
            )
        except SupabaseRpcError as exc:
            # Fallback for environments that have not applied the recovery RPC yet.
            if exc.status_code != 404:
                raise
            cutoff = datetime.now(timezone.utc).timestamp() - (max(15, int(stale_after_minutes)) * 60)
            cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            stuck = self._table_get(
                "market_price_refresh_jobs",
                params={
                    "select": "id,price_key_id,status,locked_at,started_at,worker_id,reason",
                    "status": "eq.running",
                    "or": f"(locked_at.lt.{cutoff_iso},and(locked_at.is.null,started_at.lt.{cutoff_iso}))",
                    "order": "locked_at.asc.nullsfirst",
                    "limit": max(1, min(int(max_jobs), 100)),
                },
            )
            recovered: list[dict[str, Any]] = []
            for row in stuck:
                job_id = str(row.get("id") or "").strip()
                if not job_id:
                    continue
                recovered.append(
                    self.fail_job(
                        job_id=job_id,
                        error_message="abandoned_stale_lock:worker_lock_exceeded_threshold",
                    )
                )
            return recovered
        if rows is None:
            return []
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        if isinstance(rows, dict):
            return [rows]
        return []

    def upsert_pipeline_heartbeat(
        self,
        *,
        component: str,
        worker_id: str,
        state: str,
        meta: dict[str, Any] | None = None,
        version: str | None = None,
    ) -> dict[str, Any] | None:
        payload = {
            "component": str(component).strip()[:64],
            "worker_id": str(worker_id or "unknown").strip()[:128],
            "state": str(state).strip()[:64],
            "version": (str(version).strip()[:128] if version else None),
            "last_heartbeat_at": _iso_or_none(datetime.now(timezone.utc)),
            "meta": meta or {},
            "updated_at": _iso_or_none(datetime.now(timezone.utc)),
        }
        try:
            rows = self._table_post(
                "market_price_pipeline_heartbeats",
                payload,
                prefer="resolution=merge-duplicates,return=representation",
                on_conflict="component",
            )
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {404, 42}:
                return None
            # Table may not exist yet before migration; do not break scheduling.
            if status == 400:
                body = ""
                try:
                    body = (exc.response.text or "").lower()  # type: ignore[union-attr]
                except Exception:
                    body = ""
                if "market_price_pipeline_heartbeats" in body or "could not find" in body:
                    return None
            raise
        return rows[0] if rows else None
