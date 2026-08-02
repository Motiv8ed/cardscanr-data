"""Finalize Asia product-gallery pages that are not sealed-product SKU pages."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json

ISSUE_CLASS = "official_product_page_unparsed"

# Special-card / set archive indexes are card pages, not sealed-product SKUs.
NON_PRODUCT_PAGE = re.compile(
    r"/archive/special/card/|"
    r"/card-search|"
    r"/cards?/|"
    r"/expansions?/",
    re.I,
)


def classify_asia_unparsed_product_pages(database: Path) -> dict[str, object]:
    connection = connect(str(database))
    now = datetime.now(timezone.utc).isoformat()
    counts: Counter[str] = Counter()
    samples: list[dict[str, object]] = []
    try:
        rows = connection.execute(
            """
            select id, entity_id, language_code, region_code, evidence_json, status
              from unresolved_item
             where issue_class=?
               and status in ('open','needs_review')
             order by id
            """,
            (ISSUE_CLASS,),
        ).fetchall()
        for row in rows:
            page_url = row["entity_id"] or ""
            evidence = json.loads(row["evidence_json"] or "{}")
            if NON_PRODUCT_PAGE.search(page_url):
                status = "classified_nonblocking"
                reason = "Preserved page is a special-card/set archive index, not a sealed-product SKU page"
                externally = 0
                counts["non_product_archive_pages"] += 1
            else:
                status = "blocked_external"
                reason = "Official product-gallery page preserved but yields no exact product block after parser expansion"
                externally = 1
                counts["exhausted_product_pages"] += 1
            evidence["page_parse_finalization"] = {
                "classified_at": now,
                "reason": reason,
                "resume_condition": (
                    "N/A for non-product archive pages"
                    if status == "classified_nonblocking"
                    else "An exact product block or authorized product export becomes available"
                ),
            }
            connection.execute(
                """update unresolved_item
                      set evidence_json=?, status=?, externally_unavoidable=?
                    where id=?""",
                (canonical_json(evidence), status, externally, row["id"]),
            )
            counts["pages"] += 1
            counts[f"status_{status}"] += 1
            if len(samples) < 8:
                samples.append({
                    "id": row["id"],
                    "page_url": page_url,
                    "language_code": row["language_code"],
                    "region_code": row["region_code"],
                    "status": status,
                })
        connection.commit()
        return {"classified_at": now, "counts": dict(sorted(counts.items())), "samples": samples}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def write_report(result: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "ASIA_UNPARSED_PRODUCT_PAGE_CLASSIFICATION_20260802.json"
    md_path = output_dir / "ASIA_UNPARSED_PRODUCT_PAGE_CLASSIFICATION_20260802.md"
    json_path.write_text(
        json.dumps({"schemaVersion": "1.0.0", **result}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    counts = result["counts"]
    lines = [
        "# Asia unparsed product-page classification",
        "",
        f"- Classified at: `{result['classified_at']}`",
        f"- Pages classified: `{counts.get('pages', 0)}`",
        f"- Non-product archive pages (nonblocking): `{counts.get('non_product_archive_pages', 0)}`",
        f"- Exhausted product pages (external): `{counts.get('exhausted_product_pages', 0)}`",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path
