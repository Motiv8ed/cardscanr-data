"""Import official Pokemon Asia product-gallery checkpoints into staging."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .pokemon_asia_import import LOCALE_SCOPE
from .schema import connect
from .tcgdex import canonical_json, digest, stable_id


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def import_checkpoint(database: Path, checkpoint_path: Path, locale: str) -> dict[str, int]:
    if locale not in LOCALE_SCOPE:
        raise ValueError(f"Unsupported locale {locale}")
    language, region = LOCALE_SCOPE[locale]
    provider_id = f"pokemon-asia-{locale}-products-official"
    checkpoint = sqlite3.connect(f"file:{checkpoint_path.resolve()}?mode=ro", uri=True)
    checkpoint.row_factory = sqlite3.Row
    if checkpoint.execute("select count(*) from collector_runs where status='running'").fetchone()[0]:
        checkpoint.close()
        raise RuntimeError("Product collector checkpoint still has a running job")
    snapshot_sha = file_sha256(checkpoint_path)
    now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4())
    snapshot_id = stable_id(provider_id, "snapshot", snapshot_sha[:24])
    counters: Counter[str] = Counter()
    staging = connect(str(database))
    try:
        staging.execute(
            """insert into source_provider values (?, ?, 'official', ?, 'metadata_only', ?, ?, null)
               on conflict(id) do update set rights_status=excluded.rights_status,
                 attribution_text=excluded.attribution_text""",
            (provider_id, f"Pokemon Asia official product gallery ({locale})",
             f"https://asia.pokemon-card.com/{locale}/products/",
             "Official Pokemon Card Game trainer site",
             f"https://asia.pokemon-card.com/{locale}/terms/"),
        )
        staging.execute(
            "insert into import_run values (?,?,'running',?,?, '{}','{}',?,null,null)",
            (run_id, provider_id, str(checkpoint_path), snapshot_sha, now),
        )
        staging.execute(
            """insert into source_snapshot values (?,?,?,?,?,null,?,?,?)
               on conflict(id) do update set import_run_id=excluded.import_run_id,
                 fetched_at=excluded.fetched_at""",
            (snapshot_id, provider_id, run_id, str(checkpoint_path), snapshot_sha,
             checkpoint_path.stat().st_size, now, str(checkpoint_path.parent / "raw")),
        )
        for row in checkpoint.execute("select * from products where locale=? order by page_url,ordinal", (locale,)):
            metadata = json.loads(row["metadata_json"] or "{}")
            payload = {
                "page_url": row["page_url"], "ordinal": row["ordinal"],
                "local_name": row["local_name"], "product_type": row["product_type"],
                "image_url": row["image_url"], "metadata": metadata,
                "raw_sha256": row["raw_sha256"],
            }
            source_raw = canonical_json(payload)
            source_id = stable_id(provider_id, "product", row["product_id"], digest(source_raw)[:16])
            staging.execute(
                """insert into source_record values (?,?,?,?, 'product', ?,null,?,?,?,null)
                   on conflict(id) do update set import_run_id=excluded.import_run_id,
                     snapshot_id=excluded.snapshot_id""",
                (source_id, provider_id, run_id, snapshot_id, row["product_id"], row["page_url"],
                 digest(source_raw), source_raw),
            )
            sealed_id = stable_id(provider_id, "sealed", row["product_id"])
            variant_id = stable_id(sealed_id, language, region, "standard")
            staging.execute(
                """insert into sealed_product values (?,?,?,?,?,?, 'verified',?)
                   on conflict(id) do update set source_record_id=excluded.source_record_id,
                     canonical_name=excluded.canonical_name,product_type=excluded.product_type,
                     raw_product_json=excluded.raw_product_json""",
                (sealed_id, provider_id, row["product_id"], source_id, row["local_name"],
                 row["product_type"], source_raw),
            )
            staging.execute(
                """insert into sealed_product_variant values (?,?,?,?,?,'standard',null,?)
                   on conflict(id) do update set local_name=excluded.local_name,
                     attributes_json=excluded.attributes_json""",
                (variant_id, sealed_id, language, region, row["local_name"], canonical_json(metadata)),
            )
            staging.execute("delete from product_content where sealed_product_variant_id=?", (variant_id,))
            for ordinal, description in enumerate(metadata.get("contents") or []):
                staging.execute(
                    "insert into product_content values (?,?,'other',null,?,1,?)",
                    (variant_id, ordinal, str(description), canonical_json({"verbatim_official_description": True})),
                )
                counters["contents"] += 1
            if row["image_url"]:
                image_id = stable_id(variant_id, provider_id, "display", digest(row["image_url"])[:16])
                staging.execute(
                    """insert into product_image_candidate values (?,?,?,?, 'display',?,'link_only','candidate','{}')
                       on conflict(id) do update set source_record_id=excluded.source_record_id,
                         source_url=excluded.source_url,rights_status=excluded.rights_status,
                         validation_status=case when product_image_candidate.validation_status
                           in ('verified','acquired','published','blocked') then product_image_candidate.validation_status
                           else excluded.validation_status end""",
                    (image_id, variant_id, source_id, provider_id, row["image_url"]),
                )
                counters["image_candidates"] += 1
            staging.execute(
                "insert or replace into provider_entity_mapping values (?, 'product', ?, 'sealed_product', ?, "
                "'direct_official_record','verified',?,'{}')",
                (provider_id, row["product_id"], sealed_id, source_id),
            )
            counters["products"] += 1
        parsed_pages = {row[0] for row in checkpoint.execute("select distinct page_url from products where locale=?", (locale,))}
        for row in checkpoint.execute(
            "select page_url,status,error,content_sha256 from pages where locale=? and page_url not like '%/products/'",
            (locale,),
        ):
            unresolved_id = stable_id(provider_id, "page-parse", row["page_url"])
            if row["page_url"] in parsed_pages:
                staging.execute("update unresolved_item set status='resolved' where id=?", (unresolved_id,))
                continue
            staging.execute(
                """insert or replace into unresolved_item values (?, 'sealed_product_page', ?, ?, ?,
                   'official_product_page_unparsed', ?, ?, 'needs_review', 0)""",
                (unresolved_id, row["page_url"], language, region,
                 "Official product-gallery page was preserved but yielded no exact product block",
                 canonical_json({"page_url": row["page_url"], "http_status": row["status"],
                                 "error": row["error"], "content_sha256": row["content_sha256"]})),
            )
            counters["unparsed_pages"] += 1
        staging.execute(
            "update import_run set status='completed',counters_json=?,checkpoint_json=?,completed_at=? where id=?",
            (canonical_json(dict(counters)), canonical_json({"complete": True}),
             datetime.now(timezone.utc).isoformat(), run_id),
        )
        staging.commit()
        return dict(counters)
    except Exception:
        staging.rollback()
        raise
    finally:
        staging.close()
        checkpoint.close()


__all__ = ["import_checkpoint"]
