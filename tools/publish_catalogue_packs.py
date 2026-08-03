#!/usr/bin/env python3
"""Upload immutable catalogue packs + pack manifest. Does not touch the monolith active catalogue pointer."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.publication import load_publication_config
from cardscanr_search_index.r2_s3 import build_s3_client, ensure_bucket_accessible, upload_object

DATABASE_CONTENT_TYPE = "application/vnd.sqlite3"
GZIP_CONTENT_TYPE = "application/gzip"
JSON_CONTENT_TYPE = "application/json"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
ACTIVE_PACK_MANIFEST_CACHE = "public, max-age=60, must-revalidate"
ACTIVE_PACK_MANIFEST_KEY = "v2/catalog/pokemon/packs/active/catalogue.packs.manifest.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish catalogue packs to R2 (no monolith activation).")
    parser.add_argument("--packs-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "cloudflare_env.local.json")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--publish-active-pack-pointer",
        action="store_true",
        help="Also write the packs/active manifest pointer (still not the monolith catalogue.manifest.json).",
    )
    args = parser.parse_args()

    manifest_path = args.packs_dir / "catalogue.packs.manifest.json"
    if not manifest_path.exists():
        parser.error(f"missing {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = load_publication_config(args.config)
    if not config.r2_s3_endpoint or not config.r2_access_key_id or not config.r2_secret_access_key:
        raise SystemExit("R2 S3 credentials missing from config")
    client = build_s3_client(
        endpoint_url=config.r2_s3_endpoint,
        access_key_id=config.r2_access_key_id,
        secret_access_key=config.r2_secret_access_key,
    )
    ok, detail = ensure_bucket_accessible(client, config.r2_bucket)
    if not ok:
        raise SystemExit(detail)

    uploaded: list[dict[str, object]] = []
    for pack in manifest["packs"]:
        sqlite_path = Path(pack["sqlitePath"])
        gzip_path = Path(pack["gzipPath"])
        if not sqlite_path.exists() or not gzip_path.exists():
            raise SystemExit(f"missing pack files for {pack['packId']}")
        upload_object(
            client,
            bucket=config.r2_bucket,
            object_key=pack["objectKey"],
            local_path=sqlite_path,
            content_type=DATABASE_CONTENT_TYPE,
            cache_control=IMMUTABLE_CACHE,
        )
        upload_object(
            client,
            bucket=config.r2_bucket,
            object_key=pack["gzipObjectKey"],
            local_path=gzip_path,
            content_type=GZIP_CONTENT_TYPE,
            cache_control=IMMUTABLE_CACHE,
        )
        uploaded.append(
            {
                "packId": pack["packId"],
                "objectKey": pack["objectKey"],
                "gzipObjectKey": pack["gzipObjectKey"],
                "sha256": pack["sha256"],
                "gzipSha256": pack["gzipSha256"],
                "rawSqliteBytes": pack["rawSqliteBytes"],
                "compressedDownloadBytes": pack["compressedDownloadBytes"],
            }
        )

    release_id = manifest["catalogueReleaseId"]
    immutable_manifest_key = f"v2/catalog/pokemon/packs/{release_id}/catalogue.packs.manifest.json"
    upload_object(
        client,
        bucket=config.r2_bucket,
        object_key=immutable_manifest_key,
        local_path=manifest_path,
        content_type=JSON_CONTENT_TYPE,
        cache_control=IMMUTABLE_CACHE,
    )

    active_published = False
    if args.publish_active_pack_pointer:
        upload_object(
            client,
            bucket=config.r2_bucket,
            object_key=ACTIVE_PACK_MANIFEST_KEY,
            local_path=manifest_path,
            content_type=JSON_CONTENT_TYPE,
            cache_control=ACTIVE_PACK_MANIFEST_CACHE,
        )
        active_published = True

    report = {
        "classification": "PASS",
        "bucket": config.r2_bucket,
        "publicBaseUrl": config.r2_public_base_url,
        "immutablePackManifestKey": immutable_manifest_key,
        "activePackManifestKey": ACTIVE_PACK_MANIFEST_KEY if active_published else None,
        "monolithActiveManifestUntouched": True,
        "defaultInstallCompressedBytes": manifest.get("defaultInstallCompressedBytes"),
        "packsUploaded": uploaded,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
