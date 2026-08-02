"""Import exact Pokellector gap evidence without granting mirroring rights."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .schema import connect
from .tcgdex import canonical_json, digest, stable_id


PROVIDER_ID = "pokellector-english-gap-evidence"


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""): hasher.update(chunk)
    return hasher.hexdigest()


def import_checkpoint(database: Path, checkpoint_path: Path) -> dict[str, int]:
    source = sqlite3.connect(f"file:{checkpoint_path.resolve()}?mode=ro", uri=True); source.row_factory = sqlite3.Row
    if source.execute("select count(*) from runs where status='running'").fetchone()[0]:
        source.close(); raise RuntimeError("collector checkpoint still has a running job")
    staging = connect(str(database)); counters: Counter[str] = Counter()
    checksum = file_sha256(checkpoint_path); now = datetime.now(timezone.utc).isoformat()
    run_id = str(uuid.uuid4()); snapshot_id = stable_id(PROVIDER_ID,"snapshot",checksum[:24])
    try:
        with staging:
            staging.execute("""insert into source_provider values (?,?,'community_corroboration',?,'permission_pending',?,?,null)
                on conflict(id) do update set rights_status=excluded.rights_status""",
                (PROVIDER_ID,"Pokellector exact English gap evidence","https://www.pokellector.com",
                 "Pokellector; exact set and card pages retained as gap evidence","https://www.pokellector.com/"))
            staging.execute("insert into import_run values (?,?, 'running',?,?, '{}','{}',?,null,null)",
                            (run_id,PROVIDER_ID,str(checkpoint_path),checksum,now))
            staging.execute("""insert into source_snapshot values (?,?,?,?,?,null,?,?,?)
                on conflict(id) do update set import_run_id=excluded.import_run_id,fetched_at=excluded.fetched_at""",
                (snapshot_id,PROVIDER_ID,run_id,str(checkpoint_path),checksum,checkpoint_path.stat().st_size,now,str(checkpoint_path.parent / "raw")))
            for row in source.execute("select * from evidence where status='resolved' order by target_variant_id"):
                variant = staging.execute("""select cv.id,cv.card_printing_id from card_variant cv
                                               where cv.id=?""",(row["target_variant_id"],)).fetchall()
                if len(variant) != 1 or variant[0]["card_printing_id"] != row["target_printing_id"]:
                    raise ValueError(f"target identity missing or changed: {row['target_variant_id']}")
                payload = {key: row[key] for key in row.keys() if key not in {"status","error","updated_at"}}
                raw = canonical_json(payload); record_id = stable_id(PROVIDER_ID,row["target_variant_id"],digest(raw)[:16])
                staging.execute("""insert into source_record values (?,?,?,?, 'card_image_evidence',?,null,?,?,?,null)
                    on conflict(id) do update set import_run_id=excluded.import_run_id,snapshot_id=excluded.snapshot_id""",
                    (record_id,PROVIDER_ID,run_id,snapshot_id,row["target_variant_id"],row["page_url"],digest(raw),raw))
                staging.execute("""insert or replace into provider_entity_mapping values
                    (?, 'card_image_evidence', ?, 'card_variant', ?, 'exact_set_collector_and_page_title', 'verified', ?, ?)""",
                    (PROVIDER_ID,row["target_variant_id"],row["target_variant_id"],record_id,
                     canonical_json({"set_id":row["set_id"],"set_name":row["set_name"],
                                     "collector_number":row["collector_number"],"card_name":row["card_name"],
                                     "page_url":row["page_url"]})))
                candidate_id = stable_id(row["target_variant_id"],PROVIDER_ID,"display",digest(row["image_url"])[:16])
                staging.execute("""insert into card_image_candidate values (?,?,?,?, 'display',?,'permission_pending','candidate')
                    on conflict(id) do update set source_url=excluded.source_url,rights_status=excluded.rights_status""",
                    (candidate_id,row["target_variant_id"],record_id,PROVIDER_ID,row["image_url"]))
                counters["candidates"] += 1
            staging.execute("update import_run set status='completed',counters_json=?,checkpoint_json='{""complete"":true}',completed_at=? where id=?",
                            (canonical_json(dict(counters)),now,run_id))
        return dict(counters)
    finally:
        staging.close(); source.close()
