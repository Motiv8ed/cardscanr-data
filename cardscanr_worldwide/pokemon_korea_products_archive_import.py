"""Import archived official Korean product inventories into worldwide staging."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json, digest, stable_id

PROVIDER_ID = "pokemon-korea-products-official-archive"


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def classify_product(name: str, categories: list[str]) -> tuple[str, str | None]:
    if "카드 실드" in name:
        return "accessory_product", "sleeves"
    if "플레이매트" in name:
        return "accessory_product", "playmat"
    if "덱 케이스" in name or "카드 박스" in name:
        return "accessory_product", "deck_box"
    if any(token in name for token in ("컬렉션 파일", "링 바인더", "앨범")):
        return "accessory_product", "binder"
    if any(token in name for token in ("틴 케이스", "파우치", "보관")):
        return "accessory_product", "storage"
    if any(token in name for token in ("주사위", "데미지 카운터", "코인")):
        return "accessory_product", "other"
    if "확장팩" in name and "세트" not in name and "BOX" not in name and "박스" not in name:
        return "booster_pack", None
    if any(token in name for token in ("스타터 세트", "스타트 덱", "구축덱", "60장 덱", "30장 덱")):
        return "starter_deck", None
    if any(token in name for token in ("스페셜", "키트", "컬렉션", "세트")):
        return "special_collection", None
    if any(token in name for token in ("BOX", "박스")):
        return "collection_box", None
    if "info1" in categories:
        return "booster_pack", None
    if "info2" in categories:
        return "constructed_deck", None
    return "official_product", None


def content_normalization(value: str) -> tuple[str, int]:
    matches = re.findall(r"(\d+)\s*(팩|장|개|권|세트|박스)", value)
    quantity = int(matches[-1][0]) if matches else 1
    if "팩" in value:
        return "booster_pack", quantity
    if "프로모" in value and "카드" in value:
        return "promotional_card", quantity
    if any(token in value for token in ("실드", "매트", "케이스", "앨범", "파일", "코인", "주사위")):
        return "accessory", quantity
    if "카드" in value:
        return "card", quantity
    return "other", quantity


def import_checkpoint(database: Path, checkpoint_path: Path) -> dict[str, int]:
    checkpoint = sqlite3.connect(f"file:{checkpoint_path.resolve()}?mode=ro", uri=True)
    checkpoint.row_factory = sqlite3.Row
    if checkpoint.execute("select count(*) from runs where status='running'").fetchone()[0]:
        checkpoint.close()
        raise RuntimeError("Korean product archive checkpoint still has a running collector")
    snapshot_sha = file_sha256(checkpoint_path)
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    snapshot_id = stable_id(PROVIDER_ID, "snapshot", snapshot_sha[:24])
    counters: Counter[str] = Counter()
    connection = connect(str(database))
    try:
        connection.execute(
            """insert into source_provider values (?, 'Pokémon Korea official product archive',
               'official_archive', 'https://pokemoncard.co.kr/card', 'link_only',
               'Pokémon Korea via the Internet Archive', 'https://pokemoncard.co.kr/', null)
               on conflict(id) do update set rights_status=excluded.rights_status""", (PROVIDER_ID,),
        )
        connection.execute(
            "insert into import_run values (?, ?, 'running', ?, ?, '{}', '{}', ?, null, null)",
            (run_id, PROVIDER_ID, str(checkpoint_path), snapshot_sha, now),
        )
        connection.execute(
            """insert into source_snapshot values (?, ?, ?, ?, ?, null, ?, ?, ?)
               on conflict(id) do update set import_run_id=excluded.import_run_id,fetched_at=excluded.fetched_at""",
            (snapshot_id, PROVIDER_ID, run_id, str(checkpoint_path), snapshot_sha,
             checkpoint_path.stat().st_size, now, str(checkpoint_path.parent / "raw")),
        )
        for row in checkpoint.execute("select * from products order by cast(provider_record_id as integer)"):
            categories = json.loads(row["categories_json"])
            parsed = json.loads(row["parsed_json"]) if row["parsed_json"] else {}
            name = parsed.get("local_name") or row["listing_name"]
            payload = canonical_json({
                "archive_timestamp": row["archive_timestamp"],
                "archive_digest": row["archive_digest"],
                "replay_url": row["replay_url"],
                "categories": categories,
                "listing_name": row["listing_name"],
                "listing_image_url": row["listing_image_url"],
                "detail_status": row["status"],
                "detail": parsed or None,
            })
            evidence_sha = row["raw_sha256"] or row["listing_evidence_sha256"]
            source_id = stable_id(PROVIDER_ID, row["provider_record_id"], evidence_sha[:16])
            connection.execute(
                """insert into source_record values (?, ?, ?, ?, 'sealed_product', ?, null, ?, ?, ?, null)
                   on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
                (source_id, PROVIDER_ID, run_id, snapshot_id, row["provider_record_id"],
                 row["source_url"] or f"https://pokemoncard.co.kr/card/{row['provider_record_id']}",
                 digest(payload), payload),
            )
            product_id = stable_id(PROVIDER_ID, "sealed", row["provider_record_id"])
            variant_id = stable_id(product_id, "ko", "KR", "standard")
            product_type, accessory_type = classify_product(name, categories)
            verification = "verified" if row["status"] == "parsed" else "corroborated"
            connection.execute(
                """insert into sealed_product values (?, ?, ?, ?, ?, ?, ?, ?)
                   on conflict(id) do update set source_record_id=excluded.source_record_id,
                    canonical_name=excluded.canonical_name,product_type=excluded.product_type,
                    verification_status=excluded.verification_status,raw_product_json=excluded.raw_product_json""",
                (product_id, PROVIDER_ID, row["provider_record_id"], source_id, name,
                 product_type, verification, payload),
            )
            connection.execute(
                """insert into sealed_product_variant values (?, ?, 'ko', 'KR', ?, 'standard', ?, ?)
                   on conflict(id) do update set local_name=excluded.local_name,release_date=excluded.release_date,
                    attributes_json=excluded.attributes_json""",
                (variant_id, product_id, name, parsed.get("release_date"), canonical_json({
                    "price_krw": parsed.get("price_krw"), "price_text": parsed.get("price_text"),
                    "currency": "KRW", "notice": parsed.get("notice"), "categories": categories,
                    "archive_timestamp": row["archive_timestamp"], "detail_status": row["status"],
                })),
            )
            for ordinal, content in enumerate(parsed.get("contents") or []):
                kind, quantity = content_normalization(content)
                connection.execute(
                    "insert or replace into product_content values (?, ?, ?, null, ?, ?, '{}')",
                    (variant_id, ordinal, kind, content, quantity),
                )
                counters["product_contents"] += 1
            images: list[dict[str, object]] = []
            if row["listing_image_url"]:
                images.append({"canonical_url": row["listing_image_url"], "role": "listing", "alt": row["listing_name"]})
            images.extend({**image, "role": "display"} for image in parsed.get("images") or [])
            seen: set[str] = set()
            for ordinal, image in enumerate(images):
                url = str(image["canonical_url"])
                if url in seen:
                    continue
                seen.add(url)
                image_id = stable_id(variant_id, PROVIDER_ID, image["role"], digest(url)[:16])
                connection.execute(
                    """insert or replace into product_image_candidate values (?, ?, ?, ?, ?, ?,
                       'link_only', 'candidate', ?)""",
                    (image_id, variant_id, source_id, PROVIDER_ID, image["role"], url,
                     canonical_json({"ordinal": ordinal, "alt": image.get("alt"),
                                     "source_url": image.get("source_url")})),
                )
                counters["product_images"] += 1
            if accessory_type:
                accessory_id = stable_id(PROVIDER_ID, "accessory", row["provider_record_id"])
                connection.execute(
                    """insert into accessory values (?, ?, ?, ?, ?, ?, ?, ?)
                       on conflict(id) do update set source_record_id=excluded.source_record_id,
                        canonical_name=excluded.canonical_name,accessory_type=excluded.accessory_type""",
                    (accessory_id, PROVIDER_ID, row["provider_record_id"], source_id, name,
                     accessory_type, parsed.get("notice"), verification),
                )
                counters["accessories"] += 1
            connection.execute(
                """insert or replace into provider_entity_mapping values (?, 'sealed_product', ?,
                   'sealed_product', ?, 'direct_official_archive', ?, ?, '{}')""",
                (PROVIDER_ID, row["provider_record_id"], product_id,
                 "verified" if row["status"] == "parsed" else "candidate", source_id),
            )
            if row["status"] != "parsed":
                unresolved_id = stable_id(PROVIDER_ID, "product-detail", row["provider_record_id"])
                gap_status = "documented_exhausted" if row["status"] == "missing_capture" else "open"
                gap_summary = (
                    "Official category identity and artwork are preserved; no public detail capture was indexed"
                    if gap_status == "documented_exhausted" else
                    "Official category inventory is preserved but its detail page is not yet parsed"
                )
                connection.execute(
                    """insert or replace into unresolved_item values (?, 'sealed_product', ?, 'ko', 'KR',
                       'official_product_detail_unavailable', ?, ?, ?, 0)""",
                    (unresolved_id, product_id, gap_summary,
                     canonical_json({"status": row["status"], "error": row["error"], "categories": categories,
                                     "live_url_result": "HTTP 410 across ordinary URL variants",
                                     "archive_result": "no indexed numeric detail capture"}), gap_status),
                )
                counters["detail_gaps"] += 1
            else:
                connection.execute(
                    """update unresolved_item set status='resolved' where entity_type='sealed_product'
                       and entity_id=? and issue_class='official_product_detail_unavailable'""", (product_id,),
                )
            counters["products"] += 1
        connection.execute(
            "update import_run set status='completed',counters_json=?,checkpoint_json=?,completed_at=? where id=?",
            (canonical_json(dict(counters)), canonical_json({"complete": counters["detail_gaps"] == 0}),
             datetime.now(timezone.utc).isoformat(), run_id),
        )
        connection.commit()
        return dict(counters)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
        checkpoint.close()
