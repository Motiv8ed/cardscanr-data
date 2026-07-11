from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.r2_s3 import build_s3_client, ensure_bucket_accessible, object_matches, upload_object


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    config = json.loads((ROOT / "cloudflare_env.local.json").read_text(encoding="utf-8-sig"))
    database = ROOT / "reports" / "global_rollout" / "artifacts" / "global_catalogue_canary_v2.sqlite"
    index_report = json.loads((ROOT / "reports" / "global_rollout" / "global_search_index.json").read_text(encoding="utf-8"))
    digest = sha256_file(database)
    if digest != index_report["sha256"]:
        raise RuntimeError("local global index checksum does not match its report")
    size = database.stat().st_size
    if size > 500_000_000:
        raise RuntimeError("canary exceeds the approved bounded 500 MB upload")
    endpoint = str(config.get("r2S3Endpoint") or config.get("r2Endpoint") or "")
    public_base = str(config.get("r2PublicDevUrl") or config.get("r2PublicBaseUrl") or "").rstrip("/")
    bucket = str(config["r2Bucket"])
    client = build_s3_client(endpoint_url=endpoint, access_key_id=str(config["r2AccessKeyId"]), secret_access_key=str(config["r2SecretAccessKey"]))
    accessible, reason = ensure_bucket_accessible(client, bucket)
    if not accessible:
        raise RuntimeError(reason)
    database_key = f"canary/global-catalogue/v2/{digest[:16]}/global_catalogue_canary_v2.sqlite"
    matches, _ = object_matches(client, bucket=bucket, object_key=database_key, expected_sha256=digest, expected_size=size)
    uploaded = False
    if not matches:
        upload_object(client, bucket=bucket, object_key=database_key, local_path=database, content_type="application/vnd.sqlite3", cache_control="public, max-age=31536000, immutable")
        uploaded = True
    verified, verification = object_matches(client, bucket=bucket, object_key=database_key, expected_sha256=digest, expected_size=size)
    if not verified:
        raise RuntimeError(verification)
    manifest = {
        "catalogueSchemaVersion": "1.0.0",
        "searchIndexSchemaVersion": "2.0.0",
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "generatorVersion": "global-canary-v2",
        "databaseFilename": database.name,
        "databaseUrl": f"{public_base}/{database_key}",
        "sha256": digest,
        "byteSize": size,
        "contentFingerprint": digest,
        "supportedLanguages": sorted(index_report["perLanguageCounts"]),
        "totalCardCount": index_report["records"],
        "perLanguageCounts": index_report["perLanguageCounts"],
        "minimumCompatibleAppVersion": "1.0.0+23",
        "minimumCompatibleAppVersionStatus": "qa_only",
        "previousDatabaseUrl": None,
        "previousSha256": None,
        "updatePolicy": "non_production_canary_manual_activation",
        "rollbackPolicy": "delete_qa_activation_and_restore_previous_local_index",
        "production": False,
    }
    manifest_path = ROOT / "reports" / "global_rollout" / "artifacts" / "global_catalogue_canary_v2.manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_key = f"canary/global-catalogue/v2/{digest[:16]}/global_catalogue_canary_v2.manifest.json"
    upload_object(client, bucket=bucket, object_key=manifest_key, local_path=manifest_path, content_type="application/json", cache_control="public, max-age=300")
    result = {"classification":"PASS","databaseUploaded":uploaded,"databaseVerified":verified,"databaseKey":database_key,"databaseUrl":manifest["databaseUrl"],"manifestKey":manifest_key,"manifestUrl":f"{public_base}/{manifest_key}","sha256":digest,"sizeBytes":size,"productionManifestReplaced":False,"r2ImageWrites":0}
    (ROOT / "reports" / "global_rollout" / "global_index_canary_publication.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("classification","databaseUploaded","databaseVerified","databaseKey","manifestKey","sha256","sizeBytes")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
