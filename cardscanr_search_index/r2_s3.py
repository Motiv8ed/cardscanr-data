from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .builder import sha256_file

R2_REGION = "auto"


def build_s3_client(
    *,
    endpoint_url: str,
    access_key_id: str,
    secret_access_key: str,
) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=R2_REGION,
        config=Config(signature_version="s3v4"),
    )


def ensure_bucket_accessible(client: Any, bucket: str) -> tuple[bool, str]:
    try:
        client.head_bucket(Bucket=bucket)
        return True, f"bucket_accessible:{bucket}"
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "unknown")
        return False, f"bucket_access_failed:{code}"


def object_matches(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    expected_sha256: str,
    expected_size: int,
) -> tuple[bool, str]:
    try:
        head = client.head_object(Bucket=bucket, Key=object_key)
    except ClientError:
        return False, "object_not_found"
    actual_size = int(head.get("ContentLength") or 0)
    if actual_size != expected_size:
        return False, f"size_mismatch expected={expected_size} actual={actual_size}"

    with tempfile.TemporaryDirectory() as tmp_dir:
        target = Path(tmp_dir) / "object.bin"
        client.download_file(bucket, object_key, str(target))
        actual_sha = sha256_file(target)
    if actual_sha != expected_sha256:
        return False, f"sha256_mismatch expected={expected_sha256} actual={actual_sha}"
    return True, "verified_existing_object"


def upload_object(
    client: Any,
    *,
    bucket: str,
    object_key: str,
    local_path: Path,
    content_type: str,
    cache_control: str,
) -> None:
    with open(local_path, "rb") as handle:
        client.put_object(
            Bucket=bucket,
            Key=object_key,
            Body=handle,
            ContentType=content_type,
            CacheControl=cache_control,
        )


def head_object_metadata(client: Any, *, bucket: str, object_key: str) -> dict[str, Any]:
    return client.head_object(Bucket=bucket, Key=object_key)
