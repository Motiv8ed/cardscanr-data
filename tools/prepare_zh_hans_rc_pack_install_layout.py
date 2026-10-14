#!/usr/bin/env python3
"""Prepare RC Chinese pack in a QA-only pack identity (never production zh-cn).

Layout mirrors CataloguePackManager:
  <root>/zh-cn-qa/zh-cn-qa.sqlite
  <root>/zh-cn-qa/installed_pack.json

Pack id is intentionally **zh-cn-qa** so sideload cannot overwrite the
production 41,185-record zh-cn / build98 pack directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (
    ROOT
    / "reports"
    / "global_image_acquisition"
    / "pokecardex"
    / "release_candidate"
    / "qa_pack"
    / "zh-cn.sqlite"
)
OUT = (
    ROOT
    / "reports"
    / "global_image_acquisition"
    / "pokecardex"
    / "release_candidate"
    / "pack_install_layout"
)
PACK_ID = "zh-cn-qa"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def gzip_copy(source: Path, destination: Path) -> int:
    compressor = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    written = 0
    with source.open("rb") as src, destination.open("wb") as dst:
        while True:
            chunk = src.read(8 * 1024 * 1024)
            if not chunk:
                break
            out = compressor.compress(chunk)
            if out:
                dst.write(out)
                written += len(out)
        out = compressor.flush()
        if out:
            dst.write(out)
            written += len(out)
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-id", default=PACK_ID)
    args = parser.parse_args()
    pack_id = str(args.pack_id).strip() or PACK_ID
    if pack_id == "zh-cn":
        raise SystemExit(
            "refusing pack id zh-cn — use zh-cn-qa so production build98 is never overwritten"
        )

    if not SRC.exists():
        raise SystemExit(f"missing RC sqlite: {SRC}")
    pack_dir = OUT / pack_id
    pack_dir.mkdir(parents=True, exist_ok=True)
    sqlite_path = pack_dir / f"{pack_id}.sqlite"
    gz_path = pack_dir / f"{pack_id}.sqlite.gz"
    shutil.copy2(SRC, sqlite_path)
    gzip_bytes = gzip_copy(sqlite_path, gz_path)
    raw_sha = sha256_file(sqlite_path)
    gz_sha = sha256_file(gz_path)
    raw_bytes = sqlite_path.stat().st_size
    meta = {
        "packId": pack_id,
        "kind": "language",
        "description": "Simplified Chinese RC metadata pack (QA-only identity)",
        "languages": ["zh-cn"],
        "schemaVersion": "2.2.0",
        "sha256": raw_sha,
        "gzipSha256": gz_sha,
        "rawSqliteBytes": raw_bytes,
        "compressedDownloadBytes": gzip_bytes,
        "installedBytes": raw_bytes,
        "temporaryUpdateSpaceBytes": raw_bytes + gzip_bytes,
        "recordCount": 14923,
        "productCount": 0,
        "databaseUrl": "",
        "compressedDatabaseUrl": "",
        "compression": "gzip",
        "qaOnly": True,
        "productionPromotionAllowed": False,
        "artworkPolicy": "placeholders_only",
        "attributionRequired": "Data courtesy of PokéCardex (https://pokecardex.com)",
        "neverOverwriteProductionPackId": "zh-cn",
        "generatedAtUtc": utc_now(),
    }
    (pack_dir / "installed_pack.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    summary = {
        "generatedAtUtc": utc_now(),
        "layoutRoot": str(OUT),
        "packId": pack_id,
        "sqlitePath": str(sqlite_path),
        "gzipPath": str(gz_path),
        "installedPackJson": str(pack_dir / "installed_pack.json"),
        "rawBytes": raw_bytes,
        "gzipBytes": gzip_bytes,
        "sha256": raw_sha,
        "gzipSha256": gz_sha,
        "recordCount": 14923,
        "note": "QA-only pack identity zh-cn-qa — never install as zh-cn",
    }
    (OUT / "layout_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
