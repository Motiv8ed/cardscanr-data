#!/usr/bin/env python3
"""Publish the image cards manifest to immutable R2 storage and refresh Pages redirect."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_data_paths import IMAGE_CARDS_MANIFEST_PATH, IMAGE_CARDS_MANIFEST_PUBLIC_URL
from cardscanr_search_index.publication import load_publication_config, resolve_publication_config
from cardscanr_search_index.r2_s3 import build_s3_client, object_matches, upload_object

R2_CONTENT_TYPE = "application/json; charset=utf-8"
R2_CACHE_CONTROL = "public, max-age=31536000, immutable"
REDIRECTS_PATH = ROOT / "public" / "_redirects"
VERIFY_USER_AGENT = "CardScanRPublicationVerify/1.0"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def immutable_object_key(digest: str) -> str:
    return f"v1/images/cards-manifest.{digest}.json"


def public_r2_url(public_base_url: str, object_key: str) -> str:
    return f"{public_base_url.rstrip('/')}/{object_key}"


def upload_manifest(*, config, manifest_path: Path) -> tuple[str, int, str, str]:
    digest = sha256_file(manifest_path)
    size = manifest_path.stat().st_size
    object_key = immutable_object_key(digest)
    client = build_s3_client(
        endpoint_url=config.r2_s3_endpoint or "",
        access_key_id=config.r2_access_key_id or "",
        secret_access_key=config.r2_secret_access_key or "",
    )
    existing_ok, existing_message = object_matches(
        client,
        bucket=config.r2_bucket,
        object_key=object_key,
        expected_sha256=digest,
        expected_size=size,
    )
    if not existing_ok:
        upload_object(
            client,
            bucket=config.r2_bucket,
            object_key=object_key,
            local_path=manifest_path,
            content_type=R2_CONTENT_TYPE,
            cache_control=R2_CACHE_CONTROL,
        )
        verified, verify_message = object_matches(
            client,
            bucket=config.r2_bucket,
            object_key=object_key,
            expected_sha256=digest,
            expected_size=size,
        )
        if not verified:
            raise RuntimeError(f"post_upload_verification_failed:{verify_message}")
        upload_message = verify_message
    else:
        upload_message = f"idempotent_existing_object:{existing_message}"

    public_base = config.r2_public_base_url
    if not public_base:
        raise RuntimeError("r2_public_base_url_unconfigured")
    public_url = public_r2_url(public_base, object_key)
    return digest, size, public_url, upload_message


def verify_public_object(url: str, *, expected_sha256: str, expected_size: int) -> list[str]:
    issues: list[str] = []
    request = urllib.request.Request(url, headers={"User-Agent": VERIFY_USER_AGENT}, method="HEAD")
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status != 200:
            issues.append(f"head_status:{response.status}")
        content_length = int(response.headers.get("Content-Length") or 0)
        content_type = response.headers.get("Content-Type") or ""
        cache_control = response.headers.get("Cache-Control") or ""
        if content_length != expected_size:
            issues.append(f"content_length_mismatch expected={expected_size} actual={content_length}")
        if "json" not in content_type:
            issues.append(f"content_type_mismatch:{content_type}")
        if "immutable" not in cache_control:
            issues.append(f"cache_control_missing_immutable:{cache_control}")

    download_request = urllib.request.Request(url, headers={"User-Agent": VERIFY_USER_AGENT})
    with urllib.request.urlopen(download_request, timeout=300) as response:
        body = response.read()
    if len(body) != expected_size:
        issues.append(f"download_size_mismatch expected={expected_size} actual={len(body)}")
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        issues.append("downloaded_sha256_mismatch")
    return issues


def write_redirect(public_url: str) -> None:
    redirect_line = f"{IMAGE_CARDS_MANIFEST_PUBLIC_URL} {public_url} 302"
    existing_lines: list[str] = []
    if REDIRECTS_PATH.exists():
        existing_lines = [
            line.strip()
            for line in REDIRECTS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith(IMAGE_CARDS_MANIFEST_PUBLIC_URL)
        ]
    lines = [*existing_lines, redirect_line, ""]
    REDIRECTS_PATH.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish image cards manifest to immutable R2 storage.")
    parser.add_argument("--config", default=None)
    parser.add_argument("--manifest", default=str(IMAGE_CARDS_MANIFEST_PATH))
    parser.add_argument("--skip-verify", action="store_true")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(json.dumps({"error": f"missing manifest: {manifest_path}"}, indent=2))
        return 1

    config = (
        load_publication_config(Path(args.config))
        if args.config
        else resolve_publication_config(root=ROOT)
    )
    digest, size, public_url, upload_message = upload_manifest(config=config, manifest_path=manifest_path)
    write_redirect(public_url)

    issues: list[str] = []
    if not args.skip_verify:
        try:
            issues.extend(
                verify_public_object(
                    public_url,
                    expected_sha256=digest,
                    expected_size=size,
                )
            )
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            issues.append(f"verify_failed:{exc}")

    result = {
        "sha256": digest,
        "byteSize": size,
        "objectKey": immutable_object_key(digest),
        "publicUrl": public_url,
        "redirectPath": IMAGE_CARDS_MANIFEST_PUBLIC_URL,
        "uploadResult": upload_message,
        "issues": issues,
    }
    print(json.dumps(result, indent=2))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
