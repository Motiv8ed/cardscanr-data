from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .global_builder import SCHEMA_VERSION, verify_global_search_index
from .publication import PublicationConfig
from .r2_s3 import build_s3_client, ensure_bucket_accessible, object_matches, upload_object


R2_PREFIX = "v2/catalog/pokemon/search"
ACTIVE_MANIFEST_KEY = f"{R2_PREFIX}/catalogue.manifest.json"
DATABASE_CONTENT_TYPE = "application/vnd.sqlite3"
DATABASE_CACHE_CONTROL = "public, max-age=31536000, immutable"
IMMUTABLE_MANIFEST_CACHE_CONTROL = "public, max-age=31536000, immutable"
ACTIVE_MANIFEST_CACHE_CONTROL = "public, max-age=60, must-revalidate"
MINIMUM_COMPATIBLE_APP_VERSION = "1.0.0+24"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def immutable_database_key(digest: str) -> str:
    _validate_digest(digest)
    return f"{R2_PREFIX}/versions/{digest}/catalogue.sqlite"


def immutable_manifest_key(digest: str) -> str:
    _validate_digest(digest)
    return f"{R2_PREFIX}/versions/{digest}/manifest.json"


def rollback_manifest_key(digest: str) -> str:
    _validate_digest(digest)
    return f"{R2_PREFIX}/rollbacks/{digest}/manifest.json"


def _validate_digest(digest: str) -> None:
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("digest must be a lowercase SHA-256")


def public_url(base_url: str, object_key: str) -> str:
    if not base_url.startswith("https://"):
        raise ValueError("public R2 base URL must use HTTPS")
    return f"{base_url.rstrip('/')}/{object_key.lstrip('/')}"


def inspect_database(database_path: Path) -> dict[str, Any]:
    verification = verify_global_search_index(database_path)
    with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True) as connection:
        product_counts = {
            f"{language or 'und'}:{region}": count
            for language, region, count in connection.execute(
                "SELECT language,region,COUNT(*) FROM sealed_products GROUP BY language,region ORDER BY language,region"
            )
        }
        card_image_count = int(
            connection.execute("SELECT COUNT(*) FROM cards WHERE image_display_url IS NOT NULL").fetchone()[0]
        )
        product_image_count = int(
            connection.execute("SELECT COUNT(*) FROM sealed_products WHERE image_url IS NOT NULL").fetchone()[0]
        )
    return {
        **verification,
        "sha256": sha256_file(database_path),
        "byteSize": database_path.stat().st_size,
        "perLanguageRegionProductCounts": product_counts,
        "cardImages": card_image_count,
        "productImages": product_image_count,
    }


def build_manifest(
    *,
    database_path: Path,
    database_summary: Mapping[str, Any],
    r2_public_base_url: str,
    generated_at: str,
    previous_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    digest = str(database_summary["sha256"])
    database_key = immutable_database_key(digest)
    previous_url = None
    previous_sha = None
    if previous_manifest:
        previous_url = previous_manifest.get("databaseUrl")
        previous_sha = previous_manifest.get("sha256")
    return {
        "catalogueSchemaVersion": "2.2.0",
        "searchIndexSchemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "databaseFilename": database_path.name,
        "databaseUrl": public_url(r2_public_base_url, database_key),
        "sha256": digest,
        "byteSize": int(database_summary["byteSize"]),
        "supportedLanguages": sorted(database_summary["perLanguageCounts"]),
        "totalCardCount": int(database_summary["records"]),
        "perLanguageCounts": dict(database_summary["perLanguageCounts"]),
        "totalSealedProductCount": int(database_summary["products"]),
        "totalSealedProductContentCount": int(database_summary["productContents"]),
        "perLanguageRegionProductCounts": dict(database_summary["perLanguageRegionProductCounts"]),
        "cardImageCount": int(database_summary["cardImages"]),
        "productImageCount": int(database_summary["productImages"]),
        "minimumCompatibleAppVersion": MINIMUM_COMPATIBLE_APP_VERSION,
        "previousDatabaseUrl": previous_url,
        "previousSha256": previous_sha,
        "updatePolicy": "download_verified_immutable_r2_object_then_atomic_activate",
        "rollbackPolicy": "reactivate the retained previous immutable manifest and database",
        "production": True,
    }


def load_remote_manifest(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "CardScanRPublication/2.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise RuntimeError(f"manifest_http_status:{response.status}")
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("databaseUrl") or not payload.get("sha256"):
        raise RuntimeError("invalid_previous_manifest")
    return payload


def verify_public_database(
    url: str,
    *,
    expected_sha256: str,
    expected_size: int,
    full_download: bool,
) -> dict[str, Any]:
    issues: list[str] = []
    headers: dict[str, str] = {}
    head = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "CardScanRPublication/2.1"})
    with urllib.request.urlopen(head, timeout=60) as response:
        headers = {key.lower(): value for key, value in response.headers.items()}
        if response.status != 200:
            issues.append(f"headStatus:{response.status}")
    if int(headers.get("content-length") or 0) != expected_size:
        issues.append("contentLengthMismatch")
    if "sqlite" not in headers.get("content-type", "").lower():
        issues.append("contentTypeMismatch")
    if "immutable" not in headers.get("cache-control", "").lower():
        issues.append("cacheControlMissingImmutable")

    range_request = urllib.request.Request(
        url,
        headers={"User-Agent": "CardScanRPublication/2.1", "Range": "bytes=0-99"},
    )
    with urllib.request.urlopen(range_request, timeout=60) as response:
        first_bytes = response.read()
        range_status = response.status
    if range_status != 206:
        issues.append(f"rangeStatus:{range_status}")
    if not first_bytes.startswith(b"SQLite format 3\x00"):
        issues.append("sqliteMagicMismatch")

    downloaded_sha256 = None
    if full_download:
        digest = hashlib.sha256()
        byte_count = 0
        request = urllib.request.Request(url, headers={"User-Agent": "CardScanRPublication/2.1"})
        with urllib.request.urlopen(request, timeout=1800) as response:
            while True:
                chunk = response.read(8 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                byte_count += len(chunk)
        downloaded_sha256 = digest.hexdigest()
        if byte_count != expected_size:
            issues.append("fullDownloadSizeMismatch")
        if downloaded_sha256 != expected_sha256:
            issues.append("fullDownloadSha256Mismatch")
    return {
        "classification": "PASS" if not issues else "FAIL",
        "issues": issues,
        "headStatus": 200 if not any(item.startswith("headStatus") for item in issues) else None,
        "rangeStatus": range_status,
        "downloadedSha256": downloaded_sha256,
    }


@dataclass(frozen=True)
class PublishResult:
    classification: str
    manifest: dict[str, Any]
    database_key: str
    manifest_key: str
    database_upload: str
    manifest_upload: str
    public_verification: dict[str, Any]
    previous_manifest: dict[str, Any] | None = None
    active_manifest_key: str | None = None
    rollback_manifest_key: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "manifest": self.manifest,
            "databaseKey": self.database_key,
            "manifestKey": self.manifest_key,
            "databaseUpload": self.database_upload,
            "manifestUpload": self.manifest_upload,
            "publicVerification": self.public_verification,
            "activeManifestKey": self.active_manifest_key,
            "rollbackManifestKey": self.rollback_manifest_key,
        }


def publish_version(
    *,
    database_path: Path,
    config: PublicationConfig,
    previous_manifest: Mapping[str, Any] | None,
    full_public_download: bool,
) -> tuple[PublishResult, Any]:
    summary = inspect_database(database_path)
    if summary["classification"] != "PASS":
        raise RuntimeError(f"local_database_verification_failed:{summary['issues']}")
    if not config.r2_public_base_url:
        raise RuntimeError("r2_public_base_url_unconfigured")
    client = build_s3_client(
        endpoint_url=config.r2_s3_endpoint or "",
        access_key_id=config.r2_access_key_id or "",
        secret_access_key=config.r2_secret_access_key or "",
    )
    accessible, access_result = ensure_bucket_accessible(client, config.r2_bucket)
    if not accessible:
        raise RuntimeError(access_result)
    digest = str(summary["sha256"])
    database_key = immutable_database_key(digest)
    matches, match_result = object_matches(
        client,
        bucket=config.r2_bucket,
        object_key=database_key,
        expected_sha256=digest,
        expected_size=int(summary["byteSize"]),
    )
    if matches:
        database_upload = f"idempotent:{match_result}"
    else:
        upload_object(
            client,
            bucket=config.r2_bucket,
            object_key=database_key,
            local_path=database_path,
            content_type=DATABASE_CONTENT_TYPE,
            cache_control=DATABASE_CACHE_CONTROL,
        )
        verified, verification_result = object_matches(
            client,
            bucket=config.r2_bucket,
            object_key=database_key,
            expected_sha256=digest,
            expected_size=int(summary["byteSize"]),
        )
        if not verified:
            raise RuntimeError(f"r2_database_verification_failed:{verification_result}")
        database_upload = f"uploaded:{verification_result}"

    manifest = build_manifest(
        database_path=database_path,
        database_summary=summary,
        r2_public_base_url=config.r2_public_base_url,
        generated_at=utc_now(),
        previous_manifest=previous_manifest,
    )
    manifest_key = immutable_manifest_key(digest)
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    client.put_object(
        Bucket=config.r2_bucket,
        Key=manifest_key,
        Body=manifest_bytes,
        ContentType="application/json; charset=utf-8",
        CacheControl=IMMUTABLE_MANIFEST_CACHE_CONTROL,
    )
    stored_manifest = client.get_object(Bucket=config.r2_bucket, Key=manifest_key)["Body"].read()
    if stored_manifest != manifest_bytes:
        raise RuntimeError("immutable_manifest_verification_failed")
    public_verification = verify_public_database(
        manifest["databaseUrl"],
        expected_sha256=digest,
        expected_size=int(summary["byteSize"]),
        full_download=full_public_download,
    )
    classification = "PASS" if public_verification["classification"] == "PASS" else "FAIL"
    return PublishResult(
        classification=classification,
        manifest=manifest,
        database_key=database_key,
        manifest_key=manifest_key,
        database_upload=database_upload,
        manifest_upload="uploaded_and_verified",
        public_verification=public_verification,
        previous_manifest=dict(previous_manifest) if previous_manifest else None,
    ), client


def activate_version(
    *,
    published: PublishResult,
    client: Any,
    bucket: str,
) -> PublishResult:
    if published.classification != "PASS":
        raise RuntimeError("activation_blocked_by_failed_canary")
    previous_body: bytes | None = None
    previous_digest: str | None = None
    try:
        previous_body = client.get_object(Bucket=bucket, Key=ACTIVE_MANIFEST_KEY)["Body"].read()
        previous_payload = json.loads(previous_body.decode("utf-8"))
        previous_digest = str(previous_payload.get("sha256") or "")
        _validate_digest(previous_digest)
    except client.exceptions.NoSuchKey:
        previous_body = None
    if previous_body is None and published.previous_manifest:
        previous_body = (
            json.dumps(published.previous_manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        previous_digest = str(published.previous_manifest.get("sha256") or "")
        _validate_digest(previous_digest)
    retained_key = None
    if previous_body is not None and previous_digest is not None:
        retained_key = rollback_manifest_key(previous_digest)
        client.put_object(
            Bucket=bucket,
            Key=retained_key,
            Body=previous_body,
            ContentType="application/json; charset=utf-8",
            CacheControl=IMMUTABLE_MANIFEST_CACHE_CONTROL,
        )
        retained = client.get_object(Bucket=bucket, Key=retained_key)["Body"].read()
        if retained != previous_body:
            raise RuntimeError("rollback_manifest_verification_failed")
    active_bytes = (json.dumps(published.manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    client.put_object(
        Bucket=bucket,
        Key=ACTIVE_MANIFEST_KEY,
        Body=active_bytes,
        ContentType="application/json; charset=utf-8",
        CacheControl=ACTIVE_MANIFEST_CACHE_CONTROL,
    )
    activated = client.get_object(Bucket=bucket, Key=ACTIVE_MANIFEST_KEY)["Body"].read()
    if activated != active_bytes:
        raise RuntimeError("active_manifest_verification_failed")
    return PublishResult(
        classification="PASS",
        manifest=published.manifest,
        database_key=published.database_key,
        manifest_key=published.manifest_key,
        database_upload=published.database_upload,
        manifest_upload=published.manifest_upload,
        public_verification=published.public_verification,
        previous_manifest=published.previous_manifest,
        active_manifest_key=ACTIVE_MANIFEST_KEY,
        rollback_manifest_key=retained_key,
    )
