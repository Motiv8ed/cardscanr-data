from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CardImageIdentity:
    canonical_base_id: str
    game: str
    language: str
    set_id: str
    set_code: str | None
    collector_number: str
    printed_card_number: str
    local_card_number: str
    set_total: int | None
    printed_total: int | None
    provider_set_id: str | None
    provider_ids: dict[str, Any]
    image_source: str | None
    catalogue_image_small: str | None
    catalogue_image_large: str | None
    serie_id: str | None = None
    source_card: dict[str, Any] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class ProviderImageCandidate:
    provider: str
    source_url_thumb: str
    source_url_display: str
    provider_card_id: str | None
    provider_set_id: str | None
    match_basis: str


@dataclass(frozen=True)
class ProcessedImageVariant:
    label: str
    data: bytes
    width: int
    height: int
    content_type: str = "image/webp"


@dataclass(frozen=True)
class ProcessedCardImages:
    thumb: ProcessedImageVariant
    display: ProcessedImageVariant | None
    content_hash_sha256: str
    primary_provider: str
    fallback_provider: str | None
    source_image_url: str
    source_image_url_display: str
    provider_card_id: str | None
    provider_image_set_id: str | None
    import_display: bool = False


@dataclass(frozen=True)
class PipelineCardResult:
    identity: CardImageIdentity
    status: str
    failure_reason: str | None = None
    processed: ProcessedCardImages | None = None
    retry_count: int = 0
    dry_run: bool = False
