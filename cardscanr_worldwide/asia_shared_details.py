"""Hydrate identical English Asia inventories from one completed official checkpoint."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .tcgdex import canonical_json

LOCALE_LANGUAGE = {"ph": "en", "sg": "en", "my": "en"}


def _manifest(connection: sqlite3.Connection, query: str, params: tuple[str, ...]) -> tuple[list[str], str]:
    rows = connection.execute(query, params).fetchall()
    values = ["\t".join(str(value or "") for value in row) for row in rows]
    return [str(row[0]) for row in rows], hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()


def _replace_locale(value: str | None, source: str, target: str) -> str | None:
    return value.replace(f"/{source}/", f"/{target}/") if value else value


def hydrate_shared_details(source_checkpoint: Path, target_checkpoint: Path, source_locale: str, target_locale: str) -> dict[str, object]:
    if source_locale == target_locale:
        raise ValueError("Source and target locales must differ")
    if LOCALE_LANGUAGE.get(source_locale) != LOCALE_LANGUAGE.get(target_locale) or not LOCALE_LANGUAGE.get(source_locale):
        raise ValueError("Shared-detail hydration is limited to the verified English PH/SG/MY locale family")
    source = sqlite3.connect(f"file:{source_checkpoint.resolve()}?mode=ro", uri=True)
    target = sqlite3.connect(target_checkpoint)
    now = datetime.now(timezone.utc).isoformat()
    try:
        if source.execute("select count(*) from collector_runs where status='running'").fetchone()[0]:
            raise RuntimeError("Source checkpoint still has a running collector")
        if target.execute("select count(*) from collector_runs where status='running'").fetchone()[0]:
            raise RuntimeError("Target checkpoint still has a running collector")
        source_ids, source_manifest = _manifest(
            source,
            "select card_id,raw_sha256,parsed_json from cards where locale=? and status='parsed' order by card_id",
            (source_locale,),
        )
        target_ids, target_manifest = _manifest(
            target,
            "select distinct card_id,source_url from product_cards where locale=? order by card_id",
            (target_locale,),
        )
        if not source_ids or source_ids != target_ids:
            missing = len(set(target_ids) - set(source_ids))
            extra = len(set(source_ids) - set(target_ids))
            raise RuntimeError(f"Official card-ID inventories differ (missing={missing}, extra={extra})")
        run_id = uuid.uuid4().hex[:24]
        copied = 0
        for row in source.execute(
            "select * from cards where locale=? and status='parsed' order by card_id", (source_locale,)
        ):
            card_id, source_url, local_name, image_url, parsed_json, raw_sha256, _, _ = row[1:]
            parsed = json.loads(parsed_json)
            parsed["page_url"] = _replace_locale(parsed.get("page_url"), source_locale, target_locale)
            parsed["image_url"] = _replace_locale(parsed.get("image_url"), source_locale, target_locale)
            parsed["shared_official_detail_evidence"] = {
                "source_locale": source_locale,
                "target_locale": target_locale,
                "language": LOCALE_LANGUAGE[source_locale],
                "exact_card_id": card_id,
                "source_parsed_manifest_sha256": source_manifest,
                "target_inventory_manifest_sha256": target_manifest,
                "source_raw_sha256": raw_sha256,
                "basis": "identical complete official card-ID inventories in the same English locale family",
            }
            generated_raw = canonical_json(parsed)
            target.execute(
                """insert into cards values (?, ?, ?, ?, ?, ?, ?, 'parsed', ?)
                   on conflict(locale,card_id) do update set source_url=excluded.source_url,
                     local_name=excluded.local_name,image_url=excluded.image_url,
                     parsed_json=excluded.parsed_json,raw_sha256=excluded.raw_sha256,
                     status=excluded.status,updated_at=excluded.updated_at""",
                (
                    target_locale,
                    card_id,
                    _replace_locale(source_url, source_locale, target_locale),
                    local_name,
                    _replace_locale(image_url, source_locale, target_locale),
                    generated_raw,
                    hashlib.sha256(generated_raw.encode("utf-8")).hexdigest(),
                    now,
                ),
            )
            copied += 1
        counters = {
            "copied_cards": copied,
            "source_parsed_manifest_sha256": source_manifest,
            "target_inventory_manifest_sha256": target_manifest,
        }
        target.execute(
            "insert into collector_runs values (?, ?, 'shared_detail_hydration', 'completed', ?, ?, ?, null)",
            (run_id, target_locale, now, now, canonical_json(counters)),
        )
        target.commit()
        return {"source_locale": source_locale, "target_locale": target_locale, **counters}
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()

