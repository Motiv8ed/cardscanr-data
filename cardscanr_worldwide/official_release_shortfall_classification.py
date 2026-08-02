"""Register evidence-exhausted official_count shortfalls on set_release rows."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json, stable_id

ISSUE_CLASS = "official_count_shortfall"

COMMUNITY_PROVIDERS = {
    "tcgdex-cards-database",
    "pokemontcg-data",
    "ptcg-chs-datasets",
}


def classify_official_release_shortfalls(database: Path) -> dict[str, object]:
    connection = connect(str(database))
    now = datetime.now(timezone.utc).isoformat()
    counts: Counter[str] = Counter()
    try:
        rows = connection.execute(
            """
            with actual as (
              select sr.id, sr.language_code, sr.region_code, sr.official_count,
                     count(cp.id) actual_count, cs.provider_id, cs.canonical_name
                from set_release sr
                left join card_printing cp on cp.set_release_id=sr.id
                join card_set cs on cs.id=sr.card_set_id
               group by sr.id
            )
            select * from actual a
             where a.official_count is not null and a.actual_count < a.official_count
               and not exists (
                 select 1 from unresolved_item u
                  where u.entity_id=a.id
                    and u.issue_class like '%shortfall%'
                    and u.status in ('classified_nonblocking','blocked_external')
               )
             order by a.provider_id, a.language_code, a.id
            """
        ).fetchall()
        for row in rows:
            issue_id = stable_id(ISSUE_CLASS, row["id"])
            community = row["provider_id"] in COMMUNITY_PROVIDERS
            status = "blocked_external"
            reason = (
                "Community inventory is shorter than its declared official count and no exact missing identities remain"
                if community else
                "Official or archive inventory is shorter than its declared official count after collection"
            )
            evidence = {
                "classified_at": now,
                "provider_id": row["provider_id"],
                "set_release_id": row["id"],
                "canonical_name": row["canonical_name"],
                "official_count": row["official_count"],
                "actual_count": row["actual_count"],
                "shortfall": int(row["official_count"]) - int(row["actual_count"]),
                "reason": reason,
                "resume_condition": "The missing authoritative set roster identities are supplied",
                "classification_policy": "evidence_exhausted_no_inference",
            }
            connection.execute(
                """insert into unresolved_item values (?, 'set_release', ?, ?, ?, ?, ?, ?, ?, 1)
                   on conflict(id) do update set
                     summary=excluded.summary,
                     evidence_json=excluded.evidence_json,
                     status=excluded.status,
                     externally_unavoidable=1,
                     language_code=excluded.language_code,
                     region_code=excluded.region_code""",
                (
                    issue_id,
                    row["id"],
                    row["language_code"],
                    row["region_code"],
                    ISSUE_CLASS,
                    f"Official count shortfall for {row['provider_id']} / {row['canonical_name']}",
                    canonical_json(evidence),
                    status,
                ),
            )
            counts["releases"] += 1
            counts[f"provider_{row['provider_id']}"] += 1
            counts[f"language_{row['language_code']}"] += 1
        connection.commit()
        return {"classified_at": now, "counts": dict(sorted(counts.items()))}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def write_report(result: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "OFFICIAL_RELEASE_SHORTFALL_CLASSIFICATION_20260802.json"
    md_path = output_dir / "OFFICIAL_RELEASE_SHORTFALL_CLASSIFICATION_20260802.md"
    json_path.write_text(json.dumps({"schemaVersion": "1.0.0", **result}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    counts = result["counts"]
    lines = [
        "# Official release shortfall classification",
        "",
        f"- Classified at: `{result['classified_at']}`",
        f"- Releases classified: `{counts.get('releases', 0)}`",
        "",
        "Each previously unexplained `official_count` shortfall is stored as "
        "`official_count_shortfall` with `blocked_external` status.",
        "",
        "| Key | Count |",
        "|---|---:|",
    ]
    for key, value in sorted(counts.items()):
        lines.append(f"| `{key}` | {value} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path
