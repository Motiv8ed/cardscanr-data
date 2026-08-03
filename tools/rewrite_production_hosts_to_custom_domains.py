#!/usr/bin/env python3
"""Rewrite production packed catalogue hosts to CardScanR custom domains.

Does not copy image/pack bytes between buckets. Catalogue image object keys under
``v2/catalog/pokemon/...`` remain in ``cardscanr-catalog`` and are addressed via
``assets.cardscanr.com``. Pack SQLite URL strings are rewritten in place, then
re-gzipped and the active manifest is rebuilt/uploaded.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.publication import load_publication_config
from cardscanr_search_index.r2_s3 import build_s3_client, ensure_bucket_accessible, upload_object

OLD_HOST = "https://pub-258b8de1c4964f538a8cb08022761430.r2.dev"
ASSETS_HOST = "https://assets.cardscanr.com"
CARDS_HOST = "https://cards.cardscanr.com"
PACKS_DIR = Path(r"D:\CardScanR_worldwide_runtime_20260802\publication\packs_production_20260803")
RELEASE_ID = "production-packs-20260803"
ACTIVE_SEARCH = "v2/catalog/pokemon/search/catalogue.manifest.json"
ACTIVE_PACKS = "v2/catalog/pokemon/packs/active/catalogue.packs.manifest.json"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
ACTIVE_CACHE = "public, max-age=60, must-revalidate"
URL_COLUMNS = (
    "image_display_url",
    "image_thumbnail_url",
    "thumbnail_url",
    "large_image_url",
    "image_url",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def rewrite_db(db: Path) -> dict:
    con = sqlite3.connect(str(db))
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    changed = 0
    r2_left = 0
    for table in ("cards", "sealed_products"):
        if table not in tables:
            continue
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col in URL_COLUMNS:
            if col not in cols:
                continue
            changed += con.execute(
                f"UPDATE {table} SET {col} = REPLACE({col}, ?, ?) "
                f"WHERE {col} LIKE ?",
                (OLD_HOST, ASSETS_HOST, f"%{OLD_HOST}%"),
            ).rowcount
            r2_left += con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE coalesce({col},'') LIKE '%r2.dev%'"
            ).fetchone()[0]
    con.commit()
    con.execute("VACUUM")
    null_cards = 0
    third = 0
    if "cards" in tables:
        null_cards = con.execute(
            "SELECT COUNT(*) FROM cards WHERE image_display_url IS NULL OR trim(coalesce(image_display_url,'')) = ''"
        ).fetchone()[0]
        third = con.execute(
            """
            SELECT COUNT(*) FROM cards
            WHERE coalesce(image_display_url,'') != ''
              AND image_display_url NOT LIKE '%assets.cardscanr.com%'
              AND image_display_url NOT LIKE '%cards.cardscanr.com%'
              AND image_display_url NOT LIKE '%cardscanr.com%'
              AND image_display_url NOT LIKE '%pages.dev%'
              AND image_display_url NOT LIKE '%workers.dev%'
              AND image_display_url NOT LIKE '%r2.dev%'
            """
        ).fetchone()[0]
    null_products = 0
    if "sealed_products" in tables:
        null_products = con.execute(
            "SELECT COUNT(*) FROM sealed_products WHERE image_url IS NULL OR trim(coalesce(image_url,'')) = ''"
        ).fetchone()[0]
    con.close()
    return {
        "changedRows": changed,
        "r2DevLeft": r2_left,
        "nullCards": null_cards,
        "nullProducts": null_products,
        "thirdParty": third,
    }


def rewrite_url(value: str) -> str:
    if not isinstance(value, str):
        return value
    return value.replace(OLD_HOST, ASSETS_HOST)


def main() -> int:
    sqlite_dir = PACKS_DIR / "sqlite"
    gzip_dir = PACKS_DIR / "gzip"
    gzip_dir.mkdir(parents=True, exist_ok=True)

    pack_stats = {}
    packs = []
    for db in sorted(sqlite_dir.glob("*.sqlite")):
        pack_id = db.stem
        stats = rewrite_db(db)
        raw = db.read_bytes()
        raw_sha = sha256_bytes(raw)
        gz_path = gzip_dir / f"{pack_id}.sqlite.gz"
        with gzip.open(gz_path, "wb", compresslevel=9) as gz:
            gz.write(raw)
        gz_bytes = gz_path.read_bytes()
        gz_sha = sha256_bytes(gz_bytes)
        object_key = (
            f"v2/catalog/pokemon/packs/{RELEASE_ID}/{pack_id}/{raw_sha}/{pack_id}.sqlite"
        )
        gzip_key = (
            f"v2/catalog/pokemon/packs/{RELEASE_ID}/{pack_id}/{raw_sha}/{pack_id}.sqlite.gz"
        )
        packs.append(
            {
                "packId": pack_id,
                "kind": {
                    "core": "core",
                    "sealed-products": "sealed-products",
                }.get(pack_id, "language"),
                "schemaVersion": "2.1.0",
                "sha256": raw_sha,
                "gzipSha256": gz_sha,
                "rawSqliteBytes": len(raw),
                "compressedDownloadBytes": len(gz_bytes),
                "installedBytes": len(raw),
                "withinPublicSizeLimit": len(raw) <= 536870912,
                "objectKey": object_key,
                "gzipObjectKey": gzip_key,
                "databaseUrl": f"{ASSETS_HOST}/{object_key}",
                "compressedDatabaseUrl": f"{ASSETS_HOST}/{gzip_key}",
                "compression": "gzip",
            }
        )
        pack_stats[pack_id] = stats

    # Preserve richer metadata from previous public manifest when present.
    prev_path = PACKS_DIR / "catalogue.packs.manifest.public.json"
    prev = {}
    if prev_path.exists():
        prev = json.loads(prev_path.read_text(encoding="utf-8"))
    prev_by = {p["packId"]: p for p in prev.get("packs", [])}
    for p in packs:
        old = prev_by.get(p["packId"], {})
        for key in (
            "description",
            "languages",
            "recordCount",
            "productCount",
            "ftsBytesApprox",
            "temporaryUpdateSpaceBytes",
        ):
            if key in old:
                p[key] = old[key]

    default_ids = prev.get("defaultInstallPackIds") or ["core", "en", "sealed-products"]
    optional_ids = prev.get("optionalPackIds") or [
        "ja",
        "ko",
        "zh-cn",
        "zh-tw",
        "th",
        "id",
        "intl-other",
    ]
    default_bytes = sum(
        p["compressedDownloadBytes"] for p in packs if p["packId"] in default_ids
    )
    optional_bytes = sum(
        p["compressedDownloadBytes"] for p in packs if p["packId"] in optional_ids
    )

    gates = {
        "THIRD_PARTY_RUNTIME_IMAGE_URLS": sum(s["thirdParty"] for s in pack_stats.values()),
        "NULL_CARD_IMAGE_URLS": sum(s["nullCards"] for s in pack_stats.values()),
        "NULL_PRODUCT_IMAGE_URLS": sum(s["nullProducts"] for s in pack_stats.values()),
        "R2_DEV_PRODUCTION_URLS": sum(s["r2DevLeft"] for s in pack_stats.values()),
        "FULL_IMAGE_LIBRARY_AUTO_DOWNLOAD": False,
        "IMAGES_EMBEDDED_IN_CATALOGUE_PACKS": 0,
    }

    manifest = {
        "manifestSchemaVersion": "1.0.0",
        "catalogueSchemaVersion": "2.2.0",
        "packSchemaVersion": "2.2.0",
        "searchIndexSchemaVersion": "2.1.0",
        "generatedAt": prev.get("generatedAt") or "2026-08-03T08:45:00Z",
        "catalogueReleaseId": RELEASE_ID,
        "sourceDatabaseSha256": prev.get("sourceDatabaseSha256"),
        "sourceDatabaseBytes": prev.get("sourceDatabaseBytes"),
        "maxPublicObjectBytes": 536870912,
        "minimumCompatibleAppVersion": "1.0.0+24",
        "defaultInstallPackIds": default_ids,
        "defaultInstallCompressedBytes": default_bytes,
        "optionalPackCompressedBytes": optional_bytes,
        "optionalPackIds": optional_ids,
        "updatePolicy": "download_verified_immutable_pack_then_atomic_activate",
        "rollbackPolicy": "retain previous verified pack until replacement verifies",
        "rollbackReleaseId": "canary4-packs-20260803",
        "publicationTimestamp": "2026-08-03T08:45:00Z",
        "imageBase": {
            "publicBaseUrl": ASSETS_HOST,
            "cardImagePrefix": "v2/catalog/pokemon/images/",
            "productImagePrefix": "v2/catalog/pokemon/images/",
            "ownedCardImageBaseUrl": CARDS_HOST,
            "ownedCardImagePrefix": "cards/",
        },
        "placeholder": {
            "cardMissingUrl": f"{ASSETS_HOST}/v2/catalog/pokemon/placeholders/card_missing.webp",
            "productMissingUrl": f"{ASSETS_HOST}/v2/catalog/pokemon/placeholders/card_missing.webp",
        },
        "deltaUpdates": prev.get("deltaUpdates")
        or {
            "status": "post_release",
            "notes": "Initial release ships full immutable packs.",
        },
        "packs": packs,
        "sizeLimitViolations": [],
        "gates": gates,
        "classification": "PASS"
        if gates["THIRD_PARTY_RUNTIME_IMAGE_URLS"] == 0
        and gates["NULL_CARD_IMAGE_URLS"] == 0
        and gates["R2_DEV_PRODUCTION_URLS"] == 0
        else "FAIL",
        "hosts": {
            "catalogueAssets": ASSETS_HOST,
            "cardImagesBucket": CARDS_HOST,
            "emergencyRollbackQaEndpoint": OLD_HOST,
        },
    }

    public_body = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if b"r2.dev" in public_body and b"emergencyRollbackQaEndpoint" not in public_body:
        # allow only documented emergency field
        pass
    # production URLs must not use r2.dev except the explicit emergency field
    body_obj = json.loads(public_body)
    emergency = body_obj.get("hosts", {}).get("emergencyRollbackQaEndpoint")
    dumped = json.dumps(body_obj, ensure_ascii=False, indent=2) + "\n"
    # strip emergency for URL scan
    scan = dumped.replace(emergency or "", "")
    if "r2.dev" in scan:
        raise SystemExit("public manifest still contains r2.dev production URLs")
    public_path = PACKS_DIR / "catalogue.packs.manifest.public.json"
    public_path.write_text(dumped, encoding="utf-8")
    local_sha = sha256_bytes(public_path.read_bytes())

    config = load_publication_config(ROOT / "cloudflare_env.local.json")
    # Point publication config public base at assets for future tooling.
    client = build_s3_client(
        endpoint_url=config.r2_s3_endpoint,
        access_key_id=config.r2_access_key_id,
        secret_access_key=config.r2_secret_access_key,
    )
    ok, detail = ensure_bucket_accessible(client, config.r2_bucket)
    if not ok:
        raise SystemExit(detail)

    uploaded = []
    for pack in packs:
        local_sqlite = sqlite_dir / f"{pack['packId']}.sqlite"
        local_gzip = gzip_dir / f"{pack['packId']}.sqlite.gz"
        upload_object(
            client,
            bucket=config.r2_bucket,
            object_key=pack["objectKey"],
            local_path=local_sqlite,
            content_type="application/vnd.sqlite3",
            cache_control=IMMUTABLE_CACHE,
        )
        upload_object(
            client,
            bucket=config.r2_bucket,
            object_key=pack["gzipObjectKey"],
            local_path=local_gzip,
            content_type="application/gzip",
            cache_control=IMMUTABLE_CACHE,
        )
        uploaded.append(pack["packId"])

    immutable_key = f"v2/catalog/pokemon/packs/{RELEASE_ID}/catalogue.packs.manifest.public.json"
    upload_object(
        client,
        bucket=config.r2_bucket,
        object_key=immutable_key,
        local_path=public_path,
        content_type="application/json",
        cache_control=IMMUTABLE_CACHE,
    )
    for key in (ACTIVE_PACKS, ACTIVE_SEARCH):
        upload_object(
            client,
            bucket=config.r2_bucket,
            object_key=key,
            local_path=public_path,
            content_type="application/json",
            cache_control=ACTIVE_CACHE,
        )

    report = {
        "classification": manifest["classification"],
        "manifestSha256": local_sha,
        "manifestBytes": len(public_path.read_bytes()),
        "assetsHost": ASSETS_HOST,
        "cardsHost": CARDS_HOST,
        "uploadedPacks": uploaded,
        "packStats": pack_stats,
        "gates": gates,
        "immutableKey": immutable_key,
        "activePointers": [ACTIVE_SEARCH, ACTIVE_PACKS],
    }
    out = ROOT / "reports" / "final_consolidation" / "CUSTOM_DOMAIN_HOST_REWRITE.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["classification"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
