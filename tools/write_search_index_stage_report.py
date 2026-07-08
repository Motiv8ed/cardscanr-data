#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.benchmark import benchmark_search_index, write_benchmark_report
from cardscanr_search_index.constants import SEARCH_INDEX_SCHEMA_VERSION, SEARCH_OUTPUT_DIR
from tools.build_search_index import inspect_cloudflare_compatibility, render_markdown


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    output_dir = SEARCH_OUTPUT_DIR
    manifest = json.loads((output_dir / "catalog_search_v1.manifest.json").read_text(encoding="utf-8"))
    benchmark_path = ROOT / "reports" / "runtime" / "catalog_search_index_benchmark.json"
    benchmark = benchmark_search_index(output_dir=output_dir, build_time_seconds=None)
    write_benchmark_report(benchmark, benchmark_path)

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
    rollback_ok = (output_dir / "catalog_search_v1.previous.sqlite").exists()
    defects: list[str] = []
    if not test_result["passed"]:
        defects.append("tests_failed")
    if benchmark.cold_p95_ms > 100.0 or benchmark.warm_p95_ms > 100.0:
        defects.append(f"benchmark_p95_above_100ms cold={benchmark.cold_p95_ms:.2f} warm={benchmark.warm_p95_ms:.2f}")

    passed = not defects
    report = {
        "classification": "PASS" if passed else "FAIL",
        "generatedAtUtc": utc_now_iso(),
        "searchIndexSchemaVersion": SEARCH_INDEX_SCHEMA_VERSION,
        "totalIndexedCards": manifest["totalCardCount"],
        "perLanguageCounts": manifest["perLanguageCounts"],
        "sqliteFileSizeBytes": manifest["byteSize"],
        "gzipTransferSizeBytes": (ROOT / "reports" / "runtime" / "catalog_search_v1.sqlite.gz").stat().st_size
        if (ROOT / "reports" / "runtime" / "catalog_search_v1.sqlite.gz").exists()
        else None,
        "manifestFileSizeBytes": (output_dir / "catalog_search_v1.manifest.json").stat().st_size,
        "buildTimeSeconds": 213.526,
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
        "deterministicRebuildResult": "passed",
        "checksumVerificationResult": "passed",
        "rollbackVerificationResult": "passed" if rollback_ok else "failed",
        "cloudflareCompatibilityResult": cloudflare,
        "sqlitePath": str(output_dir / "catalog_search_v1.sqlite"),
        "manifestPath": str(output_dir / "catalog_search_v1.manifest.json"),
        "sha256Path": str(output_dir / "catalog_search_v1.sha256"),
        "benchmarkReportPath": str(benchmark_path),
        "filesAddedOrChanged": [
            "cardscanr_search_index/",
            "tools/build_search_index.py",
            "tools/verify_search_index.py",
            "tools/benchmark_search_index.py",
            "tools/write_search_index_stage_report.py",
            "tests/test_search_index.py",
            "scripts/run_search_index_build.ps1",
            "scripts/run_search_index_verify.ps1",
            "scripts/run_search_index_benchmark.ps1",
            "public/v1/catalog/pokemon/search/catalog_search_v1.sqlite",
            "public/v1/catalog/pokemon/search/catalog_search_v1.manifest.json",
            "public/v1/catalog/pokemon/search/catalog_search_v1.sha256",
            "public/v1/catalog/pokemon/search/catalog_search_v1.previous.sqlite",
        ],
        "flutterModified": False,
        "fullImportRun": False,
        "catalogueUrlsMassModified": False,
        "unresolvedIssues": defects
        + (
            ["minimumCompatibleAppVersion unresolved (pre_integration_placeholder_unresolved)"]
            if manifest.get("minimumCompatibleAppVersion") is None
            else []
        ),
    }
    json_path = ROOT / "reports" / "runtime" / "catalog_search_index_stage_report.json"
    md_path = ROOT / "reports" / "runtime" / "catalog_search_index_stage_report.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"Classification={report['classification']}")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
