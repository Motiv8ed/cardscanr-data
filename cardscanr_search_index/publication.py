from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .builder import sha256_file
from .constants import (
    CATALOGUE_SCHEMA_VERSION,
    DATABASE_BASENAME,
    MANIFEST_BASENAME,
    PREVIOUS_DATABASE_BASENAME,
    SEARCH_INDEX_SCHEMA_VERSION,
    SEARCH_OUTPUT_DIR,
    SHA256_BASENAME,
    SUPPORTED_LANGUAGES,
)
from .verify import REQUIRED_INDEXES, REQUIRED_TABLES, verify_search_index
from .r2_s3 import build_s3_client, ensure_bucket_accessible, object_matches, upload_object

PAGES_MAX_ASSET_BYTES = 25 * 1024 * 1024
MINIMUM_COMPATIBLE_APP_VERSION = "1.0.0+21"
R2_CONTENT_TYPE = "application/vnd.sqlite3"
R2_CACHE_CONTROL = "public, max-age=31536000, immutable"
MANIFEST_CACHE_CONTROL = "public, max-age=300, must-revalidate"
DEFAULT_R2_BUCKET = "cardscanr-catalog"
DEFAULT_PAGES_BASE_URL = "https://cardscanr-cache.pages.dev"
R2_OBJECT_PREFIX = "v1/catalog/pokemon/search"
MANIFEST_ROLLBACK_BASENAME = "catalog_search_v1.manifest.previous.json"
INTEGRITY_REPORT_BASENAME = "catalog_search_v1.integrity.json"
EXPECTED_TOTAL_CARDS = 74578
EXPECTED_LANGUAGE_COUNTS = {"en": 46417, "jp": 28161}

SECRET_CONFIG_KEYS = frozenset(
    {
        "cloudflareApiToken",
        "r2AccessKeyId",
        "r2SecretAccessKey",
        "apiToken",
        "accessKeyId",
        "secretAccessKey",
    }
)


@dataclass(frozen=True)
class PublicationConfig:
    account_id: str | None
    r2_bucket: str
    r2_public_base_url: str | None
    pages_base_url: str
    r2_s3_endpoint: str | None = None
    cloudflare_api_token: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None

    def redacted(self) -> dict[str, Any]:
        return {
            "accountId": self.account_id,
            "r2Bucket": self.r2_bucket,
            "r2S3Endpoint": self.r2_s3_endpoint,
            "r2PublicBaseUrl": self.r2_public_base_url,
            "pagesBaseUrl": self.pages_base_url,
            "cloudflareApiToken": "configured" if self.cloudflare_api_token else None,
            "r2AccessKeyId": "configured" if self.r2_access_key_id else None,
            "r2SecretAccessKey": "configured" if self.r2_secret_access_key else None,
        }


@dataclass
class LocalDatabaseVerification:
    database_path: Path
    sha256: str
    byte_size: int
    total_cards: int
    per_language_counts: dict[str, int]
    passed: bool
    issues: list[str] = field(default_factory=list)


@dataclass
class RemoteObjectVerification:
    url: str
    status_code: int | None
    content_length: int | None
    content_type: str | None
    cache_control: str | None
    etag: str | None
    range_supported: bool | None
    downloaded_sha256: str | None
    sqlite_health_passed: bool
    issues: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues


@dataclass
class PublicationReport:
    classification: str
    r2_bucket: str | None = None
    public_read_policy_result: str | None = None
    immutable_current_object_key: str | None = None
    immutable_previous_object_key: str | None = None
    pages_manifest_url: str | None = None
    r2_database_url: str | None = None
    sha256: str | None = None
    byte_size: int | None = None
    r2_content_type: str | None = None
    r2_cache_control: str | None = None
    range_request_result: str | None = None
    complete_download_checksum_result: str | None = None
    sqlite_health_result: str | None = None
    pages_deployment_result: str | None = None
    tests_result: str | None = None
    rollback_result: str | None = None
    files_added_or_changed: list[str] = field(default_factory=list)
    unresolved_issues: list[str] = field(default_factory=list)
    flutter_modified: bool = False
    full_image_import_run: bool = False
    dry_run: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "r2Bucket": self.r2_bucket,
            "publicReadPolicyResult": self.public_read_policy_result,
            "immutableCurrentObjectKey": self.immutable_current_object_key,
            "immutablePreviousObjectKey": self.immutable_previous_object_key,
            "pagesManifestUrl": self.pages_manifest_url,
            "r2DatabaseUrl": self.r2_database_url,
            "sha256": self.sha256,
            "byteSize": self.byte_size,
            "r2ContentType": self.r2_content_type,
            "r2CacheControl": self.r2_cache_control,
            "rangeRequestResult": self.range_request_result,
            "completeDownloadChecksumResult": self.complete_download_checksum_result,
            "sqliteHealthResult": self.sqlite_health_result,
            "pagesDeploymentResult": self.pages_deployment_result,
            "testsResult": self.tests_result,
            "rollbackResult": self.rollback_result,
            "filesAddedOrChanged": self.files_added_or_changed,
            "unresolvedIssues": self.unresolved_issues,
            "flutterModified": self.flutter_modified,
            "fullImageImportRun": self.full_image_import_run,
            "dryRun": self.dry_run,
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def immutable_database_filename(sha256: str) -> str:
    digest = str(sha256 or "").strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("sha256 must be a 64-character lowercase hex digest")
    return f"catalog_search_v1.{digest}.sqlite"


def r2_object_key(filename: str) -> str:
    return f"{R2_OBJECT_PREFIX}/{filename}"


def r2_public_url(base_url: str, object_key: str) -> str:
    return f"{base_url.rstrip('/')}/{object_key.lstrip('/')}"


def pages_manifest_url(pages_base_url: str) -> str:
    return f"{pages_base_url.rstrip('/')}/v1/catalog/pokemon/search/{MANIFEST_BASENAME}"


def pages_sha256_url(pages_base_url: str) -> str:
    return f"{pages_base_url.rstrip('/')}/v1/catalog/pokemon/search/{SHA256_BASENAME}"


def _is_s3_endpoint(url: str | None) -> bool:
    return bool(url and "r2.cloudflarestorage.com" in url)


def _is_public_http_base_url(url: str | None) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return lowered.startswith("https://") and not _is_s3_endpoint(url)


def _normalize_config_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    account_id = _optional_str(payload.get("accountId"))
    r2_bucket = str(payload.get("r2Bucket") or DEFAULT_R2_BUCKET)
    pages_base_url = str(payload.get("pagesBaseUrl") or DEFAULT_PAGES_BASE_URL)

    r2_s3_endpoint = _optional_str(payload.get("r2S3Endpoint"))
    public_candidate = _optional_str(payload.get("r2PublicDevUrl")) or _optional_str(payload.get("r2PublicBaseUrl"))

    if not r2_s3_endpoint and account_id:
        r2_s3_endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
    if _is_s3_endpoint(public_candidate) and not payload.get("r2S3Endpoint"):
        r2_s3_endpoint = public_candidate
        public_candidate = _optional_str(payload.get("r2PublicDevUrl"))

    r2_public_base_url = public_candidate if _is_public_http_base_url(public_candidate) else None

    return {
        "account_id": account_id,
        "r2_bucket": r2_bucket,
        "r2_public_base_url": r2_public_base_url,
        "pages_base_url": pages_base_url,
        "r2_s3_endpoint": r2_s3_endpoint,
        "cloudflare_api_token": _optional_str(payload.get("cloudflareApiToken")),
        "r2_access_key_id": _optional_str(payload.get("r2AccessKeyId")),
        "r2_secret_access_key": _optional_str(payload.get("r2SecretAccessKey")),
    }


def load_publication_config(path: Path) -> PublicationConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid config: {path}")
    normalized = _normalize_config_payload(payload)
    return PublicationConfig(**normalized)


def resolve_publication_config(
    *,
    config_path: Path | None = None,
    root: Path | None = None,
) -> PublicationConfig:
    root = root or Path(__file__).resolve().parent.parent
    candidates = [config_path] if config_path else []
    candidates.extend(
        [
            root / "cloudflare_env.local.json",
            root / "cloudflare_env.json",
        ]
    )
    for candidate in candidates:
        if candidate and candidate.exists():
            return load_publication_config(candidate)

    return PublicationConfig(
        account_id=os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
        r2_bucket=os.environ.get("CARDSCANR_R2_BUCKET", DEFAULT_R2_BUCKET),
        r2_public_base_url=os.environ.get("CARDSCANR_R2_PUBLIC_BASE_URL"),
        pages_base_url=os.environ.get("CARDSCANR_PAGES_BASE_URL", DEFAULT_PAGES_BASE_URL),
        r2_s3_endpoint=os.environ.get("CARDSCANR_R2_S3_ENDPOINT"),
        cloudflare_api_token=os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN"),
        r2_access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
        r2_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"),
    )


def verify_local_database(database_path: Path) -> LocalDatabaseVerification:
    issues: list[str] = []
    if not database_path.exists():
        return LocalDatabaseVerification(
            database_path=database_path,
            sha256="",
            byte_size=0,
            total_cards=0,
            per_language_counts={},
            passed=False,
            issues=["database_missing"],
        )

    digest = sha256_file(database_path)
    byte_size = database_path.stat().st_size
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        for table in REQUIRED_TABLES:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not row:
                issues.append(f"missing_table:{table}")
        fts_row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cards_fts'"
        ).fetchone()
        if not fts_row:
            issues.append("missing_fts_table")
        for index in REQUIRED_INDEXES:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                (index,),
            ).fetchone()
            if not row:
                issues.append(f"missing_index:{index}")
        total_cards = int(conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
        per_language = {
            str(row["language"]): int(row["count"])
            for row in conn.execute("SELECT language, COUNT(*) AS count FROM cards GROUP BY language")
        }
    finally:
        conn.close()

    if total_cards != EXPECTED_TOTAL_CARDS:
        issues.append(f"total_cards_mismatch expected={EXPECTED_TOTAL_CARDS} actual={total_cards}")
    for language, expected in EXPECTED_LANGUAGE_COUNTS.items():
        if per_language.get(language, 0) != expected:
            issues.append(f"language_count_mismatch:{language}")

    return LocalDatabaseVerification(
        database_path=database_path,
        sha256=digest,
        byte_size=byte_size,
        total_cards=total_cards,
        per_language_counts=per_language,
        passed=not issues,
        issues=issues,
    )


def build_runtime_manifest(
    *,
    current_sha256: str,
    current_byte_size: int,
    current_database_url: str,
    current_database_filename: str,
    generated_at: str,
    previous_database_url: str | None,
    previous_sha256: str | None,
    total_card_count: int,
    per_language_counts: Mapping[str, int],
) -> dict[str, Any]:
    if not MINIMUM_COMPATIBLE_APP_VERSION:
        raise ValueError("minimumCompatibleAppVersion must be set")
    return {
        "searchIndexSchemaVersion": SEARCH_INDEX_SCHEMA_VERSION,
        "catalogueSchemaVersion": CATALOGUE_SCHEMA_VERSION,
        "generatedAt": generated_at,
        "databaseFilename": current_database_filename,
        "databaseUrl": current_database_url,
        "sha256": current_sha256,
        "byteSize": current_byte_size,
        "supportedLanguages": list(SUPPORTED_LANGUAGES),
        "totalCardCount": total_card_count,
        "perLanguageCounts": dict(per_language_counts),
        "minimumCompatibleAppVersion": MINIMUM_COMPATIBLE_APP_VERSION,
        "previousDatabaseUrl": previous_database_url,
        "previousSha256": previous_sha256,
        "updatePolicy": "download_verified_immutable_r2_object_then_atomic_activate",
        "rollbackPolicy": (
            "if current database activation fails checksum or schema validation, "
            "download previousDatabaseUrl and activate that immutable object"
        ),
    }


def build_integrity_report(source_manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "generatedAt": source_manifest.get("generatedAt"),
        "contentFingerprint": source_manifest.get("contentFingerprint"),
        "generatorVersion": source_manifest.get("generatorVersion"),
        "gitCommit": source_manifest.get("gitCommit"),
        "sourceCatalogueHashes": source_manifest.get("sourceCatalogueHashes") or {},
    }


def find_oversized_pages_assets(
    public_dir: Path,
    *,
    max_bytes: int = PAGES_MAX_ASSET_BYTES,
) -> list[tuple[Path, int]]:
    oversized: list[tuple[Path, int]] = []
    if not public_dir.exists():
        return oversized
    for path in public_dir.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > max_bytes:
            oversized.append((path, size))
    return sorted(oversized, key=lambda item: item[1], reverse=True)


def _git_tracked_files(root: Path, public_dir: Path) -> set[Path]:
    proc = subprocess.run(
        ["git", "ls-files", "--", "public"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    tracked: set[Path] = set()
    if proc.returncode != 0:
        return tracked
    for line in (proc.stdout or "").splitlines():
        rel = line.strip()
        if rel:
            tracked.add((root / rel).resolve())
    return tracked


def assert_pages_publish_safe(public_dir: Path, *, root: Path | None = None) -> list[str]:
    issues: list[str] = []
    root = root or public_dir.parent
    tracked_files = _git_tracked_files(root, public_dir)
    search_dir = public_dir / "v1" / "catalog" / "pokemon" / "search"
    blocked_names = {
        DATABASE_BASENAME,
        PREVIOUS_DATABASE_BASENAME,
        f"{DATABASE_BASENAME}-wal",
        f"{DATABASE_BASENAME}-shm",
        f"{PREVIOUS_DATABASE_BASENAME}-wal",
        f"{PREVIOUS_DATABASE_BASENAME}-shm",
    }
    for name in blocked_names:
        path = (search_dir / name).resolve()
        if path.exists() and path in tracked_files:
            issues.append(f"blocked_pages_asset_tracked:{path.relative_to(public_dir).as_posix()}")

    for path in sorted(tracked_files):
        if not path.is_file() or not path.is_relative_to(search_dir):
            continue
        size = path.stat().st_size
        rel = path.relative_to(public_dir).as_posix()
        if size > PAGES_MAX_ASSET_BYTES:
            issues.append(f"oversized_tracked_search_index_asset:{rel}:{size}")
    return issues


def fetch_managed_public_domain(config: PublicationConfig) -> str | None:
    if not config.cloudflare_api_token or not config.account_id:
        return None
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{config.account_id}"
        f"/r2/buckets/{config.r2_bucket}/domains/managed"
    )
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {config.cloudflare_api_token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None
    domain = result.get("domain")
    enabled = result.get("enabled")
    if enabled and isinstance(domain, str) and domain.strip():
        return f"https://{domain.strip()}"
    return None


def resolve_public_base_url(config: PublicationConfig) -> str | None:
    if _is_public_http_base_url(config.r2_public_base_url):
        return config.r2_public_base_url
    return fetch_managed_public_domain(config)


def probe_public_object(
    public_base_url: str,
    object_key: str,
) -> tuple[bool, str]:
    url = r2_public_url(public_base_url, object_key)
    try:
        status_code, _, _ = _http_request(url, method="HEAD")
    except Exception as exc:
        return False, f"public_probe_failed:{exc}"
    if status_code == 200:
        return True, "public_read_ok"
    return False, f"public_probe_status:{status_code}"


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _build_config_s3_client(config: PublicationConfig) -> Any:
    if not config.r2_s3_endpoint:
        raise ValueError("r2_s3_endpoint_unconfigured")
    if not config.r2_access_key_id or not config.r2_secret_access_key:
        raise ValueError("r2_s3_credentials_missing")
    return build_s3_client(
        endpoint_url=config.r2_s3_endpoint,
        access_key_id=config.r2_access_key_id,
        secret_access_key=config.r2_secret_access_key,
    )


def ensure_r2_bucket(config: PublicationConfig, *, root: Path, dry_run: bool) -> tuple[bool, str]:
    if dry_run:
        if not config.r2_s3_endpoint or not config.r2_access_key_id or not config.r2_secret_access_key:
            return False, "r2_s3_credentials_unconfigured"
        return True, f"dry_run_would_verify_bucket:{config.r2_bucket}"
    client = _build_config_s3_client(config)
    return ensure_bucket_accessible(client, config.r2_bucket)


def upload_r2_object(
    *,
    config: PublicationConfig,
    local_path: Path,
    object_key: str,
    root: Path,
    dry_run: bool,
    expected_sha256: str,
    expected_size: int,
) -> tuple[bool, str]:
    if dry_run:
        return True, f"dry_run_would_upload:{object_key}"

    client = _build_config_s3_client(config)
    existing_ok, existing_message = _r2_object_matches(
        config=config,
        object_key=object_key,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        client=client,
    )
    if existing_ok:
        return True, f"idempotent_existing_object:{existing_message}"

    upload_object(
        client,
        bucket=config.r2_bucket,
        object_key=object_key,
        local_path=local_path,
        content_type=R2_CONTENT_TYPE,
        cache_control=R2_CACHE_CONTROL,
    )

    verified, verify_message = _r2_object_matches(
        config=config,
        object_key=object_key,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        client=client,
    )
    if not verified:
        return False, f"post_upload_verification_failed:{verify_message}"
    return True, verify_message


def _r2_object_matches(
    *,
    config: PublicationConfig,
    object_key: str,
    expected_sha256: str,
    expected_size: int,
    client: Any | None = None,
    root: Path | None = None,
) -> tuple[bool, str]:
    client = client or _build_config_s3_client(config)
    return object_matches(
        client,
        bucket=config.r2_bucket,
        object_key=object_key,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
    )


DEFAULT_HTTP_USER_AGENT = "CardScanRPublicationVerify/1.0"


def _http_request(url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    merged_headers = {"User-Agent": DEFAULT_HTTP_USER_AGENT}
    if headers:
        merged_headers.update(dict(headers))
    request = urllib.request.Request(url, method=method, headers=merged_headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read() if method == "GET" else b""
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            return response.status, response_headers, body
    except urllib.error.HTTPError as exc:
        body = exc.read()
        response_headers = {key.lower(): value for key, value in exc.headers.items()}
        return exc.code, response_headers, body


def verify_remote_sqlite(
    url: str,
    *,
    expected_sha256: str,
    expected_size: int,
) -> RemoteObjectVerification:
    issues: list[str] = []
    status_code = None
    content_length = None
    content_type = None
    cache_control = None
    etag = None
    range_supported = None
    downloaded_sha256 = None
    sqlite_health_passed = False

    try:
        status_code, headers, _ = _http_request(url, method="HEAD")
    except Exception as exc:
        return RemoteObjectVerification(
            url=url,
            status_code=None,
            content_length=None,
            content_type=None,
            cache_control=None,
            etag=None,
            range_supported=None,
            downloaded_sha256=None,
            sqlite_health_passed=False,
            issues=[f"head_request_failed:{exc}"],
        )

    if status_code != 200:
        issues.append(f"head_status:{status_code}")
    content_length = _parse_int_header(headers.get("content-length"))
    content_type = headers.get("content-type")
    cache_control = headers.get("cache-control")
    etag = headers.get("etag")
    if content_length != expected_size:
        issues.append(f"content_length_mismatch expected={expected_size} actual={content_length}")
    if content_type and R2_CONTENT_TYPE not in content_type and "octet-stream" not in content_type:
        issues.append(f"content_type_mismatch:{content_type}")
    if cache_control and "immutable" not in cache_control:
        issues.append(f"cache_control_missing_immutable:{cache_control}")

    try:
        range_status, range_headers, range_body = _http_request(
            url,
            method="GET",
            headers={"Range": "bytes=0-1023"},
        )
        if range_status in (206, 200) and range_body:
            range_supported = range_status == 206 or len(range_body) < expected_size
            if range_status not in (206, 200):
                issues.append(f"range_request_failed:{range_status}")
        else:
            range_supported = False
            issues.append(f"range_request_failed:{range_status}")
    except Exception as exc:
        range_supported = False
        issues.append(f"range_request_error:{exc}")

    try:
        _, _, body = _http_request(url, method="GET")
        downloaded_sha256 = hashlib.sha256(body).hexdigest()
        if downloaded_sha256 != expected_sha256:
            issues.append("downloaded_sha256_mismatch")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite") as tmp:
            tmp.write(body)
            tmp_path = Path(tmp.name)
        try:
            health = verify_local_database(tmp_path)
            sqlite_health_passed = health.passed
            issues.extend(health.issues)
        finally:
            tmp_path.unlink(missing_ok=True)
    except Exception as exc:
        issues.append(f"download_failed:{exc}")

    return RemoteObjectVerification(
        url=url,
        status_code=status_code,
        content_length=content_length,
        content_type=content_type,
        cache_control=cache_control,
        etag=etag,
        range_supported=range_supported,
        downloaded_sha256=downloaded_sha256,
        sqlite_health_passed=sqlite_health_passed,
        issues=issues,
    )


def verify_manifest_url(url: str, *, expected_manifest: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    try:
        status_code, headers, body = _http_request(url, method="GET")
    except Exception as exc:
        return [f"manifest_request_failed:{exc}"]
    if status_code != 200:
        issues.append(f"manifest_status:{status_code}")
    content_type = headers.get("content-type") or ""
    if "json" not in content_type:
        issues.append(f"manifest_content_type:{content_type}")
    cache_control = headers.get("cache-control") or ""
    if "max-age=300" not in cache_control:
        issues.append(f"manifest_cache_control:{cache_control}")
    if headers.get("access-control-allow-origin") != "*":
        issues.append("manifest_cors_missing")
    if not headers.get("etag"):
        issues.append("manifest_etag_missing")
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return issues + ["manifest_invalid_json"]
    for key in (
        "searchIndexSchemaVersion",
        "catalogueSchemaVersion",
        "generatedAt",
        "databaseUrl",
        "databaseFilename",
        "sha256",
        "byteSize",
        "supportedLanguages",
        "minimumCompatibleAppVersion",
        "previousDatabaseUrl",
        "previousSha256",
        "updatePolicy",
        "rollbackPolicy",
    ):
        if key not in payload:
            issues.append(f"manifest_missing_field:{key}")
    if payload.get("minimumCompatibleAppVersion") != MINIMUM_COMPATIBLE_APP_VERSION:
        issues.append("manifest_minimum_compatible_version_mismatch")
    if not str(payload.get("databaseUrl") or "").startswith("http"):
        issues.append("manifest_database_url_not_r2")
    if expected_manifest.get("sha256") and payload.get("sha256") != expected_manifest.get("sha256"):
        issues.append("manifest_sha256_mismatch")
    return issues


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def preserve_manifest_rollback(output_dir: Path) -> Path | None:
    manifest_path = output_dir / MANIFEST_BASENAME
    rollback_path = output_dir / MANIFEST_ROLLBACK_BASENAME
    if not manifest_path.exists():
        return None
    shutil.copy2(manifest_path, rollback_path)
    return rollback_path


def publish_search_index(
    *,
    output_dir: Path = SEARCH_OUTPUT_DIR,
    public_dir: Path | None = None,
    config: PublicationConfig | None = None,
    root: Path | None = None,
    dry_run: bool = True,
    skip_tests: bool = False,
    skip_live_verification: bool = False,
) -> PublicationReport:
    root = root or Path(__file__).resolve().parent.parent
    public_dir = public_dir or (root / "public")
    output_dir = output_dir.resolve()
    config = config or resolve_publication_config(root=root)
    report = PublicationReport(classification="FAIL", dry_run=dry_run, r2_bucket=config.r2_bucket)
    issues: list[str] = []

    if not skip_tests:
        proc = subprocess.run(
            [os.environ.get("PYTHON", "python"), "-m", "unittest", "tests.test_search_index_publication"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        report.tests_result = "passed" if proc.returncode == 0 else "failed"
        if proc.returncode != 0:
            issues.append("publication_tests_failed")
            issues.append((proc.stdout or proc.stderr or "")[-1000:])
    else:
        report.tests_result = "skipped"

    database_path = output_dir / DATABASE_BASENAME
    previous_database_path = output_dir / PREVIOUS_DATABASE_BASENAME
    local = verify_local_database(database_path)
    report.sha256 = local.sha256
    report.byte_size = local.byte_size
    if not local.passed:
        issues.extend(local.issues)

    sidecar_path = output_dir / SHA256_BASENAME
    if sidecar_path.exists() and sidecar_path.read_text(encoding="utf-8").strip() != local.sha256:
        issues.append("sha256_sidecar_mismatch")

    pages_issues = assert_pages_publish_safe(public_dir, root=root)
    issues.extend(pages_issues)

    public_base_url = resolve_public_base_url(config)
    if not public_base_url:
        issues.append("r2_public_base_url_unconfigured")

    current_filename = immutable_database_filename(local.sha256)
    current_object_key = r2_object_key(current_filename)
    report.immutable_current_object_key = current_object_key

    previous_sha256 = None
    previous_object_key = None
    previous_database_url = None
    if previous_database_path.exists():
        previous_sha256 = sha256_file(previous_database_path)
        previous_filename = immutable_database_filename(previous_sha256)
        previous_object_key = r2_object_key(previous_filename)
        report.immutable_previous_object_key = previous_object_key
        if public_base_url:
            previous_database_url = r2_public_url(public_base_url, previous_object_key)

    if not config.r2_access_key_id or not config.r2_secret_access_key:
        issues.append("r2_s3_credentials_missing")
    elif not config.r2_s3_endpoint:
        issues.append("r2_s3_endpoint_unconfigured")

    # Continue building manifest only if local verification passed
    manifest_ready = local.passed and not pages_issues and public_base_url
    runtime_manifest: dict[str, Any] | None = None
    if manifest_ready:
        current_database_url = r2_public_url(public_base_url, current_object_key)
        report.r2_database_url = current_database_url
        report.pages_manifest_url = pages_manifest_url(config.pages_base_url)
        runtime_manifest = build_runtime_manifest(
            current_sha256=local.sha256,
            current_byte_size=local.byte_size,
            current_database_url=current_database_url,
            current_database_filename=current_filename,
            generated_at=utc_now_iso(),
            previous_database_url=previous_database_url,
            previous_sha256=previous_sha256,
            total_card_count=local.total_cards,
            per_language_counts=local.per_language_counts,
        )

    r2_upload_ok = False
    previous_upload_ok = True
    if manifest_ready and runtime_manifest:
        bucket_ok, bucket_message = ensure_r2_bucket(config, root=root, dry_run=dry_run)
        report.public_read_policy_result = bucket_message
        if not bucket_ok:
            issues.append(bucket_message)

        r2_upload_ok, upload_message = upload_r2_object(
            config=config,
            local_path=database_path,
            object_key=current_object_key,
            root=root,
            dry_run=dry_run,
            expected_sha256=local.sha256,
            expected_size=local.byte_size,
        )
        if not r2_upload_ok:
            issues.append(upload_message)
        else:
            report.complete_download_checksum_result = upload_message

        if previous_database_path.exists() and previous_object_key and previous_sha256:
            previous_upload_ok, previous_message = upload_r2_object(
                config=config,
                local_path=previous_database_path,
                object_key=previous_object_key,
                root=root,
                dry_run=dry_run,
                expected_sha256=previous_sha256,
                expected_size=previous_database_path.stat().st_size,
            )
            if not previous_upload_ok:
                issues.append(previous_message)
            report.rollback_result = previous_message
        else:
            report.rollback_result = "no_previous_database"

    if runtime_manifest and not (r2_upload_ok and previous_upload_ok):
        issues.append("manifest_publication_blocked_pending_r2_verification")
    elif runtime_manifest and r2_upload_ok and previous_upload_ok:
        source_manifest_path = output_dir / MANIFEST_BASENAME
        source_manifest = {}
        if source_manifest_path.exists():
            try:
                source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                source_manifest = {}
        integrity_report = build_integrity_report(source_manifest)
        integrity_path = output_dir / INTEGRITY_REPORT_BASENAME
        if not dry_run:
            preserve_manifest_rollback(output_dir)
            write_json_atomic(output_dir / MANIFEST_BASENAME, runtime_manifest)
            write_json_atomic(integrity_path, integrity_report)
            sidecar_path.write_text(local.sha256 + "\n", encoding="utf-8")
            report.files_added_or_changed.extend(
                [
                    str((output_dir / MANIFEST_BASENAME).relative_to(root)),
                    str((output_dir / SHA256_BASENAME).relative_to(root)),
                    str(integrity_path.relative_to(root)),
                ]
            )
        else:
            report.files_added_or_changed.extend(
                [
                    f"dry_run:{(output_dir / MANIFEST_BASENAME).relative_to(root)}",
                    f"dry_run:{(output_dir / SHA256_BASENAME).relative_to(root)}",
                    f"dry_run:{integrity_path.relative_to(root)}",
                ]
            )
        report.pages_deployment_result = (
            "dry_run_pages_contract_files_ready_for_git_push"
            if dry_run
            else "pages_contract_files_written_locally_push_required_for_live_pages"
        )

    if not skip_live_verification and runtime_manifest and r2_upload_ok and public_base_url:
        if dry_run:
            report.range_request_result = "skipped_dry_run"
            report.sqlite_health_result = "skipped_dry_run"
        else:
            remote = verify_remote_sqlite(
                report.r2_database_url or "",
                expected_sha256=local.sha256,
                expected_size=local.byte_size,
            )
            report.r2_content_type = remote.content_type
            report.r2_cache_control = remote.cache_control
            report.range_request_result = (
                "supported" if remote.range_supported else "unsupported_or_failed"
            )
            report.complete_download_checksum_result = (
                "passed" if remote.downloaded_sha256 == local.sha256 else "failed"
            )
            report.sqlite_health_result = "passed" if remote.sqlite_health_passed else "failed"
            issues.extend(remote.issues)
            manifest_issues = verify_manifest_url(
                report.pages_manifest_url or "",
                expected_manifest=runtime_manifest,
            )
            issues.extend(manifest_issues)

    report.unresolved_issues = issues
    passed = (
        not issues
        and local.passed
        and runtime_manifest is not None
        and r2_upload_ok
        and previous_upload_ok
        and report.tests_result in {"passed", "skipped"}
    )
    report.classification = "PASS" if passed else "FAIL"
    return report


def _parse_int_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
