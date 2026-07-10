from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from .models import CardImageIdentity, ProcessedCardImages


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class SupabaseImageRecordClient:
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

    def get_record(self, canonical_base_id: str) -> dict[str, Any] | None:
        response = self.session.get(
            f"{self.supabase_url}/rest/v1/pokemon_card_image_records",
            params={
                "canonical_base_id": f"eq.{canonical_base_id}",
                "select": "*",
                "limit": 1,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            return None
        return rows[0]

    def upsert_record(self, payload: dict[str, Any], *, dry_run: bool) -> None:
        if dry_run:
            return
        response = self.session.post(
            f"{self.supabase_url}/rest/v1/pokemon_card_image_records",
            params={"on_conflict": "canonical_base_id"},
            json=payload,
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

    def build_record_payload(
        self,
        identity: CardImageIdentity,
        *,
        status: str,
        processed: ProcessedCardImages | None = None,
        thumb_path: str | None = None,
        display_path: str | None = None,
        failure_reason: str | None = None,
        retry_count: int = 0,
        cache_control: str,
        existing: dict[str, Any] | None = None,
        verified: bool = False,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        payload: dict[str, Any] = {
            "canonical_base_id": identity.canonical_base_id,
            "game": identity.game,
            "language": identity.language,
            "set_id": identity.set_id,
            "set_code": identity.set_code,
            "collector_number": identity.collector_number,
            "printed_card_number": identity.printed_card_number,
            "local_card_number": identity.local_card_number,
            "set_total": identity.set_total,
            "printed_total": identity.printed_total,
            "provider_set_id": identity.provider_set_id,
            "status": status,
            "failure_reason": failure_reason,
            "retry_count": retry_count,
            "cache_control": cache_control,
            "last_attempt_at": now,
        }
        if processed is not None:
            payload.update(
                {
                    "primary_provider": processed.primary_provider,
                    "fallback_provider": processed.fallback_provider,
                    "source_image_url": processed.source_image_url,
                    "source_image_url_display": processed.source_image_url_display,
                    "provider_card_id": processed.provider_card_id,
                    "provider_image_set_id": processed.provider_image_set_id,
                    "content_hash_sha256": processed.content_hash_sha256,
                    "thumb_storage_path": thumb_path,
                    "display_storage_path": display_path,
                    "thumb_width": processed.thumb.width,
                    "thumb_height": processed.thumb.height,
                    "display_width": processed.display.width if processed.display else None,
                    "display_height": processed.display.height if processed.display else None,
                    "thumb_bytes": len(processed.thumb.data),
                    "display_bytes": len(processed.display.data) if processed.display else None,
                    "processed_at": now,
                }
            )
        if verified:
            payload["verified_at"] = now
            payload["status"] = "verified"
        if existing and existing.get("content_hash_sha256") == payload.get("content_hash_sha256") and status == "completed":
            payload["status"] = existing.get("status", status)
        return payload
