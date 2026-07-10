from __future__ import annotations

import os
from dataclasses import dataclass


STORAGE_BUCKET = "pokemon-card-images"
DEFAULT_CACHE_CONTROL = "public, max-age=31536000, immutable"
DEFAULT_THUMB_MAX_PX = 245
DEFAULT_DISPLAY_MAX_PX = 1000
DEFAULT_NETWORK_CONCURRENCY = 4
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_BASE_SECONDS = 1.0
ALLOWED_IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/webp",
        "image/png",
        "image/jpeg",
        "image/jpg",
    }
)
IMAGE_RECORD_STATUSES = frozenset(
    {
        "pending",
        "processing",
        "completed",
        "failed",
        "skipped",
        "verified",
        "provider_image_unavailable",
    }
)


@dataclass(frozen=True)
class ImagePipelineConfig:
    supabase_url: str
    supabase_secret_key: str
    bucket_name: str = STORAGE_BUCKET
    cache_control: str = DEFAULT_CACHE_CONTROL
    thumb_max_px: int = DEFAULT_THUMB_MAX_PX
    display_max_px: int = DEFAULT_DISPLAY_MAX_PX
    network_concurrency: int = DEFAULT_NETWORK_CONCURRENCY
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS
    catalogue_root: str = "public/v1"
    dry_run: bool = True
    sample_limit: int | None = None
    languages: tuple[str, ...] = ("en", "jp")
    execute: bool = False
    timeout_seconds: int = 30
    # Thumbnail rollout: import thumb.webp only; never upload display.webp unless explicitly enabled.
    import_display: bool = False

    @classmethod
    def from_env(
        cls,
        *,
        dry_run: bool = True,
        execute: bool = False,
        sample_limit: int | None = None,
        languages: tuple[str, ...] | None = None,
        import_display: bool = False,
    ) -> ImagePipelineConfig:
        supabase_url = os.getenv("SUPABASE_URL", "").strip()
        secret = (os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        if not supabase_url or not secret:
            raise ValueError(
                "SUPABASE_URL and SUPABASE_SECRET_KEY (or SUPABASE_SERVICE_ROLE_KEY) must be set in the environment."
            )
        concurrency = int(os.getenv("IMAGE_PIPELINE_NETWORK_CONCURRENCY", str(DEFAULT_NETWORK_CONCURRENCY)))
        return cls(
            supabase_url=supabase_url,
            supabase_secret_key=secret,
            network_concurrency=max(1, concurrency),
            dry_run=dry_run and not execute,
            execute=execute,
            sample_limit=sample_limit,
            languages=languages or ("en", "jp"),
            import_display=import_display,
        )
