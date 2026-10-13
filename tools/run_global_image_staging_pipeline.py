#!/usr/bin/env python3
"""Operator entry for worldwide staging image acquisition (gap-oriented).

Does not touch production master/CDN. Reuses global direct_images.jsonl and
global_image_staging_acquire.py.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
REPORT = ROOT / "reports" / "global_image_acquisition"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd: list[str]) -> int:
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-root", default=r"D:\CardScanR_Data\cardscanr-data")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--languages", default="")
    parser.add_argument("--exclude-languages", default="en,ja")
    args = parser.parse_args()
    REPORT.mkdir(parents=True, exist_ok=True)

    base = [
        sys.executable,
        str(TOOLS / "global_image_staging_acquire.py"),
        "--live-root",
        args.live_root,
    ]
    if args.report_only or not args.execute:
        code = run([*base, "--report-only"])
        if code != 0 or not args.execute:
            return code

    cmd = [
        *base,
        "--execute",
        "--concurrency",
        str(args.concurrency),
        "--exclude-languages",
        args.exclude_languages,
    ]
    if args.languages:
        cmd.extend(["--languages", args.languages])
    if args.limit:
        cmd.extend(["--limit", str(args.limit)])
    code = run(cmd)
    summary_path = REPORT / "staging_acquire_summary.json"
    envelope = {
        "generatedAtUtc": utc_now(),
        "exitCode": code,
        "summaryPath": str(summary_path) if summary_path.exists() else None,
        "productionPromotion": "forbidden_in_this_pipeline",
    }
    (REPORT / "pipeline_run_envelope.json").write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
