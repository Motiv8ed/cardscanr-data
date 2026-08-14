"""Create a credential-safe, read-only snapshot of CardScanR production state.

The backup payload is intentionally written outside Git.  The generated manifest
contains counts and checksums, but never credential values or authorization headers.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import boto3
import requests


ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = "cardscanr-data-platform-backup/1.0"
SAFE_RESOURCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object in {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def supabase_headers(secret: str, *, count: bool = False) -> dict[str, str]:
    # New Supabase secret keys belong in apikey, not Authorization. Sending them
    # as a bearer token is rejected and can trigger a misleading browser warning.
    headers = {
        "apikey": secret,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-Client-Info": USER_AGENT,
    }
    if count:
        headers["Prefer"] = "count=exact"
    return headers


def discover_supabase_resources(base_url: str, secret: str) -> list[str]:
    response = requests.get(
        f"{base_url.rstrip('/')}/rest/v1/",
        headers={
            **supabase_headers(secret),
            "Accept": "application/openapi+json",
        },
        timeout=60,
    )
    response.raise_for_status()
    schema = response.json()
    resources: set[str] = set()
    for raw_path in schema.get("paths", {}):
        resource = raw_path.strip("/")
        if SAFE_RESOURCE.fullmatch(resource):
            resources.add(resource)
    return sorted(resources)


def export_supabase_resource(
    *, base_url: str, secret: str, resource: str, destination: Path, page_size: int
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    offset = 0
    row_count = 0
    expected_total: int | None = None
    page_count = 0
    with gzip.open(temporary, "wt", encoding="utf-8", newline="\n") as handle:
        while True:
            response = requests.get(
                f"{base_url.rstrip('/')}/rest/v1/{quote(resource, safe='')}",
                headers=supabase_headers(secret, count=True),
                params={"select": "*", "limit": page_size, "offset": offset},
                timeout=120,
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError(f"unexpected response shape for {resource}")
            content_range = response.headers.get("Content-Range", "")
            if "/" in content_range and content_range.rsplit("/", 1)[1].isdigit():
                expected_total = int(content_range.rsplit("/", 1)[1])
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                handle.write("\n")
            page_count += 1
            row_count += len(rows)
            offset += len(rows)
            if not rows or len(rows) < page_size:
                break
            if expected_total is not None and offset >= expected_total:
                break
    if expected_total is not None and row_count != expected_total:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            f"row-count mismatch for {resource}: exported {row_count}, expected {expected_total}"
        )
    os.replace(temporary, destination)
    return {
        "resource": resource,
        "rows": row_count,
        "expectedRows": expected_total,
        "pages": page_count,
        "relativePath": destination.name,
        "byteSize": destination.stat().st_size,
        "sha256": sha256_file(destination),
    }


def backup_supabase(
    config_path: Path,
    destination: Path,
    *,
    page_size: int,
    known_empty_resources: set[str],
    skip_resources: set[str] | None = None,
) -> dict[str, Any]:
    config = load_json(config_path)
    base_url = str(config["SUPABASE_URL"])
    secret = str(
        config.get("SUPABASE_SECRET_KEY")
        or config.get("SUPABASE_SERVICE_ROLE_KEY")
        or ""
    )
    if not secret:
        raise ValueError("Supabase secret key is missing")
    resources = discover_supabase_resources(base_url, secret)
    exports: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    skip_resources = skip_resources or set()
    for resource in resources:
        if resource in skip_resources:
            skipped.append(
                {
                    "resource": resource,
                    "reason": "explicit_skip_forbidden_or_out_of_scope",
                }
            )
            continue
        target = destination / f"{resource}.jsonl.gz"
        try:
            exports.append(
                export_supabase_resource(
                    base_url=base_url,
                    secret=secret,
                    resource=resource,
                    destination=target,
                    page_size=page_size,
                )
            )
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            if resource not in known_empty_resources or status not in {401, 403}:
                raise
            target.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(target, "wt", encoding="utf-8", newline="\n"):
                pass
            exports.append(
                {
                    "resource": resource,
                    "rows": 0,
                    "expectedRows": 0,
                    "pages": 0,
                    "relativePath": target.name,
                    "byteSize": target.stat().st_size,
                    "sha256": sha256_file(target),
                    "status": "permission_denied_but_independently_verified_empty",
                    "httpStatus": status,
                }
            )
    return {
        "projectHost": urlparse(base_url).hostname,
        "resourceCount": len(exports),
        "totalRows": sum(item["rows"] for item in exports),
        "resources": exports,
        "skippedResources": skipped,
    }


def is_publication_manifest(key: str) -> bool:
    lowered = key.lower()
    name = lowered.rsplit("/", 1)[-1]
    return "manifest" in name or name in {"index.json", "current.json", "latest.json"}


def backup_r2(config_path: Path, destination: Path) -> dict[str, Any]:
    config = load_json(config_path)
    bucket = str(config["r2Bucket"])
    endpoint = str(config.get("r2S3Endpoint") or config["r2Endpoint"])
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=config["r2AccessKeyId"],
        aws_secret_access_key=config["r2SecretAccessKey"],
        region_name="auto",
    )
    inventory: list[dict[str, Any]] = []
    manifest_objects: list[dict[str, Any]] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            key = str(item["Key"])
            inventory.append(
                {
                    "key": key,
                    "byteSize": int(item["Size"]),
                    "etag": str(item.get("ETag", "")).strip('"'),
                    "lastModified": item["LastModified"].isoformat(),
                }
            )
            if not is_publication_manifest(key):
                continue
            target = destination / "objects" / Path(*key.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
            manifest_objects.append(
                {
                    "key": key,
                    "relativePath": target.relative_to(destination).as_posix(),
                    "byteSize": target.stat().st_size,
                    "sha256": sha256_file(target),
                }
            )
    inventory_path = destination / "bucket_inventory.json"
    atomic_json(inventory_path, inventory)
    return {
        "bucket": bucket,
        "objectCount": len(inventory),
        "totalBytes": sum(item["byteSize"] for item in inventory),
        "inventory": {
            "relativePath": inventory_path.relative_to(destination).as_posix(),
            "byteSize": inventory_path.stat().st_size,
            "sha256": sha256_file(inventory_path),
        },
        "publicationManifestCount": len(manifest_objects),
        "publicationManifests": manifest_objects,
    }


def iter_local_manifests() -> Iterable[Path]:
    candidates = [ROOT / "public" / "v1", ROOT / "data" / "images"]
    seen: set[Path] = set()
    for base in candidates:
        if not base.exists():
            continue
        for path in base.rglob("*.json"):
            lowered = path.name.lower()
            if "manifest" not in lowered and lowered not in {"index.json", "current.json", "latest.json"}:
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path


def backup_local_manifests(destination: Path) -> dict[str, Any]:
    copied: list[dict[str, Any]] = []
    for source in sorted(iter_local_manifests()):
        relative = source.relative_to(ROOT)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(
            {
                "source": relative.as_posix(),
                "relativePath": target.relative_to(destination).as_posix(),
                "byteSize": target.stat().st_size,
                "sha256": sha256_file(target),
            }
        )
    return {"fileCount": len(copied), "files": copied}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(r"D:\CardScanR_Archive\backups"),
        help="parent directory for the timestamped backup",
    )
    parser.add_argument(
        "--supabase-config", type=Path, default=ROOT / "supabase_env.local.json"
    )
    parser.add_argument(
        "--cloudflare-config", type=Path, default=ROOT / "cloudflare_env.local.json"
    )
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument(
        "--known-empty-resource",
        action="append",
        default=[],
        help=(
            "resource independently verified to contain zero rows; permits a 401/403 "
            "Data API response without silently omitting it"
        ),
    )
    parser.add_argument(
        "--skip-resource",
        action="append",
        default=[],
        help=(
            "resource to omit from the backup (e.g. user-data tables forbidden to the "
            "current key); recorded in the manifest as skipped, not as empty"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.page_size <= 1000:
        raise ValueError("page size must be between 1 and 1000")
    output = args.output_root.resolve() / f"worldwide_prechange_{utc_stamp()}"
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "schemaVersion": "1.0.0",
        "classification": "PRE_CHANGE_PRODUCTION_BACKUP",
        "generatedAtUtc": utc_iso(),
        "repositoryHead": git_head(),
        "credentialsIncluded": False,
        "supabase": backup_supabase(
            args.supabase_config.resolve(),
            output / "supabase",
            page_size=args.page_size,
            known_empty_resources=set(args.known_empty_resource),
            skip_resources=set(args.skip_resource),
        ),
        "r2": backup_r2(args.cloudflare_config.resolve(), output / "r2"),
        "localPublicationManifests": backup_local_manifests(output / "local_manifests"),
    }
    manifest_path = output / "BACKUP_MANIFEST.json"
    atomic_json(manifest_path, manifest)
    summary = {
        "backupPath": str(output),
        "manifestPath": str(manifest_path),
        "manifestSha256": sha256_file(manifest_path),
        "supabaseResources": manifest["supabase"]["resourceCount"],
        "supabaseRows": manifest["supabase"]["totalRows"],
        "r2ObjectsInventoried": manifest["r2"]["objectCount"],
        "r2ManifestsBackedUp": manifest["r2"]["publicationManifestCount"],
        "localManifestsBackedUp": manifest["localPublicationManifests"]["fileCount"],
        "credentialsIncluded": False,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
