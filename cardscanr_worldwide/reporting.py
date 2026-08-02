"""Deterministic coverage and integrity reporting for staging catalogues."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql).fetchall()]


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_report(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("pragma integrity_check").fetchone()[0]
        foreign_key_failures = [list(row) for row in connection.execute("pragma foreign_key_check").fetchall()]
        counts = {}
        for table in (
            "source_provider", "import_run", "source_snapshot", "source_record", "series", "card_set",
            "set_release", "card_design", "card_printing", "card_variant", "card_localisation",
            "attack", "ability", "marketplace_mapping", "provider_entity_mapping",
            "card_image_candidate", "sealed_product", "sealed_product_variant", "product_content",
            "product_image_candidate", "accessory", "unresolved_item",
        ):
            counts[table] = connection.execute(f"select count(*) from {table}").fetchone()[0]
        language_coverage = _rows(connection, """
            select sr.language_code, sr.region_code,
                   count(distinct sr.card_set_id) sets,
                   count(distinct cp.id) printings,
                   count(distinct cv.id) variants,
                   count(distinct case when cv.recognition_status='unknown' then cv.id end) unknown_variants,
                   count(distinct case when cp.verification_status='quarantined' then cp.id end) quarantined_printings
            from set_release sr
            left join card_printing cp on cp.set_release_id=sr.id
            left join card_variant cv on cv.card_printing_id=cp.id
            group by sr.language_code, sr.region_code
            order by sr.language_code, sr.region_code
        """)
        scope_path = Path(__file__).resolve().parents[1] / "config" / "worldwide_catalogue_scope.json"
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
        totals_by_language: dict[str, dict[str, int]] = {}
        for row in language_coverage:
            totals = totals_by_language.setdefault(row["language_code"], {
                "sets": 0, "printings": 0, "variants": 0, "unknown_variants": 0, "quarantined_printings": 0,
            })
            for key in totals:
                totals[key] += int(row[key] or 0)
        expected_language_matrix = []
        for language in scope["languages"]:
            totals = totals_by_language.get(language["code"], {
                "sets": 0, "printings": 0, "variants": 0, "unknown_variants": 0, "quarantined_printings": 0,
            })
            expected_language_matrix.append({
                "language_code": language["code"],
                "officially_printed": language["officially_printed"],
                "expected_regions": language["regional_variants"],
                **totals,
                "inventory_status": "present" if totals["printings"] else "enumerated_zero_printings",
            })
        return {
            "schema_version": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "database": str(database.resolve()),
            "database_bytes": database.stat().st_size,
            "database_sha256": _file_sha256(database),
            "integrity": {
                "sqlite_integrity_check": integrity,
                "foreign_key_failure_count": len(foreign_key_failures),
                "foreign_key_failures": foreign_key_failures[:100],
            },
            "counts": counts,
            "source_records": _rows(connection, """
                select provider_id, record_type, count(*) records,
                       sum(case when error is not null then 1 else 0 end) errors
                from source_record group by provider_id, record_type order by provider_id, record_type
            """),
            "imports": _rows(connection, """
                select provider_id, status, input_sha256, started_at, completed_at, counters_json, error_summary
                from import_run order by started_at
            """),
            "language_coverage": language_coverage,
            "expected_language_matrix": expected_language_matrix,
            "provider_entity_counts": _rows(connection, """
                select cs.provider_id, count(distinct cs.id) sets,
                       count(distinct cp.id) printings, count(distinct cv.id) variants
                from card_set cs
                left join set_release sr on sr.card_set_id=cs.id
                left join card_printing cp on cp.set_release_id=sr.id
                left join card_variant cv on cv.card_printing_id=cp.id
                group by cs.provider_id order by cs.provider_id
            """),
            "mapping_counts": _rows(connection, """
                select provider_id, entity_type, match_method, mapping_status, count(*) mappings
                from provider_entity_mapping
                group by provider_id, entity_type, match_method, mapping_status
                order by provider_id, entity_type, match_method, mapping_status
            """),
            "image_candidates": _rows(connection, """
                select provider_id, rights_status, validation_status, image_role, count(*) images
                from card_image_candidate
                group by provider_id, rights_status, validation_status, image_role
                order by provider_id, rights_status, validation_status, image_role
            """),
            "sealed_products": _rows(connection, """
                select provider_id, product_type, verification_status, count(*) products
                from sealed_product group by provider_id, product_type, verification_status
                order by provider_id, product_type, verification_status
            """),
            "unresolved": _rows(connection, """
                select issue_class, status, language_code, region_code, count(*) items
                from unresolved_item group by issue_class, status, language_code, region_code
                order by issue_class, status, language_code, region_code
            """),
        }
    finally:
        connection.close()


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Worldwide open-dataset staging report", "",
        f"- Generated: `{report['generated_at_utc']}`",
        f"- Database bytes: `{report['database_bytes']:,}`",
        f"- Database SHA-256: `{report['database_sha256']}`",
        f"- SQLite integrity: `{report['integrity']['sqlite_integrity_check']}`",
        f"- Foreign-key failures: `{report['integrity']['foreign_key_failure_count']}`", "",
        "## Core counts", "", "| Entity | Rows |", "|---|---:|",
    ]
    lines.extend(f"| {key} | {value:,} |" for key, value in report["counts"].items())
    lines.extend(["", "## Language and region coverage", "",
                  "| Language | Region | Sets | Printings | Variants | Unknown variants | Quarantined |",
                  "|---|---|---:|---:|---:|---:|---:|"])
    for row in report["language_coverage"]:
        lines.append(
            f"| {row['language_code']} | {row['region_code']} | {row['sets']:,} | "
            f"{row['printings']:,} | {row['variants']:,} | {row['unknown_variants'] or 0:,} | "
            f"{row['quarantined_printings'] or 0:,} |"
        )
    lines.extend(["", "## Enumerated language matrix", "",
                  "| Language | Expected regions | Printings | Status |", "|---|---|---:|---|"])
    for row in report["expected_language_matrix"]:
        lines.append(
            f"| {row['language_code']} | {', '.join(row['expected_regions'])} | "
            f"{row['printings']:,} | {row['inventory_status']} |"
        )
    lines.extend(["", "## Source records", "", "| Provider | Type | Records | Errors |", "|---|---|---:|---:|"])
    for row in report["source_records"]:
        lines.append(f"| {row['provider_id']} | {row['record_type']} | {row['records']:,} | {row['errors'] or 0:,} |")
    lines.extend(["", "This is an acquisition-stage report, not a completion declaration. Candidate mappings and image URLs remain unverified until their dedicated gates pass.", ""])
    return "\n".join(lines)
