#!/usr/bin/env python3
"""Publish catalogue search index contract files to R2 and Cloudflare Pages."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cardscanr_search_index.constants import SEARCH_OUTPUT_DIR
from cardscanr_search_index.publication import (
    PublicationReport,
    load_publication_config,
    publish_search_index,
    resolve_publication_config,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish search index manifest to Pages and SQLite to R2.")
    parser.add_argument("--output-dir", default=str(SEARCH_OUTPUT_DIR))
    parser.add_argument("--config", default=None, help="Path to cloudflare_env.local.json")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Plan publication without writes (default).")
    parser.add_argument("--execute", action="store_true", help="Perform uploads and write manifest files.")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-live-verification", action="store_true")
    return parser.parse_args(argv)


def render_markdown(report: PublicationReport) -> str:
    data = report.to_dict()
    lines = [
        "# Search Index Publication Report",
        "",
        f"- Classification: **{data['classification']}**",
        f"- Dry run: {data['dryRun']}",
        f"- R2 bucket: `{data.get('r2Bucket')}`",
        f"- Public read policy: {data.get('publicReadPolicyResult')}",
        f"- Current object key: `{data.get('immutableCurrentObjectKey')}`",
        f"- Previous object key: `{data.get('immutablePreviousObjectKey')}`",
        f"- Pages manifest URL: {data.get('pagesManifestUrl')}",
        f"- R2 database URL: {data.get('r2DatabaseUrl')}",
        f"- SHA-256: `{data.get('sha256')}`",
        f"- Byte size: {data.get('byteSize')}",
        f"- R2 Content-Type: {data.get('r2ContentType')}",
        f"- R2 Cache-Control: {data.get('r2CacheControl')}",
        f"- Range request result: {data.get('rangeRequestResult')}",
        f"- Complete download checksum: {data.get('completeDownloadChecksumResult')}",
        f"- SQLite health: {data.get('sqliteHealthResult')}",
        f"- Pages deployment: {data.get('pagesDeploymentResult')}",
        f"- Tests: {data.get('testsResult')}",
        f"- Rollback: {data.get('rollbackResult')}",
        f"- Flutter modified: **{data.get('flutterModified')}**",
        f"- Full image import run: **{data.get('fullImageImportRun')}**",
        "",
        "## Files added or changed",
        "",
    ]
    for item in data.get("filesAddedOrChanged") or []:
        lines.append(f"- `{item}`")
    lines.extend(["", "## Unresolved issues", ""])
    unresolved = data.get("unresolvedIssues") or []
    if unresolved:
        for issue in unresolved:
            lines.append(f"- {issue}")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dry_run = not args.execute
    config = (
        load_publication_config(Path(args.config))
        if args.config
        else resolve_publication_config(root=ROOT)
    )
    report = publish_search_index(
        output_dir=Path(args.output_dir),
        config=config,
        root=ROOT,
        dry_run=dry_run,
        skip_tests=args.skip_tests,
        skip_live_verification=args.skip_live_verification,
    )

    report_path = ROOT / "reports" / "runtime" / "catalog_search_index_publication_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path = report_path.with_suffix(".md")
    md_path.write_text(render_markdown(report), encoding="utf-8")

    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    print(f"Wrote {report_path}")
    return 0 if report.classification == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
