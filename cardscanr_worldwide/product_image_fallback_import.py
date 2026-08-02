"""Register exact archived fallbacks for removed official product images."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json, digest, stable_id


def import_fallbacks(database: Path, evidence_path: Path) -> dict[str, int]:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence.get("schema_version") != 1:
        raise ValueError("Unsupported product-image fallback evidence")
    provider_id = evidence["provider_id"]
    counters: Counter[str] = Counter()
    connection = connect(str(database))
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            for url in evidence.get("excluded_non_product_urls") or []:
                rows = connection.execute(
                    "select id,attributes_json from product_image_candidate where provider_id=? and source_url=?",
                    (provider_id, url),
                ).fetchall()
                for row in rows:
                    attributes = json.loads(row["attributes_json"] or "{}")
                    attributes["classification"] = "excluded_non_product_icon"
                    connection.execute(
                        "update product_image_candidate set validation_status='invalid',attributes_json=? where id=?",
                        (canonical_json(attributes), row["id"]),
                    )
                    counters["excluded_non_product_icons"] += 1
            for fallback in evidence.get("fallbacks") or []:
                originals = connection.execute(
                    """select * from product_image_candidate where provider_id=? and source_url=?
                       order by id""", (provider_id, fallback["original_url"]),
                ).fetchall()
                if len(originals) != 1:
                    raise ValueError(
                        f"Expected one original candidate for {fallback['original_url']}, found {len(originals)}"
                    )
                original = originals[0]
                candidate_id = stable_id(
                    original["sealed_product_variant_id"], provider_id, "archive-display",
                    digest(fallback["archive_url"])[:16],
                )
                connection.execute(
                    """insert into product_image_candidate values (?, ?, ?, ?, 'display', ?, 'link_only', 'candidate', ?)
                       on conflict(id) do update set source_url=excluded.source_url,attributes_json=excluded.attributes_json""",
                    (candidate_id, original["sealed_product_variant_id"], original["source_record_id"], provider_id,
                     fallback["archive_url"], canonical_json({
                         "fallback_for_candidate_id": original["id"],
                         "original_url": fallback["original_url"],
                         "archive_timestamp": fallback["archive_timestamp"],
                         "archive_digest": fallback["archive_digest"],
                         "match_method": "exact_original_asset_url_cdx_capture",
                     })),
                )
                counters["archive_fallback_candidates"] += 1
            for additional in evidence.get("additional_candidates") or []:
                product = connection.execute(
                    """select spv.id variant_id,sp.source_record_id from sealed_product sp
                         join sealed_product_variant spv on spv.sealed_product_id=sp.id
                        where sp.provider_id=? and sp.provider_record_id=?""",
                    (provider_id, additional["product_provider_record_id"]),
                ).fetchall()
                if len(product) != 1:
                    raise ValueError(
                        f"Expected one product variant for {additional['product_provider_record_id']}, found {len(product)}"
                    )
                candidate_id = stable_id(
                    product[0]["variant_id"], provider_id, "recovered-display",
                    digest(additional["source_url"])[:16],
                )
                connection.execute(
                    """insert into product_image_candidate values (?, ?, ?, ?, 'display', ?, 'link_only', 'candidate', ?)
                       on conflict(id) do update set source_url=excluded.source_url,attributes_json=excluded.attributes_json""",
                    (candidate_id, product[0]["variant_id"], product[0]["source_record_id"], provider_id,
                     additional["source_url"], canonical_json({
                         "product_provider_record_id": additional["product_provider_record_id"],
                         "archive_timestamp": additional.get("archive_timestamp"),
                         "archive_digest": additional.get("archive_digest"),
                         "match_method": additional["match_method"],
                     })),
                )
                counters["additional_exact_candidates"] += 1
        return dict(counters)
    finally:
        connection.close()
