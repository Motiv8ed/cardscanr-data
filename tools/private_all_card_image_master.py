#!/usr/bin/env python3
"""Build a private all-card master SQLite index from eight SHA-pinned R2 packs.

This indexes EXISTING card/set metadata and image URL fields; it does not fetch
card artwork or modify any source, website, customer account or R2 object.
A verified 2,286-row image-source overlay ZIP can be merged later, locally.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from tools.preflight_multilingual_crossrelease import (
    ACTIVE_PACKS, ARCHIVE_PACKS, ACTIVE, CANARY, MAX_GZIP,
    get, inspect_pack,
)
from tools.preflight_legacy_korean_pack import pick
from tools.private_artwork_takedown_controls import _safe_image_base

EXPECTED_CARD_ROWS = {
    "en": 20710, "ja": 28453, "ko": 4646,
    "zh-tw": 36148, "th": 12548, "id": 15113, "zh-cn": 41185,
}
EXPECTED_TOTAL = sum(EXPECTED_CARD_ROWS.values())
MAX_OVERLAY_BYTES = 20_000_000

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE cards (
  canonical_printing_id TEXT PRIMARY KEY,
  canonical_base_id TEXT NOT NULL,
  pack_id TEXT NOT NULL,
  manifest_origin TEXT NOT NULL,
  language TEXT NOT NULL,
  set_id TEXT NOT NULL,
  printed_collector_number TEXT,
  native_card_name TEXT,
  english_card_name TEXT,
  native_set_name TEXT,
  rarity TEXT,
  release_date TEXT,
  provider_card_id TEXT,
  provider_set_id TEXT,
  mirror_permission_status TEXT,
  original_thumbnail_url TEXT,
  original_display_url TEXT,
  source_metadata_json TEXT NOT NULL
);
CREATE INDEX idx_cards_language_set ON cards(language, set_id);
CREATE INDEX idx_cards_collector ON cards(language, printed_collector_number);
CREATE TABLE sets (
  pack_id TEXT NOT NULL,
  set_id TEXT NOT NULL,
  language TEXT,
  native_set_name TEXT,
  release_date TEXT,
  source_metadata_json TEXT NOT NULL,
  PRIMARY KEY(pack_id, set_id)
);
CREATE TABLE image_sources (
  canonical_printing_id TEXT NOT NULL,
  image_url TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  image_host TEXT,
  rights_status TEXT NOT NULL DEFAULT 'unverified',
  reference_status TEXT NOT NULL DEFAULT 'unverified',
  local_download_status TEXT NOT NULL DEFAULT 'not_downloaded',
  local_relpath TEXT,
  local_sha256 TEXT,
  last_http_status INTEGER,
  provenance_sha256 TEXT,
  PRIMARY KEY(canonical_printing_id, image_url),
  FOREIGN KEY(canonical_printing_id) REFERENCES cards(canonical_printing_id)
);
CREATE INDEX idx_images_url ON image_sources(image_url);
CREATE INDEX idx_images_rights_download ON image_sources(rights_status,local_download_status);
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE source_pack_receipts (
  pack_id TEXT PRIMARY KEY,
  manifest_origin TEXT,
  pack_sha256 TEXT,
  gzip_sha256 TEXT,
  card_rows INTEGER,
  set_rows INTEGER
);
"""

_CARD_FIELDS = (
    "canonical_printing_id", "canonical_base_id", "language", "set_id",
    "printed_collector_number", "native_card_name", "english_card_name",
    "native_set_name", "rarity", "release_date", "provider_card_id",
    "provider_set_id", "mirror_permission_status",
    "image_thumbnail_url", "image_display_url",
)


def _text(value) -> str | None:
    return str(value) if value is not None else None


def _url_host(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme == "https" and not parsed.username and not parsed.password:
            return parsed.hostname
    except (TypeError, ValueError):
        return None
    return None


def _image_ref(db: sqlite3.Connection, cid: str, url: str | None, kind: str,
               provenance_sha256: str | None = None) -> bool:
    if not url or not isinstance(url, str) or len(url) > 4000:
        return False
    result = db.execute(
        "INSERT OR IGNORE INTO image_sources "
        "(canonical_printing_id,image_url,source_kind,image_host,provenance_sha256) "
        "VALUES (?,?,?,?,?)",
        (cid, url, kind, _url_host(url), provenance_sha256),
    )
    return result.rowcount == 1


def ingest_pack(source_db: sqlite3.Connection, target: sqlite3.Connection,
                *, pack_id: str, manifest_origin: str) -> dict:
    """Stream an immutable pack into the local-only master index."""
    source_db.row_factory = sqlite3.Row
    cards = 0
    original_cards_with_ref = 0
    original_urls = 0
    for raw in source_db.execute("SELECT * FROM cards"):
        value = dict(raw)
        cid = _text(value.get("canonical_printing_id"))
        base = _text(value.get("canonical_base_id"))
        lang = _text(value.get("language"))
        set_id = _text(value.get("set_id"))
        if not cid or not base or not set_id or not lang:
            raise ValueError("incomplete_card_identity:" + pack_id)
        if pack_id != "core" and lang != pack_id:
            raise ValueError("card_language_drift:" + pack_id)
        thumb, display = value.get("image_thumbnail_url"), value.get("image_display_url")
        record = (
            cid, base, pack_id, manifest_origin, lang, set_id,
            *(_text(value.get(k)) for k in _CARD_FIELDS[4:13]),
            _text(thumb), _text(display),
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",",":")),
        )
        try:
            target.execute(
                "INSERT INTO cards (canonical_printing_id,canonical_base_id,pack_id,"
                "manifest_origin,language,set_id,printed_collector_number,native_card_name,"
                "english_card_name,native_set_name,rarity,release_date,provider_card_id,"
                "provider_set_id,mirror_permission_status,original_thumbnail_url,"
                "original_display_url,source_metadata_json) VALUES (" + ",".join("?"*18) + ")",
                record,
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate_canonical_printing_across_packs:" + cid) from exc
        cards += 1
        original_cards_with_ref += int(bool(thumb or display))
        original_urls += int(_image_ref(target,cid,thumb,"original_thumbnail"))
        original_urls += int(_image_ref(target,cid,display,"original_display"))

    sets = 0
    for raw in source_db.execute("SELECT * FROM sets"):
        v = dict(raw)
        sid = _text(v.get("set_id"))
        if not sid:
            raise ValueError("empty_source_set_id:" + pack_id)
        target.execute(
            "INSERT INTO sets(pack_id,set_id,language,native_set_name,release_date,source_metadata_json) "
            "VALUES (?,?,?,?,?,?)",
            (pack_id,sid,_text(v.get("language")),_text(v.get("native_set_name")),
             _text(v.get("release_date")),
             json.dumps(v,ensure_ascii=False,sort_keys=True,default=str,separators=(",",":"))),
        )
        sets += 1
    return {"cards": cards, "sets": sets,
            "cards_with_existing_image_ref": original_cards_with_ref,
            "existing_unique_source_url_rows": original_urls}


def _immutable_connect(path:Path) -> sqlite3.Connection:
    uri = "file:" + quote(path.resolve().as_posix(), safe="/:") + "?mode=ro&immutable=1"
    db=sqlite3.connect(uri,uri=True)
    db.execute("PRAGMA query_only=ON")
    return db


def build_master(outdir:Path, fetch=get) -> dict:
    if outdir.is_symlink() or (outdir.exists() and (not outdir.is_dir() or any(outdir.iterdir()))):
        raise FileExistsError("output_must_be_new_empty_directory")
    outdir.mkdir(parents=True,exist_ok=True)
    active=json.loads(fetch(ACTIVE,2_000_000))
    archived=json.loads(fetch(CANARY,2_000_000))
    selected=[(p,"active",pick(active,p)) for p in ACTIVE_PACKS] + [
        (p,"archived_canary",pick(archived,p)) for p in ARCHIVE_PACKS
    ]
    dbpath=outdir/"CardScanR_Master_Catalogue.sqlite"
    totals=Counter()
    summary=[]
    with sqlite3.connect(dbpath) as dst, tempfile.TemporaryDirectory(prefix="cardscanr-master-index-") as temp:
        dst.executescript(SCHEMA)
        for pack_id,origin,descriptor in selected:
            zipped=fetch(descriptor["compressedDatabaseUrl"],MAX_GZIP)
            local=Path(temp)/(pack_id+".sqlite")
            inspect_pack(descriptor,zipped,local)
            try:
                with _immutable_connect(local) as src:
                    counts=ingest_pack(src,dst,pack_id=pack_id,manifest_origin=origin)
                if pack_id=="core":
                    if counts["cards"]!=0:
                        raise ValueError("core_pack_unexpected_card_rows")
                elif counts["cards"]!=EXPECTED_CARD_ROWS[pack_id]:
                    raise ValueError("unexpected_pack_card_count:"+pack_id)
                dst.execute(
                    "INSERT INTO source_pack_receipts VALUES (?,?,?,?,?,?)",
                    (pack_id,origin,descriptor["sha256"],descriptor["gzipSha256"],
                     counts["cards"],counts["sets"]),
                )
                dst.commit()
                totals.update(counts)
                summary.append({"pack_id":pack_id,"origin":origin,
                                "pack_sha256":descriptor["sha256"],**counts})
            finally:
                local.unlink(missing_ok=True)
        if totals["cards"]!=EXPECTED_TOTAL:
            raise ValueError("unexpected_total_card_count")
        distinct=dst.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        if distinct!=EXPECTED_TOTAL:
            raise ValueError("canonical_printing_count_mismatch")
        distinct_with_image=dst.execute(
            "SELECT COUNT(DISTINCT canonical_printing_id) FROM image_sources"
        ).fetchone()[0]
        if distinct_with_image != totals["cards_with_existing_image_ref"]:
            raise ValueError("existing_url_coverage_mismatch")
        dst.execute("INSERT INTO metadata VALUES (?,?)",
                    ("classification","PRIVATE_CARD_IMAGE_MASTER_DATABASE"))
        dst.execute("INSERT INTO metadata VALUES (?,?)",("rights_cleared","false"))
        dst.execute("INSERT INTO metadata VALUES (?,?)",("public_publish_enabled","false"))
        dst.commit()
        integrity=dst.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity!="ok":
            raise ValueError("master_sqlite_integrity_failed")
    sha=hashlib.file_digest(dbpath.open("rb"),"sha256").hexdigest()
    receipt={
        "schema_version":1,
        "classification":"PRIVATE_CARD_IMAGE_MASTER_DATABASE",
        "generated_utc":datetime.now(timezone.utc).isoformat(),
        "card_rows":EXPECTED_TOTAL,
        "language_packs":len(EXPECTED_CARD_ROWS),
        "core_plus_language_packs":len(selected),
        "original_cards_with_image_link":totals["cards_with_existing_image_ref"],
        "original_cards_without_image_link":EXPECTED_TOTAL-totals["cards_with_existing_image_ref"],
        "original_image_source_url_records":totals["existing_unique_source_url_rows"],
        "sets_materialized":totals["sets"],
        "packs":summary,
        "master_sqlite_file":dbpath.name,
        "master_sqlite_sha256":sha,
        "master_sqlite_bytes":dbpath.stat().st_size,
        "overlay_image_sources_merged":0,
        "customer_data_included":False,
        "image_binary_files_downloaded":0,
        "external_rights_granted":False,
        "production_writes":0,
        "safe_to_publish":False,
    }
    (outdir/"build_receipt.json").write_text(
        json.dumps(receipt,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8"
    )
    return receipt


def merge_overlay(dbpath:Path, overlay_zip:Path) -> dict:
    """Attach exact 2,286-source registry, without overwriting existing links."""
    with zipfile.ZipFile(overlay_zip) as archive:
        info=archive.getinfo("artwork_source_registry.jsonl")
        if info.file_size>MAX_OVERLAY_BYTES:
            raise ValueError("oversized_overlay_file")
        raw=archive.read(info)
    if len(raw)>MAX_OVERLAY_BYTES:
        raise ValueError("oversized_overlay_body")
    rows=[json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if len(rows)>100000:
        raise ValueError("overlay_count_over_budget")
    added=0
    seen=set()
    with sqlite3.connect(dbpath) as db:
        db.execute("PRAGMA foreign_keys=ON")
        for row in rows:
            cid=row.get("canonical_printing_id")
            url=row.get("provider_low_webp_url")
            base=row.get("source_image_base_url")
            if (not isinstance(cid,str) or not isinstance(base,str) or
                    cid in seen or row.get("identity_match")!="exact_unique" or
                    not _safe_image_base(base) or url!=base+"/low.webp"):
                raise ValueError("overlay_invalid_card_identity_or_url")
            seen.add(cid)
            current=db.execute(
                "SELECT language,set_id,printed_collector_number FROM cards WHERE canonical_printing_id=?",
                (cid,),
            ).fetchone()
            if not current:
                raise ValueError("overlay_printing_not_in_master:"+cid)
            lang,set_id,number=current
            if lang!=row.get("language") or set_id!="tcgdex:asia:set:"+str(row.get("provider_set_id")):
                raise ValueError("overlay_wrong_language_or_set:"+cid)
            added+=int(_image_ref(db,cid,url,"tcgdex_exact_source_candidate",
                                  row.get("source_set_sha256")))
        # Duplicate import is a no-op; never elevates rights or status.
        db.commit()
        total=db.execute("SELECT COUNT(*) FROM image_sources").fetchone()[0]
        with_overlay=db.execute(
            "SELECT COUNT(DISTINCT canonical_printing_id) FROM image_sources "
            "WHERE source_kind='tcgdex_exact_source_candidate'"
        ).fetchone()[0]
    return {"overlay_records_validated":len(rows),
            "new_image_source_rows":added,
            "distinct_printings_with_overlay_candidate":with_overlay,
            "total_source_links_in_master":total,
            "image_binary_files_downloaded":0,
            "rights_granted":False,"public_publish_enabled":False}


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="action",required=True)
    build=sub.add_parser("build")
    build.add_argument("--output",type=Path,required=True)
    overlay=sub.add_parser("merge-overlay")
    overlay.add_argument("--master",type=Path,required=True)
    overlay.add_argument("--overlay-zip",type=Path,required=True)
    args=parser.parse_args()
    result=build_master(args.output) if args.action=="build" else merge_overlay(args.master,args.overlay_zip)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
