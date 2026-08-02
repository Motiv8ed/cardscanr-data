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
            "product_image_candidate", "image_validation_result", "image_acquisition_attempt",
            "publication_run", "publication_artifact", "accessory", "unresolved_item",
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
        release_reconciliation = _rows(connection, """
            with release_counts as (
              select sr.id,sr.language_code,sr.region_code,sr.official_count,count(cp.id) actual_count
                from set_release sr left join card_printing cp on cp.set_release_id=sr.id
               group by sr.id
            )
            select language_code,region_code,count(*) releases,
                   sum(case when actual_count>0 then 1 else 0 end) populated_releases,
                   sum(case when actual_count=0 then 1 else 0 end) zero_printing_releases,
                   sum(coalesce(official_count,0)) declared_official_cards,
                   sum(actual_count) normalized_printings,
                   sum(case when official_count is not null and actual_count=official_count then 1 else 0 end) exact_count_releases,
                   sum(case when official_count is not null and actual_count<official_count then 1 else 0 end) shortfall_releases,
                   sum(case when official_count is not null and actual_count>official_count then 1 else 0 end) excess_releases,
                   sum(case when official_count is null then 1 else 0 end) unknown_count_releases
              from release_counts group by language_code,region_code order by language_code,region_code
        """)
        image_readiness = dict(connection.execute("""
            select count(distinct cp.id) total_printings,
                   count(distinct case when cic.id is not null then cp.id end) with_candidate,
                   count(distinct case when cic.validation_status in ('verified','acquired','published') then cp.id end) technically_verified,
                   count(distinct case when cic.validation_status in ('verified','acquired','published')
                                        and cic.rights_status in ('approved_for_mirror','link_only') then cp.id end) app_eligible
              from card_printing cp left join card_variant cv on cv.card_printing_id=cp.id
              left join card_image_candidate cic on cic.card_variant_id=cv.id
        """).fetchone())
        product_image_readiness = dict(connection.execute("""
            select count(distinct spv.id) total_product_variants,
                   count(distinct case when pic.id is not null then spv.id end) with_candidate,
                   count(distinct case when pic.validation_status in ('verified','acquired','published') then spv.id end) technically_verified,
                   count(distinct case when pic.validation_status in ('verified','acquired','published')
                                        and pic.rights_status in ('approved_for_mirror','link_only') then spv.id end) app_eligible
              from sealed_product_variant spv left join product_image_candidate pic
                on pic.sealed_product_variant_id=spv.id
        """).fetchone())
        publication_gates = {
            "collector_number_collision_groups": connection.execute("""
                select count(*) from (
                  select set_release_id,collector_number,count(*) rows
                    from card_printing group by set_release_id,collector_number having count(*)>1
                )
            """).fetchone()[0],
            "classified_collector_collision_groups": connection.execute("""
                select count(*) from unresolved_item where issue_class='collector_number_collision'
                  and status='classified_nonblocking'
            """).fetchone()[0],
            "collector_collision_groups_needing_review": connection.execute("""
                select count(*) from unresolved_item where issue_class='collector_number_collision'
                  and status='needs_review'
            """).fetchone()[0],
            "secret_bearing_card_image_urls": connection.execute("""
                select count(*) from card_image_candidate
                 where lower(source_url) like '%token=%' or lower(source_url) like '%api_key=%'
                    or lower(source_url) like '%apikey=%' or lower(source_url) like '%localhost%'
            """).fetchone()[0],
            "secret_bearing_product_image_urls": connection.execute("""
                select count(*) from product_image_candidate
                 where lower(source_url) like '%token=%' or lower(source_url) like '%api_key=%'
                    or lower(source_url) like '%apikey=%' or lower(source_url) like '%localhost%'
            """).fetchone()[0],
            "secret_bearing_source_payloads": connection.execute("""
                select count(*) from source_record where lower(coalesce(raw_payload_json,'')) like '%auth_key=%'
                   or lower(coalesce(raw_payload_json,'')) like '%token=%'
                   or lower(coalesce(raw_payload_json,'')) like '%api_key=%'
                   or lower(coalesce(raw_payload_json,'')) like '%x-amz-signature=%'
            """).fetchone()[0],
            "secret_bearing_product_payloads": connection.execute("""
                select count(*) from sealed_product where lower(raw_product_json) like '%auth_key=%'
                   or lower(raw_product_json) like '%token=%' or lower(raw_product_json) like '%api_key=%'
                   or lower(raw_product_json) like '%x-amz-signature=%'
            """).fetchone()[0],
            "transient_only_product_image_candidates": connection.execute(
                "select count(*) from product_image_candidate where validation_status='acquired_transient'"
            ).fetchone()[0],
            "open_unresolved_items": connection.execute(
                "select count(*) from unresolved_item where status in ('open','needs_review')"
            ).fetchone()[0],
            "external_blocker_items": connection.execute(
                "select count(*) from unresolved_item where externally_unavoidable=1 and status='blocked_external'"
            ).fetchone()[0],
            "failed_image_validation_results": connection.execute(
                "select count(*) from image_validation_result where status='fail'"
            ).fetchone()[0],
            "not_found_image_acquisition_attempts": connection.execute(
                "select count(*) from image_acquisition_attempt where outcome='not_found'"
            ).fetchone()[0],
            "active_publication_runs": connection.execute(
                "select count(*) from publication_run where status='active'"
            ).fetchone()[0],
        }
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
            "release_reconciliation": release_reconciliation,
            "expected_language_matrix": expected_language_matrix,
            "image_readiness": image_readiness,
            "product_image_readiness": product_image_readiness,
            "publication_gates": publication_gates,
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
            "image_acquisition_attempts": _rows(connection, """
                select provider_id,entity_type,outcome,http_status,count(*) attempts
                  from image_acquisition_attempt
                 group by provider_id,entity_type,outcome,http_status
                 order by provider_id,entity_type,outcome,http_status
            """),
            "image_validation_results": _rows(connection, """
                select validator,status,count(*) validations
                  from image_validation_result group by validator,status order by validator,status
            """),
            "publication_runs": _rows(connection, """
                select id,version,status,catalogue_sha256,manifest_sha256,object_prefix,
                       previous_publication_id,started_at,activated_at,completed_at,rollback_retained
                  from publication_run order by started_at,id
            """),
            "sealed_products": _rows(connection, """
                select provider_id, product_type, verification_status, count(*) products
                from sealed_product group by provider_id, product_type, verification_status
                order by provider_id, product_type, verification_status
            """),
            "sealed_product_regions": _rows(connection, """
                select language_code,region_code,count(*) product_variants
                  from sealed_product_variant group by language_code,region_code
                 order by language_code,region_code
            """),
            "provider_rights": _rows(connection, """
                select rights_status,count(*) providers from source_provider
                 group by rights_status order by rights_status
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
    lines.extend(["", "## Publication readiness", "",
                  "| Gate | Value |", "|---|---:|"])
    lines.extend(f"| {key} | {value:,} |" for key, value in report["publication_gates"].items())
    lines.extend(["", "### Card images", "",
                  "| Total printings | With candidate | Technically verified | App eligible |",
                  "|---:|---:|---:|---:|",
                  f"| {report['image_readiness']['total_printings']:,} | {report['image_readiness']['with_candidate']:,} | "
                  f"{report['image_readiness']['technically_verified']:,} | {report['image_readiness']['app_eligible']:,} |",
                  "", "### Product images", "",
                  "| Product variants | With candidate | Technically verified | App eligible |",
                  "|---:|---:|---:|---:|",
                  f"| {report['product_image_readiness']['total_product_variants']:,} | "
                  f"{report['product_image_readiness']['with_candidate']:,} | "
                  f"{report['product_image_readiness']['technically_verified']:,} | "
                  f"{report['product_image_readiness']['app_eligible']:,} |"])
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
