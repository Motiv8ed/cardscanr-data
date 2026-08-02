"""Verify and register immutable catalogue bundles in staging publication history."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .publication_export import file_sha256
from .schema import connect
from .tcgdex import canonical_json, stable_id


def _verified_artifacts(bundle: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for name, expected in sorted((manifest.get("outputs") or {}).items()):
        path = bundle / name
        if not path.is_file():
            raise FileNotFoundError(f"Publication artifact is missing: {path}")
        actual_bytes = path.stat().st_size
        actual_sha = file_sha256(path)
        if actual_bytes != expected.get("bytes") or actual_sha != expected.get("sha256"):
            raise RuntimeError(
                f"Publication artifact mismatch for {name}: bytes={actual_bytes}/{expected.get('bytes')} "
                f"sha256={actual_sha}/{expected.get('sha256')}"
            )
        artifacts.append({
            "artifact_type": "jsonl", "object_key": name, "byte_size": actual_bytes,
            "sha256": actual_sha, "rows": expected.get("rows"),
        })
    manifest_path = bundle / "manifest.json"
    artifacts.append({
        "artifact_type": "manifest", "object_key": "manifest.json",
        "byte_size": manifest_path.stat().st_size, "sha256": file_sha256(manifest_path), "rows": None,
    })
    return artifacts


def register_bundle(
    database: Path,
    bundle: Path,
    *,
    status: str = "canary",
    previous_version: str | None = None,
) -> dict[str, Any]:
    if status not in {"canary", "verified", "active"}:
        raise ValueError("Only canary, verified, or active bundles may be registered")
    bundle = bundle.resolve()
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest.get("catalogueVersion")
    if not version or bundle.name != version:
        raise ValueError(f"Bundle directory/version mismatch: directory={bundle.name!r}, version={version!r}")
    if manifest.get("integrity") != {"sqliteIntegrityCheck": "ok", "foreignKeyFailures": 0}:
        raise RuntimeError("Bundle manifest did not pass its staging integrity gates")
    artifacts = _verified_artifacts(bundle, manifest)
    manifest_sha = artifacts[-1]["sha256"]
    run_id = stable_id("publication", version, manifest_sha)
    now = datetime.now(timezone.utc).isoformat()
    counters = {item["object_key"]: item["rows"] for item in artifacts if item["rows"] is not None}
    gates = {
        "artifactChecksumsVerified": True,
        "artifactCount": len(artifacts),
        "sourceIntegrity": manifest["integrity"],
        "productionPublished": bool(manifest.get("productionPublished")),
    }
    connection = connect(str(database))
    try:
        previous_id = None
        if previous_version:
            row = connection.execute("select id from publication_run where version=?", (previous_version,)).fetchone()
            if not row:
                raise ValueError(f"Previous publication version is not registered: {previous_version}")
            previous_id = row["id"]
        existing = connection.execute("select * from publication_run where version=?", (version,)).fetchone()
        if existing:
            if existing["manifest_sha256"] != manifest_sha or existing["object_prefix"] != str(bundle):
                raise RuntimeError(f"Immutable publication version conflict: {version}")
            if existing["status"] != status:
                raise RuntimeError(
                    f"Publication {version} is already {existing['status']}; use a dedicated promotion operation"
                )
            run_id = existing["id"]
        with connection:
            if not existing:
                connection.execute(
                    """insert into publication_run values (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, version, status, manifest.get("sourceDatabaseSha256"), manifest_sha,
                     str(bundle), previous_id, canonical_json(counters), canonical_json(gates),
                     manifest.get("generatedAtUtc") or now, now if status == "active" else None, now, 1),
                )
            for artifact in artifacts:
                artifact_id = stable_id("publication-artifact", run_id, artifact["object_key"])
                prior = connection.execute(
                    "select byte_size,sha256 from publication_artifact where publication_run_id=? and object_key=?",
                    (run_id, artifact["object_key"]),
                ).fetchone()
                if prior and (prior["byte_size"], prior["sha256"]) != (artifact["byte_size"], artifact["sha256"]):
                    raise RuntimeError(f"Immutable publication artifact conflict: {artifact['object_key']}")
                connection.execute(
                    """insert or ignore into publication_artifact values (?,?,?,?,?,?,?,?)""",
                    (artifact_id, run_id, artifact["artifact_type"], artifact["object_key"], None,
                     artifact["byte_size"], artifact["sha256"], now),
                )
        return {
            "publication_run_id": run_id, "version": version, "status": status,
            "manifest_sha256": manifest_sha, "artifact_count": len(artifacts),
            "artifact_bytes": sum(item["byte_size"] for item in artifacts),
            "object_prefix": str(bundle), "rollback_retained": True,
        }
    finally:
        connection.close()
