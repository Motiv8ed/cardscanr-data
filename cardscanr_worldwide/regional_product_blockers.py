"""Register explicit external blockers for missing regional sealed-product catalogues."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json, stable_id

ISSUE_CLASS = "regional_sealed_product_catalogue_unavailable"


def register_regional_product_blockers(database: Path, scope_path: Path) -> dict[str, object]:
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    connection = connect(str(database))
    now = datetime.now(timezone.utc).isoformat()
    counts: Counter[str] = Counter()
    try:
        covered = {
            (row[0], row[1])
            for row in connection.execute(
                "select distinct language_code,region_code from sealed_product_variant where language_code is not null"
            )
        }
        expected = {
            (language["code"], region)
            for language in scope["languages"]
            if language["officially_printed"]
            for region in language["regional_variants"]
        }
        for language, region in sorted(expected):
            entity_id = f"{language}:{region}"
            issue_id = stable_id(ISSUE_CLASS, entity_id)
            if (language, region) in covered:
                connection.execute(
                    """update unresolved_item set status='resolved',externally_unavoidable=0
                         where id=? and issue_class=?""",
                    (issue_id, ISSUE_CLASS),
                )
                counts["covered_regions"] += 1
                continue
            evidence = {
                "classified_at": now,
                "language_code": language,
                "region_code": region,
                "current_normalized_product_variants": 0,
                "alternatives_exhausted": [
                    "open-dataset and official regional collectors already represented in staging",
                    "official localized Pokemon sealed-product gallery ordinary-browser inspection",
                    "cookie-free HTTP inspection stopped by the provider access control",
                ],
                "official_database_report": "reports/worldwide_catalogue/OFFICIAL_LOCALIZED_DATABASE_BLOCKER_20260802.md",
                "resume_condition": (
                    "A stable authorized regional product export/API becomes available or the owner obtains "
                    "written permission and an approved acquisition method"
                ),
            }
            connection.execute(
                """insert into unresolved_item values (?, 'regional_product_catalogue', ?, ?, ?, ?, ?, ?,
                       'blocked_external', 1)
                   on conflict(id) do update set summary=excluded.summary,evidence_json=excluded.evidence_json,
                     status=excluded.status,externally_unavoidable=excluded.externally_unavoidable""",
                (
                    issue_id,
                    entity_id,
                    language,
                    region,
                    ISSUE_CLASS,
                    "No exact normalized sealed-product catalogue is available for this expected language/region",
                    canonical_json(evidence),
                ),
            )
            counts["blocked_regions"] += 1
            counts[f"language_{language}"] += 1
        connection.commit()
        return {
            "classified_at": now,
            "expected_language_regions": len(expected),
            "covered_language_regions": len(expected & covered),
            "counts": dict(sorted(counts.items())),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def write_report(result: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "REGIONAL_PRODUCT_EXTERNAL_BLOCKERS_20260802.json"
    md_path = output_dir / "REGIONAL_PRODUCT_EXTERNAL_BLOCKERS_20260802.md"
    json_path.write_text(json.dumps({"schemaVersion": "1.0.0", **result}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    counts = result["counts"]
    lines = [
        "# Regional sealed-product external blockers",
        "",
        f"- Expected printed language/region pairs: `{result['expected_language_regions']:,}`",
        f"- Pairs with an exact normalized product catalogue: `{result['covered_language_regions']:,}`",
        f"- Pairs blocked on an authorized exact source: `{counts.get('blocked_regions', 0):,}`",
        "",
        "Each missing pair is stored in staging as `regional_sealed_product_catalogue_unavailable` with "
        "`blocked_external` status. Existing US, Japan, Korea, China, and Pokemon Asia catalogues are preserved; "
        "they are not projected onto regions whose packaging, contents, language, or release identity has not been verified.",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path

