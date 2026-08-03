#!/usr/bin/env python3
"""Activate the production packed catalogue pointer without touching canary releases."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.publication import load_publication_config
from cardscanr_search_index.r2_s3 import build_s3_client, ensure_bucket_accessible, upload_object

JSON_CONTENT_TYPE = "application/json"
DATABASE_CONTENT_TYPE = "application/vnd.sqlite3"
GZIP_CONTENT_TYPE = "application/gzip"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
ACTIVE_CACHE = "public, max-age=60, must-revalidate"
ACTIVE_SEARCH_POINTER = "v2/catalog/pokemon/search/catalogue.manifest.json"
ACTIVE_PACKS_POINTER = "v2/catalog/pokemon/packs/active/catalogue.packs.manifest.json"


def head_public(url: str) -> dict:
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "CardScanR-CatalogueActivator/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return {
                "url": url,
                "status": resp.status,
                "contentType": resp.headers.get("Content-Type"),
                "contentLength": resp.headers.get("Content-Length"),
            }
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None)
        return {"url": url, "status": code or 0, "error": str(exc)}


def get_public(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "CardScanR-CatalogueActivator/1.0"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, resp.read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packs-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "cloudflare_env.local.json")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--skip-upload-packs", action="store_true")
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()

    manifest_path = args.packs_dir / "catalogue.packs.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Public manifests must never include local filesystem paths.
    public_manifest = json.loads(json.dumps(manifest))
    for pack in public_manifest.get("packs", []):
        for key in ("sqlitePath", "gzipPath"):
            pack.pop(key, None)
    public_body = (
        json.dumps(public_manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    if b"CardScanR_worldwide_runtime" in public_body or b":\\\\" in public_body:
        raise SystemExit("public manifest still contains local filesystem paths")
    public_manifest_path = args.packs_dir / "catalogue.packs.manifest.public.json"
    public_manifest_path.write_bytes(public_body)
    body = public_body
    local_sha = hashlib.sha256(body).hexdigest()
    # Determinism check
    body2 = (
        json.dumps(json.loads(body.decode("utf-8")), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    if body != body2:
        raise SystemExit("public manifest serialization is not deterministic")
    config = load_publication_config(args.config)
    client = build_s3_client(
        endpoint_url=config.r2_s3_endpoint,
        access_key_id=config.r2_access_key_id,
        secret_access_key=config.r2_secret_access_key,
    )
    ok, detail = ensure_bucket_accessible(client, config.r2_bucket)
    if not ok:
        raise SystemExit(detail)

    public_base = config.r2_public_base_url.rstrip("/")
    rollback_before = head_public(f"{public_base}/{ACTIVE_SEARCH_POINTER}")
    packs_active_before = head_public(f"{public_base}/{ACTIVE_PACKS_POINTER}")

    uploaded = []
    if not args.skip_upload_packs:
        for pack in manifest["packs"]:
            upload_object(
                client,
                bucket=config.r2_bucket,
                object_key=pack["objectKey"],
                local_path=Path(pack["sqlitePath"]),
                content_type=DATABASE_CONTENT_TYPE,
                cache_control=IMMUTABLE_CACHE,
            )
            upload_object(
                client,
                bucket=config.r2_bucket,
                object_key=pack["gzipObjectKey"],
                local_path=Path(pack["gzipPath"]),
                content_type=GZIP_CONTENT_TYPE,
                cache_control=IMMUTABLE_CACHE,
            )
            uploaded.append(pack["packId"])

    release_id = manifest["catalogueReleaseId"]
    immutable_key = f"v2/catalog/pokemon/packs/{release_id}/catalogue.packs.manifest.json"
    upload_object(
        client,
        bucket=config.r2_bucket,
        object_key=immutable_key,
        local_path=public_manifest_path,
        content_type=JSON_CONTENT_TYPE,
        cache_control=IMMUTABLE_CACHE,
    )
    immutable_url = f"{public_base}/{immutable_key}"
    imm_head = head_public(immutable_url)
    imm_status, imm_bytes = get_public(immutable_url)
    imm_sha = hashlib.sha256(imm_bytes).hexdigest()

    activated = False
    active_heads = {}
    if args.activate:
        # Keep packs/active in sync, then atomically write the production search pointer.
        upload_object(
            client,
            bucket=config.r2_bucket,
            object_key=ACTIVE_PACKS_POINTER,
            local_path=public_manifest_path,
            content_type=JSON_CONTENT_TYPE,
            cache_control=ACTIVE_CACHE,
        )
        upload_object(
            client,
            bucket=config.r2_bucket,
            object_key=ACTIVE_SEARCH_POINTER,
            local_path=public_manifest_path,
            content_type=JSON_CONTENT_TYPE,
            cache_control=ACTIVE_CACHE,
        )
        activated = True
        for key in (ACTIVE_PACKS_POINTER, ACTIVE_SEARCH_POINTER):
            url = f"{public_base}/{key}"
            st, data = get_public(url)
            active_heads[key] = {
                "url": url,
                "status": st,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "matchesLocal": hashlib.sha256(data).hexdigest() == local_sha,
            }

    # Verify default packs resolve
    pack_heads = []
    for pack in manifest["packs"]:
        if pack["packId"] not in manifest.get("defaultInstallPackIds", []):
            continue
        for field in ("compressedDatabaseUrl", "databaseUrl"):
            pack_heads.append(head_public(pack[field]))

    # Canary retention check
    canaries = {
        "canary2": f"{public_base}/v2/catalog/pokemon/search/versions/89c07376b30e9b0edf8ee1ad74c8b53583dc12a11f5f3fb71ec5d8419db5428b/manifest.json",
        "canary3": f"{public_base}/v2/catalog/pokemon/search/versions/95f4acb70ff30f19a3d18d21435d21711fda557ea003f9f131319ff5db540425/manifest.json",
        "canary4": f"{public_base}/v2/catalog/pokemon/search/versions/87939dfc7a5e7a29a5c8191b1a56abd947a7e5f89011ef91b84231318e0e41c0/manifest.json",
        "canary4_packs": f"{public_base}/v2/catalog/pokemon/packs/canary4-packs-20260803/catalogue.packs.manifest.json",
    }
    canary_status = {name: head_public(url) for name, url in canaries.items()}

    report = {
        "classification": "PASS" if activated and all(v.get("matchesLocal") for v in active_heads.values()) else ("UPLOADED" if not activated else "FAIL"),
        "releaseId": release_id,
        "localManifestSha256": local_sha,
        "localManifestBytes": len(body),
        "immutableKey": immutable_key,
        "immutableVerification": {
            "head": imm_head,
            "getStatus": imm_status,
            "sha256": imm_sha,
            "matchesLocal": imm_sha == local_sha,
        },
        "rollbackEvidenceBeforeActivation": {
            "searchPointer": rollback_before,
            "packsActivePointer": packs_active_before,
        },
        "packsUploaded": uploaded,
        "activated": activated,
        "activePointers": active_heads,
        "defaultPackHeads": pack_heads,
        "canariesPreserved": canary_status,
        "gates": manifest.get("gates"),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["classification"] in {"PASS", "UPLOADED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
