from __future__ import annotations

from typing import Any

import requests

from .retry import RetryableError, retry_call


class SupabaseImageStorageClient:
    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        bucket_name: str,
        timeout_seconds: int = 30,
    ) -> None:
        self.supabase_url = supabase_url.rstrip("/")
        self.bucket_name = bucket_name
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
            }
        )

    def object_exists(self, object_path: str) -> bool:
        clean_path = object_path.lstrip("/")
        response = self.session.head(
            f"{self.supabase_url}/storage/v1/object/{self.bucket_name}/{clean_path}",
            timeout=self.timeout_seconds,
        )
        if response.status_code in {404, 400}:
            return False
        if response.status_code >= 400:
            response.raise_for_status()
        return True

    def upload_if_absent(
        self,
        object_path: str,
        data: bytes,
        *,
        content_type: str,
        cache_control: str,
        max_retries: int,
        retry_base_seconds: float,
        dry_run: bool,
    ) -> str:
        clean_path = object_path.lstrip("/")
        if self.object_exists(clean_path):
            return "exists"
        if dry_run:
            return "dry_run"
        def _upload() -> str:
            response = self.session.post(
                f"{self.supabase_url}/storage/v1/object/{self.bucket_name}/{clean_path}",
                data=data,
                headers={
                    "Content-Type": content_type,
                    "x-upsert": "false",
                    "Cache-Control": cache_control,
                },
                timeout=self.timeout_seconds,
            )
            if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
                raise RetryableError(f"retryable upload status {response.status_code}")
            if response.status_code == 409:
                return "exists"
            response.raise_for_status()
            return "uploaded"

        return retry_call(
            _upload,
            max_retries=max_retries,
            base_seconds=retry_base_seconds,
            retryable=lambda exc: isinstance(exc, (RetryableError, requests.Timeout, requests.ConnectionError)),
        )

    def verify_public_readable(self, object_path: str) -> bool:
        clean_path = object_path.lstrip("/")
        response = self.session.get(
            f"{self.supabase_url}/storage/v1/object/public/{self.bucket_name}/{clean_path}",
            timeout=self.timeout_seconds,
        )
        return response.status_code == 200 and bool(response.content)
