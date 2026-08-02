"""Normalize regional scope and classify exhausted derived-roster gaps."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json

DERIVATION_PROVIDER = "cardscanr-regional-roster-derivations"
BLOCKED_ISSUES = (
    "regional_card_metadata_unverified",
    "regional_variant_unclassified",
    "regional_card_image_missing",
)


def _pt_conflicts(connection: sqlite3.Connection, table: str) -> int:
    if table == "set_release":
        return connection.execute(
            """select count(*) from set_release source
                 join set_release target
                   on target.card_set_id=source.card_set_id
                  and target.language_code=source.language_code
                  and target.region_code='BR'
                where source.language_code='pt' and source.region_code='INTL'"""
        ).fetchone()[0]
    return connection.execute(
        """select count(*) from sealed_product_variant source
             join sealed_product_variant target
               on target.sealed_product_id=source.sealed_product_id
              and target.language_code=source.language_code
              and target.region_code='BR'
              and target.variant_key=source.variant_key
            where source.language_code='pt' and source.region_code='INTL'"""
    ).fetchone()[0]


def normalize_portuguese_brazil_scope(connection: sqlite3.Connection) -> dict[str, int]:
    """Assign generic Portuguese records to Brazil without inventing pt-PT rows."""

    for table in ("set_release", "sealed_product_variant"):
        conflicts = _pt_conflicts(connection, table)
        if conflicts:
            raise RuntimeError(f"Cannot normalize Portuguese scope: {conflicts} {table} conflicts")
    counters: dict[str, int] = {}
    cursor = connection.execute(
        "update set_release set region_code='BR' where language_code='pt' and region_code='INTL'"
    )
    counters["set_releases"] = cursor.rowcount
    cursor = connection.execute(
        "update sealed_product_variant set region_code='BR' where language_code='pt' and region_code='INTL'"
    )
    counters["sealed_product_variants"] = cursor.rowcount
    cursor = connection.execute(
        "update unresolved_item set region_code='BR' where language_code='pt' and region_code='INTL'"
    )
    counters["unresolved_items"] = cursor.rowcount
    return counters


def classify_derived_regional_blockers(database: Path) -> dict[str, object]:
    """Classify only derived regional gaps after all safe exact sources were exhausted."""

    connection = connect(str(database))
    now = datetime.now(timezone.utc).isoformat()
    counts: Counter[str] = Counter()
    try:
        normalization = normalize_portuguese_brazil_scope(connection)
        rows = connection.execute(
            """select u.*,sr.provider_id source_provider_id
                 from unresolved_item u
                 join card_printing cp on cp.id=u.entity_id
                 join source_record sr on sr.id=cp.source_record_id
                where u.entity_type='card_printing'
                  and sr.provider_id=?
                  and u.issue_class in (?,?,?)
                  and u.status in ('open','needs_review','blocked_external')
                order by u.id""",
            (DERIVATION_PROVIDER, *BLOCKED_ISSUES),
        ).fetchall()
        for row in rows:
            evidence = json.loads(row["evidence_json"] or "{}")
            evidence["external_blocker"] = {
                "classified_at": now,
                "reason": (
                    "Exact localized card metadata, physical-variant evidence, or an exact image "
                    "is not available from the exhausted safe sources"
                ),
                "alternatives_exhausted": [
                    "TCGdex exact language-specific set roster endpoint",
                    "official localized Pokemon card database ordinary-browser and cookie-free HTTP inspection",
                    "public checklist corroboration that did not supply exact localized metadata or images",
                ],
                "tcgdex_probe_report": "reports/worldwide_catalogue/TCGDEX_REGIONAL_ROSTER_PROBE_20260802.md",
                "official_database_report": "reports/worldwide_catalogue/OFFICIAL_LOCALIZED_DATABASE_BLOCKER_20260802.md",
                "resume_condition": (
                    "An exact authorized source becomes available or the owner obtains written permission "
                    "and an approved acquisition method"
                ),
            }
            connection.execute(
                """update unresolved_item
                      set evidence_json=?,status='blocked_external',externally_unavoidable=1
                    where id=?""",
                (canonical_json(evidence), row["id"]),
            )
            counts["items"] += 1
            counts[f"issue_{row['issue_class']}"] += 1
            counts[f"language_{row['language_code']}"] += 1
        connection.commit()
        return {
            "classified_at": now,
            "normalization": normalization,
            "classification": dict(sorted(counts.items())),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def write_report(result: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "REGIONAL_EXTERNAL_BLOCKERS_20260802.json"
    md_path = output_dir / "REGIONAL_EXTERNAL_BLOCKERS_20260802.md"
    payload = {"schemaVersion": "1.0.0", **result}
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    classification = result["classification"]
    normalization = result["normalization"]
    lines = [
        "# Regional external blocker classification",
        "",
        f"- Evidence-exhausted derived items classified: `{classification.get('items', 0):,}`",
        f"- Portuguese set releases reassigned from `INTL` to `BR`: `{normalization.get('set_releases', 0):,}`",
        f"- Portuguese sealed-product variants reassigned: `{normalization.get('sealed_product_variants', 0):,}`",
        f"- Portuguese unresolved rows reassigned: `{normalization.get('unresolved_items', 0):,}`",
        "",
        "Only rows created by `cardscanr-regional-roster-derivations` were classified. The records remain "
        "provisional; this step does not infer a localized card name, rules text, image, finish, foil pattern, or stamp.",
        "",
        "The embedded evidence links the zero-result TCGdex roster probe and the official localized-database "
        "access-control boundary. Collection can resume if an exact authorized source or approved method becomes available.",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path

