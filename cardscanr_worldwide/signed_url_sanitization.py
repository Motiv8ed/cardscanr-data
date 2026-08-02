"""Remove transient public signed URLs from publishable normalized records."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .schema import connect
from .tcgdex import canonical_json, digest


def _strip_signed(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_signed(item) for item in value]
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            if key == "signed_source_url":
                continue
            output[key] = _strip_signed(item)
        return output
    if isinstance(value, str):
        parsed = urlsplit(value)
        query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        if query_keys & {"auth_key", "token", "api_key", "apikey", "signature", "x-amz-signature"}:
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return value


def sanitize_signed_urls(database: Path, provider_id: str = "pokemon-cn-official") -> dict[str, int]:
    connection = connect(str(database))
    connection.row_factory = sqlite3.Row
    counters: Counter[str] = Counter()
    try:
        with connection:
            for row in connection.execute(
                "select id,raw_payload_json from source_record where provider_id=?", (provider_id,),
            ).fetchall():
                if not row["raw_payload_json"]:
                    continue
                original = json.loads(row["raw_payload_json"])
                sanitized = canonical_json(_strip_signed(original))
                if sanitized != canonical_json(original):
                    connection.execute(
                        "update source_record set raw_payload_json=?,source_sha256=? where id=?",
                        (sanitized, digest(sanitized), row["id"]),
                    )
                    counters["source_records"] += 1
            for row in connection.execute(
                "select id,raw_product_json from sealed_product where provider_id=?", (provider_id,),
            ).fetchall():
                sanitized = canonical_json(_strip_signed(json.loads(row["raw_product_json"] or "{}")))
                if sanitized != canonical_json(json.loads(row["raw_product_json"] or "{}")):
                    connection.execute("update sealed_product set raw_product_json=? where id=?", (sanitized, row["id"]))
                    counters["sealed_products"] += 1
            for row in connection.execute(
                "select id,attributes_json from product_image_candidate where provider_id=?", (provider_id,),
            ).fetchall():
                original = json.loads(row["attributes_json"] or "{}")
                sanitized = canonical_json(_strip_signed(original))
                if sanitized != canonical_json(original):
                    connection.execute("update product_image_candidate set attributes_json=? where id=?",
                                       (sanitized, row["id"]))
                    counters["image_candidates"] += 1
        return dict(counters)
    finally:
        connection.close()

