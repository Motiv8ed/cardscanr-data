"""Private, metadata-only compatibility contract for legacy web JSON shards.

This is NOT a publication adapter. It only validates a bounded in-memory
sample from SHA-verified R2 SQLite language packs. No image URLs, file writes,
production grants, public selectors or customer data are emitted.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from cardscanr_search_index.global_builder import _normalize

WEB_FIELDS = frozenset({
    "id", "lang", "set", "num", "local", "numNorm", "setTotal",
    "printedTotal", "name", "setName", "rarity", "thumb", "n",
})
SAMPLE_LIMIT = 3
SUPPORTED_PRIVATE_PACKS = frozenset({"en", "ja", "ko", "zh-tw", "th", "id", "zh-cn"})


def preview_metadata_row(row: Mapping[str, Any], language: str) -> dict[str, Any]:
    """Adapt a pack row to the *shape* of web search data, not to a public URL."""
    if language not in SUPPORTED_PRIVATE_PACKS:
        raise ValueError("unapproved_private_language")
    actual_lang = str(row.get("language") or "")
    if actual_lang != language:
        raise ValueError("pack_row_language_mismatch")
    base = str(row.get("canonical_base_id") or "").strip()
    printing = str(row.get("canonical_printing_id") or "").strip()
    set_id = str(row.get("set_id") or "").strip()
    number = str(row.get("printed_collector_number") or "").strip()
    native = str(row.get("native_card_name") or "").strip()
    if not all((base, printing, set_id, number, native)):
        raise ValueError("missing_canonical_metadata")
    if len(base) > 300 or len(number) > 100 or len(native) > 500:
        raise ValueError("unbounded_metadata_field")
    normalized = _normalize(native)
    if not normalized:
        raise ValueError("empty_normalized_name")
    local_number = number.split("/", 1)[0].strip()
    # Existing website shard folder/API code is jp, whereas R2 pack is ja.
    web_language = "jp" if language == "ja" else language
    output = {
        "id": base,
        "lang": web_language,
        "set": set_id,
        "num": number,
        "local": local_number,
        "numNorm": str(row.get("normalized_collector_number") or _normalize(number)),
        "setTotal": None,
        "printedTotal": None,
        "name": native,
        "setName": str(row.get("native_set_name") or set_id),
        "rarity": row.get("rarity"),
        "thumb": None,  # Never copy source images into preview shards.
        "n": normalized,
    }
    if set(output) != WEB_FIELDS or output["thumb"] is not None:
        raise AssertionError("private_preview_output_contract_broken")
    # Explicitly test the source has a distinct printing ID without replacing
    # the existing portal's base ID with it.
    return output


def validate_bounded_preview(rows: list[Mapping[str, Any]], language: str) -> dict[str, Any]:
    if not (1 <= len(rows) <= SAMPLE_LIMIT):
        raise ValueError("sample_not_bounded")
    rendered = [preview_metadata_row(row, language) for row in rows]
    ids = [item["id"] for item in rendered]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_base_id")
    if any(item["thumb"] is not None for item in rendered):
        raise ValueError("image_field_present")
    return {
        "language": language,
        "metadata_only": True,
        "sample_count": len(rendered),
        "no_images": True,
        "web_field_contract": "legacy-json-1.1.0",
        "production_enabled": False,
    }
