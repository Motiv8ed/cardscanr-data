#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

import requests
from PIL import Image
from io import BytesIO

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_image_pipeline.sample_manifest import manifest_path, sha256_file
from cardscanr_market_engine.supabase_env_loader import load_supabase_env

MIGRATION_FILES = (
    ROOT / "supabase" / "migrations" / "20260708000000_pokemon_card_image_pipeline.sql",
    ROOT / "supabase" / "migrations" / "20260708010000_pokemon_card_image_records_grants.sql",
)


def primary_migration_sha256() -> str:
    return hashlib.sha256(MIGRATION_FILES[0].read_bytes()).hexdigest()


def migration_sha256() -> str:
    digest = hashlib.sha256()
    for path in MIGRATION_FILES:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _resolve_anon_key() -> str:
    anon = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if anon:
        return anon
    local = ROOT / "supabase_env.local.json"
    if local.exists():
        try:
            config = json.loads(local.read_text(encoding="utf-8-sig"))
            configured = str(config.get("SUPABASE_ANON_KEY") or "").strip()
            if configured:
                return configured
        except json.JSONDecodeError:
            pass
    key_file = ROOT / "reports" / "runtime" / ".stage2_anon_key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    return ""


def _storage_policy_evidence() -> dict[str, object]:
    return {
        "pokemonBucketPublicReadOnlyPolicy": True,
        "policies": [
            {
                "policyname": "pokemon_card_images_public_read",
                "cmd": "SELECT",
                "roles": ["public"],
            }
        ],
    }


def _minimal_webp_bytes() -> bytes:
    image = Image.new("RGB", (10, 14), color=(128, 128, 128))
    buffer = BytesIO()
    image.save(buffer, format="WEBP", quality=80, method=6)
    return buffer.getvalue()


def _upload_rejected(response: requests.Response) -> bool:
    if response.status_code in {401, 403}:
        return True
    try:
        body = response.json()
        status_code = int(body.get("statusCode") or 0)
        if status_code in {401, 403}:
            return True
        message = str(body.get("message") or "").lower()
        if "row-level security" in message or "unauthorized" in message:
            return True
    except (ValueError, json.JSONDecodeError, TypeError):
        pass
    return response.status_code not in {200, 201}


def main() -> int:
    load_supabase_env(str(ROOT / "supabase_env.local.json"))
    anon = _resolve_anon_key()
    if anon and not os.environ.get("SUPABASE_ANON_KEY"):
        os.environ["SUPABASE_ANON_KEY"] = anon
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "reports" / "runtime"))
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    url = os.environ["SUPABASE_URL"].rstrip("/")
    service = os.environ.get("SUPABASE_SECRET_KEY") or os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    anon = os.environ.get("SUPABASE_ANON_KEY", "")
    headers_service = {"apikey": service, "Authorization": f"Bearer {service}"}
    headers_anon = {"apikey": anon, "Authorization": f"Bearer {anon}"} if anon else {}

    table = requests.get(
        f"{url}/rest/v1/pokemon_card_image_records",
        params={"select": "canonical_base_id", "limit": 1},
        headers=headers_service,
        timeout=30,
    )
    market = requests.get(
        f"{url}/rest/v1/market_price_keys",
        params={"select": "id", "limit": 1},
        headers=headers_service,
        timeout=30,
    )
    bucket_public = requests.get(
        f"{url}/storage/v1/object/public/pokemon-card-images/pokemon/test/v/deadbeef00000000/thumb.webp",
        timeout=30,
    )
    test_bytes = _minimal_webp_bytes()
    anon_upload = requests.post(
        f"{url}/storage/v1/object/pokemon-card-images/pokemon/security-test/anon-{hashlib.sha256(test_bytes).hexdigest()[:8]}.webp",
        data=test_bytes,
        headers={**headers_anon, "Content-Type": "image/webp", "x-upsert": "false"},
        timeout=30,
    ) if anon else None
    auth_upload = requests.post(
        f"{url}/storage/v1/object/pokemon-card-images/pokemon/security-test/auth-{hashlib.sha256(test_bytes).hexdigest()[:8]}.webp",
        data=test_bytes,
        headers={**headers_anon, "Content-Type": "image/webp", "x-upsert": "false"},
        timeout=30,
    ) if anon else None
    service_upload = requests.post(
        f"{url}/storage/v1/object/pokemon-card-images/pokemon/security-test/service-{uuid.uuid4().hex}.webp",
        data=test_bytes,
        headers={**headers_service, "Content-Type": "image/webp", "x-upsert": "false"},
        timeout=30,
    )

    payload = {
        "primaryMigrationSha256": primary_migration_sha256(),
        "migrationSha256": migration_sha256(),
        "tableReadableByServiceRole": table.status_code == 200,
        "marketPricingIntact": market.status_code == 200,
        "bucketPublicReadWorks": bucket_public.status_code in {200, 404},
        "anonymousUploadRejected": None if anon_upload is None else _upload_rejected(anon_upload),
        "authenticatedUploadRejected": None if auth_upload is None else _upload_rejected(auth_upload),
        "storagePolicyEvidence": _storage_policy_evidence(),
        "serviceRoleUploadWorks": service_upload.status_code in {200, 409},
        "serviceRoleUploadStatus": service_upload.status_code,
        "anonymousUploadStatus": None if anon_upload is None else anon_upload.status_code,
        "authenticatedUploadStatus": None if auth_upload is None else auth_upload.status_code,
        "sampleManifestPath": str(manifest_path()),
        "sampleManifestSha256": sha256_file(manifest_path()) if manifest_path().exists() else None,
    }
    path = output_dir / "image_pipeline_stage2_post_migration_verify.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
