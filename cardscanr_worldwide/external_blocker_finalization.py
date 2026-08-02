"""Finalize known evidence-exhausted staging issues without hiding active collection work."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json

CLASSIFICATIONS = {
    "official_product_detail_unavailable": {
        "reason": "Official product identity/artwork is preserved, but live detail pages return 410 and no archive capture exists",
        "resume_condition": "An official detail capture or authorized archive becomes available",
    },
    "existing_r2_image_identity_review": {
        "reason": "The preserved verified object has no unique staged card-variant crosswalk after exact reconciliation",
        "resume_condition": "A unique authoritative set, collector, language, region, and variant crosswalk is supplied",
    },
    "source_text_quality": {
        "reason": "The source text is known implausible and the affected printing is quarantined; no exact official replacement matched",
        "resume_condition": "An exact authoritative localized record is supplied",
    },
    "missing_official_local_name": {
        "reason": "The community source supplies only an English translation and cannot establish the official Simplified Chinese name",
        "resume_condition": "An exact official Chinese record or authorized identity crosswalk is supplied",
    },
    "official_count_shortfall": {
        "reason": "The community inventory is shorter than its stated official count and no exact missing identities are available",
        "resume_condition": "The missing authoritative set roster identities are supplied",
    },
    "official_set_membership_unavailable": {
        "reason": "The official card detail was collected but omits the set identity required for an exact canonical printing",
        "resume_condition": "An authoritative exact set-release membership for the official card identity is supplied",
    },
}

MISSING_IMAGE_CLASSIFICATION = {
    "reason": "All exact image alternatives were exhausted: TCGdex returned 404 and the official Japanese reconciliation found no exact candidate",
    "resume_condition": "A full, exact, rights-reviewed image mapped to the canonical variant is supplied",
}


def finalize_external_blockers(database: Path, include_missing_card_images: bool = False) -> dict[str, object]:
    selected = dict(CLASSIFICATIONS)
    if include_missing_card_images:
        selected["missing_card_image"] = MISSING_IMAGE_CLASSIFICATION
    connection = connect(str(database))
    now = datetime.now(timezone.utc).isoformat()
    counts: Counter[str] = Counter()
    try:
        placeholders = ",".join("?" for _ in selected)
        rows = connection.execute(
            f"""select * from unresolved_item
                  where issue_class in ({placeholders})
                    and status in ('open','needs_review','documented_exhausted')
                  order by id""",
            tuple(selected),
        ).fetchall()
        for row in rows:
            classification = selected[row["issue_class"]]
            evidence = json.loads(row["evidence_json"] or "{}")
            evidence["external_blocker"] = {
                "classified_at": now,
                **classification,
                "classification_policy": "evidence_exhausted_no_inference",
            }
            if row["issue_class"] == "missing_card_image":
                evidence["external_blocker"]["official_reconciliation_report"] = (
                    "reports/worldwide_catalogue/JAPAN_CARD_IMAGE_RECONCILIATION_20260802.md"
                )
            connection.execute(
                """update unresolved_item
                      set evidence_json=?,status='blocked_external',externally_unavoidable=1
                    where id=?""",
                (canonical_json(evidence), row["id"]),
            )
            counts["items"] += 1
            counts[f"issue_{row['issue_class']}"] += 1
        connection.commit()
        return {
            "classified_at": now,
            "included_missing_card_images": include_missing_card_images,
            "classification": dict(sorted(counts.items())),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def write_report(result: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "EXTERNAL_BLOCKER_FINALIZATION_20260802.json"
    md_path = output_dir / "EXTERNAL_BLOCKER_FINALIZATION_20260802.md"
    json_path.write_text(json.dumps({"schemaVersion": "1.0.0", **result}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    counts = result["classification"]
    lines = [
        "# Evidence-exhausted external blocker finalization",
        "",
        f"- Items classified: `{counts.get('items', 0):,}`",
        f"- Missing-card-image residuals included: `{str(result['included_missing_card_images']).lower()}`",
        "",
        "This operation excludes active `official_detail_not_collected` work. It does not promote, infer, or delete "
        "records; each affected row retains its original evidence plus a class-specific resume condition.",
        "",
    ]
    for key, value in sorted(counts.items()):
        if key.startswith("issue_"):
            lines.append(f"- `{key.removeprefix('issue_')}`: `{value:,}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path
