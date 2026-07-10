from __future__ import annotations

import re

from .identity import normalize_local_card_number, sha256_hex
from .models import CardImageIdentity

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


def safe_path_segment(value: str) -> str:
    text = str(value or "").strip()
    text = _SAFE_SEGMENT.sub("-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    return text or "unknown"


def version_directory_name(content_hash_sha256: str) -> str:
    digest = content_hash_sha256.strip().lower()
    if len(digest) < 16:
        raise ValueError("content_hash_sha256 must be at least 16 hex characters")
    return digest[:16]


def build_storage_base(
    identity: CardImageIdentity,
    *,
    content_hash_sha256: str,
) -> str:
    version = version_directory_name(content_hash_sha256)
    collector_segment = safe_path_segment(identity.collector_number)
    return "/".join(
        [
            safe_path_segment(identity.game),
            safe_path_segment(identity.language),
            safe_path_segment(identity.set_id),
            collector_segment,
            "v",
            version,
        ]
    )


def build_thumb_storage_path(
    identity: CardImageIdentity,
    *,
    content_hash_sha256: str,
) -> str:
    return f"{build_storage_base(identity, content_hash_sha256=content_hash_sha256)}/thumb.webp"


def build_display_storage_path(
    identity: CardImageIdentity,
    *,
    content_hash_sha256: str,
) -> str:
    return f"{build_storage_base(identity, content_hash_sha256=content_hash_sha256)}/display.webp"


def build_storage_paths(
    identity: CardImageIdentity,
    *,
    content_hash_sha256: str,
    bucket_name: str = "pokemon-card-images",
    import_display: bool = True,
) -> tuple[str, str | None]:
    del bucket_name  # reserved for callers that pass bucket context
    thumb_path = build_thumb_storage_path(identity, content_hash_sha256=content_hash_sha256)
    if not import_display:
        return thumb_path, None
    return thumb_path, build_display_storage_path(identity, content_hash_sha256=content_hash_sha256)


def public_storage_url(supabase_url: str, bucket_name: str, object_path: str) -> str:
    base = supabase_url.rstrip("/")
    clean_path = object_path.lstrip("/")
    return f"{base}/storage/v1/object/public/{bucket_name}/{clean_path}"


def content_hash_from_display_bytes(display_bytes: bytes) -> str:
    if not display_bytes:
        raise ValueError("display_bytes must be non-empty")
    return sha256_hex(display_bytes)


def local_card_number_candidates(local_card_number: str) -> list[str]:
    normalized = normalize_local_card_number(local_card_number)
    if not normalized:
        return []
    candidates = [normalized]
    if normalized.isdigit():
        candidates.append(normalized.zfill(3))
        candidates.append(normalized.zfill(2))
    deduped: list[str] = []
    for item in candidates:
        if item not in deduped:
            deduped.append(item)
    return deduped
