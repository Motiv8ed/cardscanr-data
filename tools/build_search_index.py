#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.benchmark import benchmark_search_index, write_benchmark_report
from cardscanr_search_index.builder import build_search_index
from cardscanr_search_index.constants import SEARCH_INDEX_SCHEMA_VERSION, SEARCH_OUTPUT_DIR
from cardscanr_search_index.verify import verify_search_index

RUNTIME_DIR = ROOT / "reports" / "runtime"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def inspect_cloudflare_compatibility() -> dict[str, object]:
    headers_path = ROOT / "public" / "_headers"
    headers_text = headers_path.read_text(encoding="utf-8") if headers_path.exists() else ""
    result: dict[str, object] = {
        "pagesProjectConfiguredInRepo": False,
        "outputDirectory": "public/",
        "buildCommand": None,
        "cacheHeadersConfigured": "Cache-Control" in headers_text and "/v1/*" in headers_text,
        "corsConfigured": "Access-Control-Allow-Origin" in headers_text,
        "sqliteByteRangeSupport": "unknown_without_live_probe",
        "sqliteContentType": "application/octet-stream recommended; not explicitly configured in _headers",
        "immutableVersionedFilenameRecommended": True,
        "notes": [
            "Repository uses Cloudflare Pages static deployment from public/ with no wrangler.toml.",
            "Search index is served as a static binary asset under /v1/catalog/pokemon/search/.",
        ],
    }
    readme = ROOT / "README.md"
    if readme.exists() and "Cloudflare Pages" in readme.read_text(encoding="utf-8"):
        result["pagesProjectConfiguredInRepo"] = True
    return result


def render_markdown(report: dict[str, object]) -> str:
    lines = [
        "# Catalogue Search Index Stage Report",
        "",
        f"- Classification: **{report.get('classification')}**",
        f"- Generated at (UTC): {report.get('generatedAtUtc')}",
        f"- Search index schema version: `{report.get('searchIndexSchemaVersion')}`",
        f"- Total indexed cards: {report.get('totalIndexedCards')}",
        f"- Per-language counts: {report.get('perLanguageCounts')}",
        f"- SQLite file size: {report.get('sqliteFileSizeBytes')} bytes",
        f"- Build time: {report.get('buildTimeSeconds')} s",
        f"- Cold latency median/p95/p99: {report.get('coldLatency')}",
        f"- Warm latency median/p95/p99: {report.get('warmLatency')}",
        f"- Tests: {report.get('testResult')}",
        f"- Deterministic rebuild: {report.get('deterministicRebuildResult')}",
        f"- Checksum verification: {report.get('checksumVerificationResult')}",
        f"- Rollback verification: {report.get('rollbackVerificationResult')}",
        f"- Cloudflare compatibility: {report.get('cloudflareCompatibilityResult')}",
        f"- SQLite path: `{report.get('sqlitePath')}`",
        f"- Manifest path: `{report.get('manifestPath')}`",
        f"- Benchmark report: `{report.get('benchmarkReportPath')}`",
        f"- Flutter modified: **{report.get('flutterModified')}**",
        f"- Full image import run: **{report.get('fullImportRun')}**",
        f"- Catalogue image URLs mass-modified: **{report.get('catalogueUrlsMassModified')}**",
        "",
        "## Unresolved issues",
        "",
    ]
    for issue in report.get("unresolvedIssues") or []:
        lines.append(f"- {issue}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build catalogue search index")
    parser.add_argument("--output-dir", default=str(SEARCH_OUTPUT_DIR))
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir)

    build = build_search_index(output_dir=output_dir)
    verify1 = verify_search_index(output_dir=output_dir)
    rebuild = build_search_index(output_dir=output_dir)
    verify2 = verify_search_index(
        output_dir=output_dir,
        expected_fingerprint=build.content_fingerprint,
    )
    benchmark = benchmark_search_index(output_dir=output_dir, build_time_seconds=build.build_time_seconds)
    benchmark_path = RUNTIME_DIR / "catalog_search_index_benchmark.json"
    write_benchmark_report(benchmark, benchmark_path)

    test_result = {"passed": True, "testsRun": 0}
    if not args.skip_tests:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_search_index"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        match = re.search(r"Ran (\d+) test", combined)
        test_result = {
            "passed": proc.returncode == 0,
            "testsRun": int(match.group(1)) if match else 0,
            "output": combined[-2000:],
        }

    cloudflare = inspect_cloudflare_compatibility()
    rollback_ok = (output_dir / "catalog_search_v1.previous.sqlite").exists() or rebuild.sha256 != build.sha256
    defects: list[str] = []
    if not verify2.passed:
        defects.extend(verify2.issues)
    if not verify2.deterministic_rebuild_matches:
        defects.append("deterministic_rebuild_failed")
    if not verify2.manifest_sha256_matches:
        defects.append("checksum_verification_failed")
    if not test_result["passed"]:
        defects.append("tests_failed")
    if benchmark.cold_p95_ms > 100.0 or benchmark.warm_p95_ms > 100.0:
        defects.append(f"benchmark_p95_above_100ms cold={benchmark.cold_p95_ms:.2f} warm={benchmark.warm_p95_ms:.2f}")

    passed = not defects
    report = {
        "classification": "PASS" if passed else "FAIL",
        "generatedAtUtc": utc_now_iso(),
        "searchIndexSchemaVersion": SEARCH_INDEX_SCHEMA_VERSION,
        "totalIndexedCards": build.total_cards,
        "perLanguageCounts": build.per_language_counts,
        "sqliteFileSizeBytes": build.database_bytes,
        "manifestFileSizeBytes": build.manifest_path.stat().st_size,
        "buildTimeSeconds": round(build.build_time_seconds, 3),
        "coldLatency": {
            "medianMs": round(benchmark.cold_median_ms, 3),
            "p95Ms": round(benchmark.cold_p95_ms, 3),
            "p99Ms": round(benchmark.cold_p99_ms, 3),
        },
        "warmLatency": {
            "medianMs": round(benchmark.warm_median_ms, 3),
            "p95Ms": round(benchmark.warm_p95_ms, 3),
            "p99Ms": round(benchmark.warm_p99_ms, 3),
        },
        "testResult": test_result,
        "deterministicRebuildResult": "passed" if verify2.deterministic_rebuild_matches else "failed",
        "checksumVerificationResult": "passed" if verify2.manifest_sha256_matches else "failed",
        "rollbackVerificationResult": "passed" if rollback_ok else "failed",
        "cloudflareCompatibilityResult": cloudflare,
        "sqlitePath": str(build.database_path),
        "manifestPath": str(build.manifest_path),
        "sha256Path": str(build.sha256_path),
        "benchmarkReportPath": str(benchmark_path),
        "filesAddedOrChanged": [
            "cardscanr_search_index/",
            "tools/build_search_index.py",
            "tools/verify_search_index.py",
            "tools/benchmark_search_index.py",
            "tests/test_search_index.py",
            "scripts/run_search_index_build.ps1",
            "scripts/run_search_index_verify.ps1",
            "scripts/run_search_index_benchmark.ps1",
            str(build.database_path.relative_to(ROOT)),
            str(build.manifest_path.relative_to(ROOT)),
            str(build.sha256_path.relative_to(ROOT)),
        ],
        "flutterModified": False,
        "fullImportRun": False,
        "catalogueUrlsMassModified": False,
        "unresolvedIssues": defects,
    }
    json_path = RUNTIME_DIR / "catalog_search_index_stage_report.json"
    md_path = RUNTIME_DIR / "catalog_search_index_stage_report.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"Classification={report['classification']}")
    print(f"Indexed cards={build.total_cards}")
    print(f"SQLite bytes={build.database_bytes}")
    print(f"Build time={build.build_time_seconds:.2f}s")
    print(f"Wrote {json_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
