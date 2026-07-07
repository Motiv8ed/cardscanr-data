#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_image_pipeline.catalogue import DEFAULT_CATALOGUE_ROOT, iter_catalogue_identities
from cardscanr_image_pipeline.config import ImagePipelineConfig
from dataclasses import replace
from cardscanr_image_pipeline.reports import audit_catalogue_coverage
from cardscanr_image_pipeline.sample_manifest import (
    build_stratified_sample,
    load_sample_manifest,
    manifest_path,
    sha256_file,
    write_sample_manifest,
)
from cardscanr_image_pipeline.stage2_runner import Stage2Runner, build_contact_sheet, write_json_report
from cardscanr_market_engine.supabase_env_loader import load_supabase_env

MIGRATION_FILES = (
    ROOT / "supabase" / "migrations" / "20260708000000_pokemon_card_image_pipeline.sql",
    ROOT / "supabase" / "migrations" / "20260708010000_pokemon_card_image_records_grants.sql",
)
RUNTIME_DIR = ROOT / "reports" / "runtime"


def primary_migration_sha256() -> str:
    return hashlib.sha256(MIGRATION_FILES[0].read_bytes()).hexdigest()


def combined_migration_sha256() -> str:
    digest = hashlib.sha256()
    for path in MIGRATION_FILES:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def migration_sha256() -> str:
    return primary_migration_sha256()


def catalogue_counts() -> dict[str, int]:
    counts = {"en": 0, "jp": 0, "total": 0}
    for identity in iter_catalogue_identities(DEFAULT_CATALOGUE_ROOT, languages=("en", "jp")):
        counts[identity.language] = counts.get(identity.language, 0) + 1
        counts["total"] += 1
    return counts


def git_status() -> str:
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def write_pre_migration_evidence(output_dir: Path) -> Path:
    payload = {
        "generatedAtUtc": _utc_now(),
        "migrationFilename": MIGRATION_FILES[0].name,
        "migrationSha256": primary_migration_sha256(),
        "combinedMigrationSha256": combined_migration_sha256(),
        "gitCommit": git_commit(),
        "gitStatus": git_status(),
        "catalogueCounts": catalogue_counts(),
        "note": "Remote migration/table/bucket state must be checked separately against Supabase.",
    }
    path = output_dir / "image_pipeline_stage2_pre_migration_evidence.json"
    write_json_report(payload, path)
    return path


def render_final_markdown(report: dict[str, object]) -> str:
    lines = [
        "# Image Pipeline Stage 2 Final Report",
        "",
        f"- Classification: **{report.get('classification')}**",
        f"- Migration result: {report.get('migrationResult')}",
        f"- Migration SHA-256: `{report.get('migrationSha256')}`",
        f"- Table verification: {report.get('tableVerification')}",
        f"- Bucket/policy verification: {report.get('bucketVerification')}",
        f"- Sample manifest: `{report.get('sampleManifestPath')}`",
        f"- Sample manifest SHA-256: `{report.get('sampleManifestSha256')}`",
        f"- Attempted: {report.get('attemptedCount')}",
        f"- Downloaded: {report.get('downloadedCount')}",
        f"- Uploaded: {report.get('uploadedCount')}",
        f"- Verified: {report.get('verifiedCount')}",
        f"- Skipped: {report.get('skippedCount')}",
        f"- Failed: {report.get('failedCount')}",
        f"- Ambiguous: {report.get('ambiguousCount')}",
        f"- Provider breakdown: {report.get('providerBreakdown')}",
        f"- Language breakdown: {report.get('languageBreakdown')}",
        f"- Total source bytes: {report.get('totalSourceBytes')}",
        f"- Total thumb bytes: {report.get('totalThumbBytes')}",
        f"- Total display bytes: {report.get('totalDisplayBytes')}",
        f"- Estimated storage all cards: {report.get('estimatedStorageAllCardsBytes')}",
        f"- Estimated storage matchable cards: {report.get('estimatedStorageMatchableCardsBytes')}",
        f"- Idempotent rerun: {report.get('idempotentRerun')}",
        f"- Tests: {report.get('testResult')}",
        f"- Contact sheet: `{report.get('contactSheetPath')}`",
        f"- JSON report: `{report.get('jsonReportPath')}`",
        f"- Full import run: **{report.get('fullImportRun')}**",
        "",
        "## Unresolved defects",
        "",
    ]
    for defect in report.get("unresolvedDefects") or []:
        lines.append(f"- {defect}")
    lines.append("")
    return "\n".join(lines)


def run_unit_tests() -> dict[str, object]:
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_image_pipeline")
    result = unittest.TextTestRunner(stream=open(os.devnull, "w"), verbosity=0).run(suite)
    return {
        "passed": result.wasSuccessful(),
        "testsRun": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
    }


def build_final_report(
    *,
    output_dir: Path,
    runner: Stage2Runner,
    manifest: dict[str, object],
) -> dict[str, object]:
    execute_path = output_dir / "image_pipeline_stage2_execute.json"
    verify_path = output_dir / "image_pipeline_stage2_verify.json"
    migration_path = output_dir / "image_pipeline_stage2_post_migration_verify.json"
    execution = json.loads(execute_path.read_text(encoding="utf-8"))
    verification = json.loads(verify_path.read_text(encoding="utf-8")) if verify_path.exists() else {}
    migration = json.loads(migration_path.read_text(encoding="utf-8")) if migration_path.exists() else {}

    idempotent = runner.verify_idempotent_rerun(manifest)
    idempotent_path = write_json_report(idempotent, output_dir / "image_pipeline_stage2_idempotent_rerun.json")

    contact_path = output_dir / "image_pipeline_stage2_contact_sheet.png"
    build_contact_sheet(execution.get("cards") or [], output_path=contact_path)

    test_result = run_unit_tests()
    coverage = audit_catalogue_coverage()
    coverage_path = write_json_report(coverage, output_dir / "image_pipeline_stage2_coverage.json")

    attempted = int(execution.get("attemptedCount") or 0)
    per_card_stored = 0
    if attempted:
        per_card_stored = (
            int(execution.get("totalThumbBytes") or 0) + int(execution.get("totalDisplayBytes") or 0)
        ) // attempted
    catalogue_total = int(coverage.get("totalCards") or catalogue_counts()["total"])
    matchable_total = int(coverage.get("withProviderMatch") or 0)

    defects: list[str] = []
    migration_ok = bool(migration.get("tableReadableByServiceRole")) and bool(migration.get("marketPricingIntact"))
    bucket_ok = bool(migration.get("bucketPublicReadWorks")) and bool(migration.get("serviceRoleUploadWorks"))
    anon_rejected = migration.get("anonymousUploadRejected")
    auth_rejected = migration.get("authenticatedUploadRejected")
    if anon_rejected is False:
        defects.append("anonymous uploads were not rejected")
        bucket_ok = False
    elif anon_rejected is None:
        defects.append("anonymous upload rejection was not tested (SUPABASE_ANON_KEY unavailable)")
    if auth_rejected is False:
        defects.append("authenticated client uploads were not rejected")
        bucket_ok = False

    if not verification.get("passed"):
        defects.append("independent verification failed")
    if not idempotent.get("passed"):
        defects.append("idempotent rerun did not skip all completed cards without re-download/upload")
    if not test_result.get("passed"):
        defects.append("unit tests failed")
    if int(execution.get("ambiguousCount") or 0) > 0:
        defects.append("ambiguous records were executed")
    if int(execution.get("failedCount") or 0) > 0:
        defects.append("failed executions present")

    passed = (
        migration_ok
        and bucket_ok
        and anon_rejected is True
        and auth_rejected is True
        and verification.get("passed")
        and idempotent.get("passed")
        and test_result.get("passed")
        and int(execution.get("ambiguousCount") or 0) == 0
        and int(execution.get("failedCount") or 0) == 0
        and int(verification.get("verifiedCount") or 0) == attempted
    )

    manifest_file = manifest_path()
    report: dict[str, object] = {
        "classification": "PASS" if passed else "FAIL",
        "generatedAtUtc": _utc_now(),
        "migrationResult": "applied" if migration_ok else "failed_or_unverified",
        "migrationSha256": primary_migration_sha256(),
        "combinedMigrationSha256": combined_migration_sha256(),
        "tableVerification": "passed" if migration.get("tableReadableByServiceRole") else "failed",
        "bucketVerification": "passed" if bucket_ok and anon_rejected is True else "failed_or_incomplete",
        "sampleManifestPath": str(manifest_file),
        "sampleManifestSha256": sha256_file(manifest_file),
        "attemptedCount": attempted,
        "downloadedCount": int(execution.get("downloadedCount") or 0),
        "uploadedCount": int(execution.get("uploadedCount") or 0),
        "verifiedCount": int(verification.get("verifiedCount") or 0),
        "skippedCount": int(execution.get("skippedCount") or 0),
        "failedCount": int(execution.get("failedCount") or 0),
        "ambiguousCount": int(execution.get("ambiguousCount") or 0),
        "providerBreakdown": execution.get("providerBreakdown") or {},
        "languageBreakdown": execution.get("languageBreakdown") or {},
        "totalSourceBytes": int(execution.get("totalSourceBytes") or 0),
        "totalThumbBytes": int(execution.get("totalThumbBytes") or 0),
        "totalDisplayBytes": int(execution.get("totalDisplayBytes") or 0),
        "estimatedStorageAllCardsBytes": per_card_stored * catalogue_total,
        "estimatedStorageMatchableCardsBytes": per_card_stored * matchable_total,
        "catalogueTotalCards": catalogue_total,
        "matchableCards": matchable_total,
        "idempotentRerun": idempotent,
        "testResult": test_result,
        "contactSheetPath": str(contact_path),
        "coverageReportPath": str(coverage_path),
        "idempotentRerunPath": str(idempotent_path),
        "fullImportRun": False,
        "unresolvedDefects": defects,
        "migrationDetails": migration,
        "verificationSummary": {
            "passed": verification.get("passed"),
            "verifiedCount": verification.get("verifiedCount"),
            "issueCount": verification.get("issueCount"),
        },
    }
    json_report_path = output_dir / "image_pipeline_stage2_final_report.json"
    md_report_path = output_dir / "image_pipeline_stage2_final_report.md"
    write_json_report(report, json_report_path)
    report["jsonReportPath"] = str(json_report_path)
    report["markdownReportPath"] = str(md_report_path)
    md_report_path.write_text(render_final_markdown(report), encoding="utf-8")
    write_json_report(report, json_report_path)
    return report


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    load_supabase_env(str(ROOT / "supabase_env.local.json"))
    parser = argparse.ArgumentParser(description="Stage 2 image pipeline orchestrator")
    parser.add_argument("command", choices=["pre-migration", "build-sample", "dry-run", "execute", "verify", "final-report"])
    parser.add_argument("--output-dir", default=str(RUNTIME_DIR))
    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.command == "pre-migration":
        path = write_pre_migration_evidence(output_dir)
        print(f"Wrote {path}")
        return 0

    if args.command == "build-sample":
        manifest = build_stratified_sample()
        path = write_sample_manifest(manifest)
        print(f"Wrote sample manifest {path} ({manifest['cardCount']} cards)")
        return 0

    manifest = load_sample_manifest(manifest_path())

    if args.command in {"execute", "final-report"}:
        config = replace(ImagePipelineConfig.from_env(execute=True, dry_run=False), network_concurrency=2)
    elif args.command == "dry-run":
        config = ImagePipelineConfig.from_env(execute=False, dry_run=True)
    else:
        config = ImagePipelineConfig.from_env(execute=False, dry_run=True)
    runner = Stage2Runner(config)

    if args.command == "dry-run":
        payload = runner.dry_run_manifest(manifest)
        path = write_json_report(payload, output_dir / "image_pipeline_stage2_dry_run.json")
        print(f"Dry run cards={payload['cardCount']} ambiguous={payload['ambiguousCount']} stop={payload['shouldStop']}")
        print(f"Wrote {path}")
        return 1 if payload["shouldStop"] else 0

    if args.command == "execute":
        payload = runner.execute_manifest(manifest)
        path = write_json_report(payload, output_dir / "image_pipeline_stage2_execute.json")
        print(f"Executed attempted={payload['attemptedCount']} uploaded={payload['uploadedCount']} failed={payload['failedCount']}")
        print(f"Wrote {path}")
        return 0 if payload["failedCount"] == 0 else 1

    if args.command == "verify":
        execution = json.loads((output_dir / "image_pipeline_stage2_execute.json").read_text(encoding="utf-8"))
        verification = runner.verify_manifest(manifest, execution_cards=execution.get("cards") or [])
        path = write_json_report(verification, output_dir / "image_pipeline_stage2_verify.json")
        print(f"Verified={verification['verifiedCount']} issues={verification['issueCount']}")
        print(f"Wrote {path}")
        return 0 if verification["passed"] else 1

    if args.command == "final-report":
        report = build_final_report(output_dir=output_dir, runner=runner, manifest=manifest)
        print(f"Classification={report['classification']}")
        print(f"Wrote {report['jsonReportPath']}")
        print(f"Wrote {report['markdownReportPath']}")
        return 0 if report["classification"] == "PASS" else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
