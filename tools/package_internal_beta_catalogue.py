from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.r2_s3 import (
    build_s3_client,
    ensure_bucket_accessible,
    object_matches,
    upload_object,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deterministic_gzip(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    with source.open("rb") as input_handle, temporary.open("wb") as raw_output:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=raw_output,
            mtime=0,
        ) as output_handle:
            shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
    temporary.replace(destination)


def main() -> int:
    database = (
        ROOT
        / "reports"
        / "global_rollout"
        / "artifacts"
        / "global_catalogue_canary_v2.sqlite"
    )
    index_report = json.loads(
        (ROOT / "reports" / "global_rollout" / "global_search_index.json").read_text(
            encoding="utf-8"
        )
    )
    config = json.loads(
        (ROOT / "cloudflare_env.local.json").read_text(encoding="utf-8-sig")
    )
    previous = json.loads(
        (
            ROOT
            / "public"
            / "v1"
            / "catalog"
            / "pokemon"
            / "search"
            / "catalog_search_v1.manifest.json"
        ).read_text(encoding="utf-8")
    )
    digest = sha256_file(database)
    if digest != index_report["sha256"]:
        raise RuntimeError("global catalogue checksum does not match build report")

    artifact_dir = ROOT / "reports" / "global_catalogue_qa" / "artifacts"
    compressed = artifact_dir / "global_catalogue_v2.sqlite.gz"
    deterministic_gzip(database, compressed)
    compressed_digest = sha256_file(compressed)
    deterministic_copy = artifact_dir / "global_catalogue_v2.repeat.sqlite.gz"
    deterministic_gzip(database, deterministic_copy)
    deterministic = compressed.read_bytes() == deterministic_copy.read_bytes()
    deterministic_copy.unlink()
    if not deterministic:
        raise RuntimeError("gzip transport is not deterministic")

    database_size = database.stat().st_size
    compressed_size = compressed.stat().st_size
    prefix = f"v2/internal-beta/catalog/pokemon/search/{digest[:16]}"
    database_key = f"{prefix}/global_catalogue_v2.sqlite"
    compressed_key = f"{prefix}/global_catalogue_v2.sqlite.gz"
    public_base = str(
        config.get("r2PublicDevUrl") or config.get("r2PublicBaseUrl") or ""
    ).rstrip("/")
    endpoint = str(config.get("r2S3Endpoint") or config.get("r2Endpoint") or "")
    bucket = str(config["r2Bucket"])
    client = build_s3_client(
        endpoint_url=endpoint,
        access_key_id=str(config["r2AccessKeyId"]),
        secret_access_key=str(config["r2SecretAccessKey"]),
    )
    accessible, reason = ensure_bucket_accessible(client, bucket)
    if not accessible:
        raise RuntimeError(reason)

    uploads = []
    for local_path, key, expected_sha, content_type, encoding in (
        (database, database_key, digest, "application/vnd.sqlite3", None),
        (
            compressed,
            compressed_key,
            compressed_digest,
            "application/gzip",
            None,
        ),
    ):
        matches, _ = object_matches(
            client,
            bucket=bucket,
            object_key=key,
            expected_sha256=expected_sha,
            expected_size=local_path.stat().st_size,
        )
        if not matches:
            upload_object(
                client,
                bucket=bucket,
                object_key=key,
                local_path=local_path,
                content_type=content_type,
                cache_control="public, max-age=31536000, immutable",
            )
        verified, detail = object_matches(
            client,
            bucket=bucket,
            object_key=key,
            expected_sha256=expected_sha,
            expected_size=local_path.stat().st_size,
        )
        if not verified:
            raise RuntimeError(detail)
        uploads.append({"key": key, "uploaded": not matches, "verified": verified})

    manifest_relative = Path(
        "v2/internal-beta/catalog/pokemon/search/catalog_search_v2.manifest.json"
    )
    manifest_path = ROOT / "public" / manifest_relative
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    pages_base = str(config.get("pagesBaseUrl") or "").rstrip("/")
    manifest = {
        "catalogueSchemaVersion": "1.0.0",
        "searchIndexSchemaVersion": "2.0.0",
        "catalogueVersion": digest[:16],
        "generatedAt": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "generatorVersion": "internal-beta-v2",
        "databaseFilename": "global_catalogue_v2.sqlite",
        "databaseUrl": f"{public_base}/{database_key}",
        "compressedDatabaseUrl": f"{public_base}/{compressed_key}",
        "compression": "gzip",
        "sha256": digest,
        "compressedSha256": compressed_digest,
        "byteSize": database_size,
        "compressedByteSize": compressed_size,
        "contentFingerprint": digest,
        "supportedLanguages": sorted(index_report["perLanguageCounts"]),
        "totalCardCount": index_report["records"],
        "totalSetCount": 1495,
        "perLanguageCounts": index_report["perLanguageCounts"],
        "minimumCompatibleAppVersion": "1.0.0+24",
        "minimumCompatibleAppVersionStatus": "internal_beta",
        "previousManifestVersion": previous.get("contentFingerprint")
        or previous.get("sha256"),
        "previousDatabaseUrl": previous.get("databaseUrl"),
        "previousSha256": previous.get("sha256"),
        "rollbackManifestUrl": f"{pages_base}/v1/catalog/pokemon/search/catalog_search_v1.manifest.json",
        "immutableDatabaseUrl": f"{public_base}/{database_key}",
        "updatePolicy": "download_compressed_verify_extract_verify_atomic_replace",
        "rollbackPolicy": "retain_previous_sqlite_and_manifest_until_v2_activation_succeeds",
        "production": False,
        "releaseChannel": "internal_beta",
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = {
        "classification": "PASS",
        "manifestPath": str(manifest_path),
        "manifestUrl": f"{pages_base}/{manifest_relative.as_posix()}",
        "databaseSha256": digest,
        "databaseSizeBytes": database_size,
        "compressedSha256": compressed_digest,
        "compressedSizeBytes": compressed_size,
        "compressionRatio": round(compressed_size / database_size, 6),
        "deterministicCompression": deterministic,
        "uploads": uploads,
        "productionManifestReplaced": False,
        "providerImageWrites": 0,
    }
    report_path = ROOT / "reports" / "global_catalogue_qa" / "packaging.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
