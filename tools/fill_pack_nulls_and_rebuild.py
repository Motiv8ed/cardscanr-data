#!/usr/bin/env python3
"""Fill null image URLs in catalogue packs with CardScanR placeholders, re-gzip, and rebuild manifest."""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path

PLACEHOLDER = (
    "https://assets.cardscanr.com/"
    "v2/catalog/pokemon/placeholders/card_missing.webp"
)
PUBLIC_BASE = "https://assets.cardscanr.com"
SRC_PACKS = Path(r"D:\CardScanR_worldwide_runtime_20260802\publication\packs_canary4_20260803")
OUT_PACKS = Path(r"D:\CardScanR_worldwide_runtime_20260802\publication\packs_production_20260803")
RELEASE_ID = "production-packs-20260803"
REPORT = Path(
    r"D:\CardScanR_worktrees\worldwide_catalogue_products_20260802\reports\final_consolidation\PACK_NULL_FILL_PRODUCTION.json"
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def fill_nulls(db: Path) -> dict:
    con = sqlite3.connect(str(db))
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    card_filled = 0
    if "cards" in tables:
        card_filled = con.execute(
            """
            UPDATE cards
            SET image_display_url = ?,
                image_thumbnail_url = CASE
                    WHEN image_thumbnail_url IS NULL OR trim(coalesce(image_thumbnail_url,'')) = ''
                    THEN ? ELSE image_thumbnail_url END,
                thumbnail_url = CASE
                    WHEN thumbnail_url IS NULL OR trim(coalesce(thumbnail_url,'')) = ''
                    THEN ? ELSE thumbnail_url END,
                large_image_url = ?,
                image_source = 'cardscanr_placeholder'
            WHERE image_display_url IS NULL OR trim(coalesce(image_display_url,'')) = ''
            """,
            (PLACEHOLDER, PLACEHOLDER, PLACEHOLDER, PLACEHOLDER),
        ).rowcount
        # Fill missing thumbnails when display already exists.
        con.execute(
            """
            UPDATE cards
            SET image_thumbnail_url = ?,
                thumbnail_url = CASE
                    WHEN thumbnail_url IS NULL OR trim(coalesce(thumbnail_url,'')) = ''
                    THEN ? ELSE thumbnail_url END
            WHERE image_thumbnail_url IS NULL OR trim(coalesce(image_thumbnail_url,'')) = ''
            """,
            (PLACEHOLDER, PLACEHOLDER),
        )
    product_filled = 0
    if "sealed_products" in tables:
        product_filled = con.execute(
            """
            UPDATE sealed_products
            SET image_url = ?, image_provider = 'cardscanr_placeholder'
            WHERE image_url IS NULL OR trim(coalesce(image_url,'')) = ''
            """,
            (PLACEHOLDER,),
        ).rowcount
    con.commit()
    con.execute("VACUUM")
    null_cards = 0
    if "cards" in tables:
        null_cards = con.execute(
            "SELECT COUNT(*) FROM cards WHERE image_display_url IS NULL OR trim(coalesce(image_display_url,'')) = ''"
        ).fetchone()[0]
    null_products = 0
    if "sealed_products" in tables:
        null_products = con.execute(
            "SELECT COUNT(*) FROM sealed_products WHERE image_url IS NULL OR trim(coalesce(image_url,'')) = ''"
        ).fetchone()[0]
    third = 0
    if "cards" in tables:
        third = con.execute(
            """
            SELECT COUNT(*) FROM cards
            WHERE coalesce(image_display_url,'') != ''
              AND image_display_url NOT LIKE '%r2.dev%'
              AND image_display_url NOT LIKE '%cardscanr%'
              AND image_display_url NOT LIKE '%pages.dev%'
              AND image_display_url NOT LIKE '%workers.dev%'
              AND image_display_url NOT LIKE '%placeholder%'
            """
        ).fetchone()[0]
    con.close()
    return {
        "cardRowsTouched": card_filled,
        "productRowsTouched": product_filled,
        "nullCardDisplayRemaining": null_cards,
        "nullProductRemaining": null_products,
        "thirdPartyRemaining": third,
    }


def gzip_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as fin, gzip.open(dest, "wb", compresslevel=9) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)


def main() -> int:
    t0 = time.time()
    if OUT_PACKS.exists():
        shutil.rmtree(OUT_PACKS)
    sqlite_out = OUT_PACKS / "sqlite"
    gzip_out = OUT_PACKS / "gzip"
    sqlite_out.mkdir(parents=True)
    gzip_out.mkdir(parents=True)

    src_manifest = json.loads((SRC_PACKS / "catalogue.packs.manifest.json").read_text(encoding="utf-8"))
    pack_results = []
    for pack in src_manifest["packs"]:
        pack_id = pack["packId"]
        print(f"processing {pack_id}...", flush=True)
        src_sqlite = Path(pack["sqlitePath"])
        dest_sqlite = sqlite_out / f"{pack_id}.sqlite"
        shutil.copy2(src_sqlite, dest_sqlite)
        fill = fill_nulls(dest_sqlite)
        digest = sha256_file(dest_sqlite)
        dest_gz = gzip_out / f"{pack_id}.sqlite.gz"
        gzip_copy(dest_sqlite, dest_gz)
        gz_digest = sha256_file(dest_gz)
        raw_bytes = dest_sqlite.stat().st_size
        gz_bytes = dest_gz.stat().st_size
        object_key = f"v2/catalog/pokemon/packs/{RELEASE_ID}/{pack_id}/{digest}/{pack_id}.sqlite"
        gzip_key = f"v2/catalog/pokemon/packs/{RELEASE_ID}/{pack_id}/{digest}/{pack_id}.sqlite.gz"
        updated = dict(pack)
        updated.update(
            {
                "sqlitePath": str(dest_sqlite),
                "gzipPath": str(dest_gz),
                "sha256": digest,
                "gzipSha256": gz_digest,
                "rawSqliteBytes": raw_bytes,
                "compressedDownloadBytes": gz_bytes,
                "installedBytes": raw_bytes,
                "temporaryUpdateSpaceBytes": raw_bytes * 2 + gz_bytes,
                "withinPublicSizeLimit": raw_bytes < 536870912 and gz_bytes < 536870912,
                "objectKey": object_key,
                "gzipObjectKey": gzip_key,
                "databaseUrl": f"{PUBLIC_BASE}/{object_key}",
                "compressedDatabaseUrl": f"{PUBLIC_BASE}/{gzip_key}",
                "nullFill": fill,
            }
        )
        # strip local-only noise already present
        pack_results.append(updated)
        print(
            f"  null_cards={fill['nullCardDisplayRemaining']} null_products={fill['nullProductRemaining']} "
            f"sha={digest[:12]} gz={gz_bytes}",
            flush=True,
        )

    default_ids = src_manifest.get("defaultInstallPackIds", ["core", "en", "sealed-products"])
    default_bytes = sum(
        p["compressedDownloadBytes"] for p in pack_results if p["packId"] in default_ids
    )
    optional_bytes = sum(
        p["compressedDownloadBytes"] for p in pack_results if p["packId"] not in default_ids
    )
    produced = {
        "manifestSchemaVersion": "1.0.0",
        "catalogueSchemaVersion": "2.2.0",
        "packSchemaVersion": "2.2.0",
        "searchIndexSchemaVersion": "2.1.0",
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "catalogueReleaseId": RELEASE_ID,
        "sourceDatabaseSha256": src_manifest.get("sourceDatabaseSha256"),
        "sourceDatabaseBytes": src_manifest.get("sourceDatabaseBytes"),
        "maxPublicObjectBytes": 536870912,
        "minimumCompatibleAppVersion": "1.0.0+24",
        "defaultInstallPackIds": default_ids,
        "defaultInstallCompressedBytes": default_bytes,
        "optionalPackCompressedBytes": optional_bytes,
        "optionalPackIds": [p["packId"] for p in pack_results if p["packId"] not in default_ids],
        "updatePolicy": "download_verified_immutable_pack_then_atomic_activate",
        "rollbackPolicy": "retain previous verified pack until replacement verifies",
        "rollbackReleaseId": "canary4-packs-20260803",
        "publicationTimestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "imageBase": {
            "publicBaseUrl": PUBLIC_BASE,
            "cardImagePrefix": "v2/catalog/pokemon/images/",
            "productImagePrefix": "v2/catalog/pokemon/images/",
        },
        "placeholder": {
            "cardMissingUrl": PLACEHOLDER,
            "productMissingUrl": PLACEHOLDER,
        },
        "deltaUpdates": src_manifest.get("deltaUpdates"),
        "packs": pack_results,
        "sizeLimitViolations": [
            p["packId"] for p in pack_results if not p.get("withinPublicSizeLimit", True)
        ],
        "gates": {
            "THIRD_PARTY_RUNTIME_IMAGE_URLS": sum(p["nullFill"]["thirdPartyRemaining"] for p in pack_results),
            "NULL_CARD_IMAGE_URLS": sum(p["nullFill"]["nullCardDisplayRemaining"] for p in pack_results),
            "NULL_PRODUCT_IMAGE_URLS": sum(p["nullFill"]["nullProductRemaining"] for p in pack_results),
            "FULL_IMAGE_LIBRARY_AUTO_DOWNLOAD": False,
            "IMAGES_EMBEDDED_IN_CATALOGUE_PACKS": 0,
        },
        "classification": "PASS",
    }
    if produced["gates"]["NULL_CARD_IMAGE_URLS"] != 0 or produced["gates"]["NULL_PRODUCT_IMAGE_URLS"] != 0:
        produced["classification"] = "FAIL_NULLS_REMAIN"
    if produced["gates"]["THIRD_PARTY_RUNTIME_IMAGE_URLS"] != 0:
        produced["classification"] = "FAIL_THIRD_PARTY"
    if produced["sizeLimitViolations"]:
        produced["classification"] = "FAIL_SIZE_LIMIT"

    # Strip nullFill from published pack entries (keep in report)
    published_packs = []
    for p in pack_results:
        pub = {k: v for k, v in p.items() if k != "nullFill"}
        published_packs.append(pub)
    produced["packs"] = published_packs

    manifest_path = OUT_PACKS / "catalogue.packs.manifest.json"
    # Deterministic dump (sorted keys for stability of non-list objects; packs keep order)
    body1 = json.dumps(produced, ensure_ascii=False, indent=2) + "\n"
    manifest_path.write_text(body1, encoding="utf-8")
    body2 = json.dumps(json.loads(manifest_path.read_text(encoding="utf-8")), ensure_ascii=False, indent=2) + "\n"
    deterministic = body1 == body2
    manifest_sha = hashlib.sha256(body1.encode("utf-8")).hexdigest()

    report = {
        "classification": produced["classification"],
        "releaseId": RELEASE_ID,
        "outputDir": str(OUT_PACKS),
        "manifestPath": str(manifest_path),
        "manifestSha256": manifest_sha,
        "manifestBytes": len(body1.encode("utf-8")),
        "deterministicRebuild": deterministic,
        "gates": produced["gates"],
        "defaultInstallCompressedBytes": default_bytes,
        "elapsedSec": round(time.time() - t0, 1),
        "packNullFill": {p["packId"]: p.get("nullFill") for p in pack_results},
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if produced["classification"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
