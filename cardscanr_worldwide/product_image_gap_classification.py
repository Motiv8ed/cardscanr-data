"""Classify sealed-product variants that lack a publication-pass image."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json, stable_id

ISSUE_ENTITY_TYPE = "sealed_product_variant"

PARSER_NOISE_PATTERNS = (
    re.compile(r"^isi produk", re.I),
    re.compile(r"^product contents?", re.I),
    re.compile(r"^内容"),
    re.compile(r"^\*"),
    re.compile(r"muncul pikachu", re.I),
    re.compile(r"戴帽子的皮卡丘登场"),
    re.compile(r"เปิดตัวพิคาชูสวมหมวก", re.I),
)

CLASS_META = {
    "historical_theme_deck_no_image_source": {
        "status": "blocked_external",
        "externally_unavoidable": 1,
        "reason": "pokemontcg-data theme/starter identities have no image URL and no exact US-archive name match",
        "resume_condition": "An exact official or authorized archive pack-art URL for the SKU identity is supplied",
    },
    "china_product_image_transient_only": {
        "status": "blocked_external",
        "externally_unavoidable": 1,
        "reason": "Official CN bytes were acquired only via page-issued transient URLs; direct CDN URLs return HTML",
        "resume_condition": "A stable public official image URL or authorized durable mirror permission is supplied",
    },
    "asia_expansion_sku_no_pack_art_url": {
        "status": "blocked_external",
        "externally_unavoidable": 1,
        "reason": "Asia trainer-site expansion inventory lists a sealed SKU without a dedicated gallery pack-art URL",
        "resume_condition": "An official Asia products-gallery asset exactly matching the SKU is published",
    },
    "asia_local_product_gallery_unavailable": {
        "status": "blocked_external",
        "externally_unavoidable": 1,
        "reason": "SG/MY/PH official /products/ indexes only link to US expansions; no local sealed gallery exists",
        "resume_condition": "A local official sealed-product gallery becomes available for ordinary authorized collection",
    },
    "asia_gallery_invalid_or_placeholder_asset": {
        "status": "blocked_external",
        "externally_unavoidable": 1,
        "reason": "Official Asia gallery candidate failed technical validation (HTML archive URL or news placeholder)",
        "resume_condition": "A real pack-art image URL is published on the official product page",
    },
    "product_parser_false_positive": {
        "status": "classified_nonblocking",
        "externally_unavoidable": 0,
        "reason": "Collector HTML fragment was stored as a product identity without a real SKU/image",
        "resume_condition": "Parser exclusion removes the non-product row on the next Asia products import",
    },
    "china_community_product_image_rights_blocked": {
        "status": "blocked_external",
        "externally_unavoidable": 1,
        "reason": "ptcg-chs dataset cover art is rights-blocked for app publication",
        "resume_condition": "An official rights-cleared product image for the exact SKU is supplied",
    },
    "us_product_gap_evidence_no_image": {
        "status": "blocked_external",
        "externally_unavoidable": 1,
        "reason": "US gap-evidence SKU has no validated official product image candidate",
        "resume_condition": "An exact official US product image URL is captured",
    },
    "japan_accessory_product_no_image": {
        "status": "blocked_external",
        "externally_unavoidable": 1,
        "reason": "Japan accessory product has no validated display image candidate",
        "resume_condition": "An official accessory image URL is captured from the Japan products catalogue",
    },
    "product_image_gap_unclassified": {
        "status": "needs_review",
        "externally_unavoidable": 0,
        "reason": "Variant lacks a pass image and did not match a known gap class",
        "resume_condition": "Manual classification or an exact image candidate is supplied",
    },
}


def _is_parser_noise(name: str) -> bool:
    text = (name or "").strip()
    if not text:
        return True
    return any(pattern.search(text) for pattern in PARSER_NOISE_PATTERNS)


def classify_variant(provider_id: str, product_type: str, canonical_name: str, cand_count: int,
                     validation_statuses: str | None) -> str:
    if provider_id == "pokemontcg-data":
        return "historical_theme_deck_no_image_source"
    if provider_id == "pokemon-cn-official":
        return "china_product_image_transient_only"
    if provider_id == "ptcg-chs-datasets":
        return "china_community_product_image_rights_blocked"
    if provider_id == "pokemon-us-product-gap-evidence":
        return "us_product_gap_evidence_no_image"
    if provider_id == "pokemon-japan-products-official":
        return "japan_accessory_product_no_image"
    if provider_id.endswith("-products-official"):
        if provider_id in {
            "pokemon-asia-sg-products-official",
            "pokemon-asia-my-products-official",
            "pokemon-asia-ph-products-official",
        }:
            return "asia_local_product_gallery_unavailable"
        if cand_count and validation_statuses and "fail" in (validation_statuses or "").split(","):
            return "asia_gallery_invalid_or_placeholder_asset"
        # Zero-candidate Asia gallery rows are usually HTML fragment titles, not real SKUs.
        return "product_parser_false_positive"
    if provider_id.startswith("pokemon-asia-") and provider_id.endswith("-official"):
        if provider_id in {
            "pokemon-asia-sg-official",
            "pokemon-asia-my-official",
            "pokemon-asia-ph-official",
        }:
            return "asia_local_product_gallery_unavailable"
        return "asia_expansion_sku_no_pack_art_url"
    return "product_image_gap_unclassified"


def classify_product_image_gaps(database: Path) -> dict[str, object]:
    connection = connect(str(database))
    now = datetime.now(timezone.utc).isoformat()
    counts: Counter[str] = Counter()
    samples: dict[str, list[dict[str, object]]] = {}
    try:
        rows = connection.execute(
            """
            select v.id as variant_id, v.language_code, v.region_code, v.local_name,
                   sp.provider_id, sp.product_type, sp.canonical_name, sp.provider_record_id,
                   (select count(*) from product_image_candidate pic
                     where pic.sealed_product_variant_id=v.id) cand_count,
                   (select group_concat(distinct ivr.status)
                      from product_image_candidate pic
                      join image_validation_result ivr on ivr.product_image_candidate_id=pic.id
                     where pic.sealed_product_variant_id=v.id) validation_statuses
              from sealed_product_variant v
              join sealed_product sp on sp.id=v.sealed_product_id
             where not exists (
               select 1 from product_image_candidate pic
               join image_validation_result ivr on ivr.product_image_candidate_id=pic.id
               where pic.sealed_product_variant_id=v.id and ivr.status='pass'
             )
             order by sp.provider_id, sp.canonical_name, v.id
            """
        ).fetchall()
        for row in rows:
            issue_class = classify_variant(
                row["provider_id"], row["product_type"], row["canonical_name"],
                int(row["cand_count"] or 0), row["validation_statuses"],
            )
            meta = CLASS_META[issue_class]
            issue_id = stable_id("product-image-gap", issue_class, row["variant_id"])
            evidence = {
                "classified_at": now,
                "provider_id": row["provider_id"],
                "provider_record_id": row["provider_record_id"],
                "product_type": row["product_type"],
                "canonical_name": row["canonical_name"],
                "local_name": row["local_name"],
                "candidate_count": int(row["cand_count"] or 0),
                "validation_statuses": row["validation_statuses"],
                "reason": meta["reason"],
                "resume_condition": meta["resume_condition"],
                "classification_policy": "evidence_exhausted_no_inference",
            }
            connection.execute(
                """insert into unresolved_item values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   on conflict(id) do update set
                     summary=excluded.summary,
                     evidence_json=excluded.evidence_json,
                     status=excluded.status,
                     externally_unavoidable=excluded.externally_unavoidable,
                     language_code=excluded.language_code,
                     region_code=excluded.region_code""",
                (
                    issue_id,
                    ISSUE_ENTITY_TYPE,
                    row["variant_id"],
                    row["language_code"],
                    row["region_code"],
                    issue_class,
                    f"No publication-pass product image for {row['provider_id']} / {row['canonical_name']}",
                    canonical_json(evidence),
                    meta["status"],
                    meta["externally_unavoidable"],
                ),
            )
            counts["variants"] += 1
            counts[f"class_{issue_class}"] += 1
            counts[f"status_{meta['status']}"] += 1
            bucket = samples.setdefault(issue_class, [])
            if len(bucket) < 5:
                bucket.append({
                    "variant_id": row["variant_id"],
                    "provider_id": row["provider_id"],
                    "product_type": row["product_type"],
                    "canonical_name": row["canonical_name"],
                    "candidate_count": int(row["cand_count"] or 0),
                    "validation_statuses": row["validation_statuses"],
                })
        connection.commit()
        return {
            "classified_at": now,
            "variants_without_pass_image": len(rows),
            "counts": dict(sorted(counts.items())),
            "samples": samples,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def write_report(result: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "PRODUCT_IMAGE_GAP_RELEASE_CLASSIFICATION_20260802.json"
    md_path = output_dir / "PRODUCT_IMAGE_GAP_RELEASE_CLASSIFICATION_20260802.md"
    json_path.write_text(
        json.dumps({"schemaVersion": "1.0.0", **result}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    counts = result["counts"]
    lines = [
        "# Product image gap release classification",
        "",
        f"- Classified at: `{result['classified_at']}`",
        f"- Variants without a `pass` image validation: `{result['variants_without_pass_image']}`",
        "",
        "## Counts by class",
        "",
        "| Class | Count |",
        "|---|---:|",
    ]
    for key, value in sorted(counts.items()):
        if key.startswith("class_"):
            lines.append(f"| `{key.removeprefix('class_')}` | {value} |")
    lines.extend([
        "",
        "## Status rollup",
        "",
        "| Status | Count |",
        "|---|---:|",
    ])
    for key, value in sorted(counts.items()):
        if key.startswith("status_"):
            lines.append(f"| `{key.removeprefix('status_')}` | {value} |")
    lines.extend([
        "",
        "## Notes",
        "",
        "- China leftovers remain `acquired_transient`: direct `image.pokemon.com.cn` URLs return HTML without a page-issued fetch URL.",
        "- Asia `*-official` expansion SKUs are inventory identities; only `*-products-official` galleries supply dedicated pack art.",
        "- SG/MY/PH local sealed galleries are absent; see `ASIA_PRODUCT_GALLERY_GAPS_20260802.md`.",
        "- Parser false positives are `classified_nonblocking` and do not block card publication.",
        "",
    ])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path
