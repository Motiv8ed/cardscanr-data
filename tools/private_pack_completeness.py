"""Read-only coverage inventory for each SHA-verified worldwide language pack.

Counts missing card/set metadata and image-reference *availability*, not rights.
No image binaries, card rows, customer data or public output are written.
"""
from __future__ import annotations

import sqlite3
from typing import Any

_REQUIRED = (
    "canonical_base_id", "set_id", "native_card_name",
    "native_set_name", "printed_collector_number",
)
_OPTIONAL = (
    "english_card_name", "rarity", "regulation_mark", "release_date",
    "image_thumbnail_url", "image_display_url", "mirror_permission_status",
    "provider_card_id", "provider_set_id", "native_name_status",
)


def inventory_pack(db: sqlite3.Connection) -> dict[str, Any]:
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(cards)")}
    absent = [key for key in _REQUIRED if key not in columns]
    if absent:
        raise ValueError("missing_required_pack_columns:" + ",".join(absent))
    def exists(name: str) -> str:
        return name if name in columns else "NULL"

    def empty(name: str) -> str:
        return "COALESCE(TRIM(CAST(" + exists(name) + " AS TEXT)),'') = ''"

    def count_if(predicate: str) -> str:
        return "COALESCE(SUM(CASE WHEN (" + predicate + ") THEN 1 ELSE 0 END),0)"

    card_count, distinct_sets = db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT set_id) FROM cards"
    ).fetchone()
    gaps: dict[str, int] = {}
    for name in (*_REQUIRED, *_OPTIONAL):
        gaps["missing_" + name] = int(db.execute(
            "SELECT " + count_if(empty(name)) + " FROM cards"
        ).fetchone()[0])
    image_reference = "(" + " OR ".join(
        "NOT (" + empty(name) + ")"
        for name in ("image_thumbnail_url", "image_display_url")
    ) + ")"
    images_with_reference = int(db.execute(
        "SELECT " + count_if(image_reference) + " FROM cards"
    ).fetchone()[0])
    # This mirrors the source *field*, not a permission decision for CardScanR.
    provider_mirror_approved = int(db.execute(
        "SELECT " + count_if(exists("mirror_permission_status") + " = 'approved'")
        + " FROM cards"
    ).fetchone()[0])
    # Useful deterministic shortlist for source discovery. No raw card rows.
    missing_image = "NOT " + image_reference
    set_gaps = [
        {
            "set_id": str(set_id),
            "card_records": int(total),
            "no_image_reference": int(image_missing),
        }
        for set_id, total, image_missing in db.execute(
            "SELECT set_id, COUNT(*) AS total,"
            + count_if(missing_image)
            + " AS no_image_reference FROM cards GROUP BY set_id"
            + " ORDER BY no_image_reference DESC, total DESC, set_id LIMIT 12"
        )
    ]
    set_details_missing = db.execute(
        "SELECT COUNT(*) FROM (SELECT set_id FROM cards "
        "GROUP BY set_id HAVING " + count_if(empty("native_set_name")) + " = COUNT(*))"
    ).fetchone()[0]
    return {
        "card_records": int(card_count),
        "distinct_referenced_sets": int(distinct_sets),
        "sets_missing_all_native_names": int(set_details_missing),
        "cards_with_image_reference": images_with_reference,
        "cards_without_image_reference": int(card_count) - images_with_reference,
        "source_mirror_permission_approved_field_count": provider_mirror_approved,
        "missing_metadata_field_counts": gaps,
        "worst_image_reference_gap_sets": set_gaps,
        "notes": (
            "Image URL presence is not verified reachability. Source mirror "
            "approval field is not a legal or production grant. Missing fields "
            "are per-card NULL/empty source values, not assumed missing cards."
        ),
        "artwork_downloaded": False,
        "safe_to_publish": False,
    }
