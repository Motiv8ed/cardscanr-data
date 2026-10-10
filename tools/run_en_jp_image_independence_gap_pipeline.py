#!/usr/bin/env python3
"""Gap-only EN/JA image independence pipeline for newly released catalogue cards.

Does NOT re-acquire already-hosted printings. Flow:
  1) Detect EN/JA cards lacking CardScanR CDN binding and/or local master
  2) Run existing multisource resolver against unresolved only (--skip-hosted)
  3) Apply catalogue bindings from local master publicUrl
  4) Re-run regression gate; report failures fail-closed (no silent substitute)

Identity and printing substitution rules remain inside the existing resolvers.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "image_independence"
TOOLS = ROOT / "tools"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd: list[str]) -> int:
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", action="store_true", help="Upload new masters to R2")
    parser.add_argument("--apply", action="store_true", help="Apply CDN bindings to catalogue")
    parser.add_argument("--limit", type=int, default=0, help="Resolver limit (0=all gaps)")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Only detect/report gaps; do not acquire",
    )
    args = parser.parse_args()

    summary: dict = {
        "generatedAtUtc": utc_now(),
        "steps": [],
        "policy": {
            "skipAlreadyHosted": True,
            "noSilentPrintingSubstitution": True,
            "failClosedIdentity": True,
            "sellerLicenceNotInferredFromPublicAvailability": True,
        },
    }

    # 1) Detect gaps via closeout verifier (no CDN HTTP storm; hash optional skip)
    detect_cmd = [
        sys.executable,
        str(TOOLS / "verify_en_jp_image_independence_closeout.py"),
        "--skip-cdn",
        "--skip-hash",
        "--skip-backup",
    ]
    code = run(detect_cmd)
    summary["steps"].append({"step": "detect", "exitCode": code})
    result_path = REPORT / "closeout_verification_result.json"
    if result_path.is_file():
        summary["detect"] = json.loads(result_path.read_text(encoding="utf-8"))

    missing = int((summary.get("detect") or {}).get("missingCount") or 0)
    if args.report_only or missing == 0:
        summary["status"] = "no_gaps" if missing == 0 else "report_only"
        out = REPORT / "en_jp_gap_pipeline_summary.json"
        out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2))
        return 0 if missing == 0 else 2

    # 2) Acquire only unresolved (existing resolver skips hosted by default)
    resolve_cmd = [
        sys.executable,
        str(TOOLS / "image_independence_multisource_resolver.py"),
        "--skip-hosted",
    ]
    if args.upload:
        resolve_cmd.append("--upload")
    if args.limit:
        resolve_cmd.extend(["--limit", str(args.limit)])
    code = run(resolve_cmd)
    summary["steps"].append({"step": "resolve_gaps", "exitCode": code})
    if code != 0:
        summary["status"] = "resolve_failed"
        (REPORT / "en_jp_gap_pipeline_summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        return code

    # 3) Apply catalogue bindings from master
    if args.apply:
        apply_cmd = [
            sys.executable,
            str(TOOLS / "apply_image_independence_to_catalogue.py"),
        ]
        code = run(apply_cmd)
        summary["steps"].append({"step": "apply_catalogue", "exitCode": code})
        if code != 0:
            summary["status"] = "apply_failed"
            (REPORT / "en_jp_gap_pipeline_summary.json").write_text(
                json.dumps(summary, indent=2) + "\n", encoding="utf-8"
            )
            return code

    # 4) Regression gate (fail closed)
    gate_cmd = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_en_jp_image_independence_regression.py",
        "-q",
        "--tb=line",
    ]
    code = run(gate_cmd)
    summary["steps"].append({"step": "regression_gate", "exitCode": code})
    summary["status"] = "ok" if code == 0 else "regression_failed"
    (REPORT / "en_jp_gap_pipeline_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
