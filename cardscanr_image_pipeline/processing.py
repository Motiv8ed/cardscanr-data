from __future__ import annotations

import os
from io import BytesIO
from typing import Any

import requests
from PIL import Image

from .config import ALLOWED_IMAGE_CONTENT_TYPES
from .identity import sha256_hex
from .models import ProcessedCardImages, ProcessedImageVariant, ProviderImageCandidate
from .retry import RetryableError, retry_call


def pokewallet_request_headers(url: str) -> dict[str, str]:
    if "api.pokewallet.io" not in url:
        return {}
    api_key = (os.getenv("POKEWALLET_API_KEY") or os.getenv("CARDSCANR_POKEWALLET_API_KEY") or "").strip()
    if not api_key:
        return {}
    return {"X-API-Key": api_key}


class ImageValidationError(ValueError):
    pass


def download_image_bytes(
    session: requests.Session,
    url: str,
    *,
    timeout_seconds: int,
    max_retries: int,
    retry_base_seconds: float,
) -> tuple[bytes, str]:
    def _fetch() -> tuple[bytes, str]:
        response = session.get(
            url,
            timeout=timeout_seconds,
            headers=pokewallet_request_headers(url),
        )
        if response.status_code in {408, 425, 429, 500, 502, 503, 504}:
            raise RetryableError(f"retryable HTTP {response.status_code} for {url}")
        response.raise_for_status()
        content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type and content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
            raise ImageValidationError(f"unsupported content type {content_type!r}")
        data = response.content
        if not data:
            raise ImageValidationError("empty response body")
        return data, content_type or "application/octet-stream"

    return retry_call(
        _fetch,
        max_retries=max_retries,
        base_seconds=retry_base_seconds,
        retryable=lambda exc: isinstance(exc, (RetryableError, requests.Timeout, requests.ConnectionError)),
    )


def validate_card_image_geometry(image: Image.Image) -> None:
    width, height = image.size
    if height <= width:
        raise ImageValidationError(f"expected portrait orientation, got {width}x{height}")
    aspect = height / width
    if aspect < 1.15 or aspect > 1.85:
        raise ImageValidationError(f"unexpected card aspect ratio {aspect:.3f}")


def decode_image(data: bytes) -> Image.Image:
    try:
        image = Image.open(BytesIO(data))
        image.load()
    except Exception as exc:
        raise ImageValidationError(f"unable to decode image: {exc}") from exc
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ImageValidationError("decoded image has non-positive dimensions")
    return image


def decode_and_validate_card_image(data: bytes) -> Image.Image:
    image = decode_image(data)
    validate_card_image_geometry(image)
    return image


def resize_to_webp(image: Image.Image, *, max_px: int, label: str) -> ProcessedImageVariant:
    working = image.convert("RGBA") if image.mode not in {"RGB", "RGBA"} else image.copy()
    working.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    width, height = working.size
    if width <= 0 or height <= 0:
        raise ImageValidationError(f"{label} variant has non-positive dimensions")
    buffer = BytesIO()
    working.save(buffer, format="WEBP", quality=85, method=6)
    data = buffer.getvalue()
    if not data:
        raise ImageValidationError(f"{label} variant encoded to empty webp")
    return ProcessedImageVariant(label=label, data=data, width=width, height=height)


def process_downloaded_image(
    source_bytes: bytes,
    candidate: ProviderImageCandidate,
    *,
    fallback_provider: str | None,
    thumb_max_px: int,
    display_max_px: int,
) -> ProcessedCardImages:
    display_image = decode_and_validate_card_image(source_bytes)
    display_variant = resize_to_webp(display_image, max_px=display_max_px, label="display")
    thumb_variant = resize_to_webp(display_image, max_px=thumb_max_px, label="thumb")
    content_hash = sha256_hex(display_variant.data)
    return ProcessedCardImages(
        thumb=thumb_variant,
        display=display_variant,
        content_hash_sha256=content_hash,
        primary_provider=candidate.provider,
        fallback_provider=fallback_provider,
        source_image_url=candidate.source_url_thumb,
        source_image_url_display=candidate.source_url_display,
        provider_card_id=candidate.provider_card_id,
        provider_image_set_id=candidate.provider_set_id,
    )


def process_provider_candidate(
    session: requests.Session,
    candidate: ProviderImageCandidate,
    *,
    fallback_provider: str | None,
    thumb_max_px: int,
    display_max_px: int,
    timeout_seconds: int,
    max_retries: int,
    retry_base_seconds: float,
) -> ProcessedCardImages:
    display_raw, _ = download_image_bytes(
        session,
        candidate.source_url_display,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
    )
    return process_downloaded_image(
        display_raw,
        candidate,
        fallback_provider=fallback_provider,
        thumb_max_px=thumb_max_px,
        display_max_px=display_max_px,
    )


def validate_existing_record_dimensions(record: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for prefix in ("thumb", "display"):
        width = record.get(f"{prefix}_width")
        height = record.get(f"{prefix}_height")
        size = record.get(f"{prefix}_bytes")
        if width is not None and int(width) <= 0:
            issues.append(f"{prefix}_width_non_positive")
        if height is not None and int(height) <= 0:
            issues.append(f"{prefix}_height_non_positive")
        if size is not None and int(size) <= 0:
            issues.append(f"{prefix}_bytes_zero")
    return issues
