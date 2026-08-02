#!/usr/bin/env python3
"""Report a resumable image-validation checkpoint without exposing transient fetch URLs."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.checkpoint.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        status = {row["status"]: row["rows"] for row in connection.execute(
            "select status,count(*) rows from assets group by status order by status"
        )}
        providers = [dict(row) for row in connection.execute(
            """select c.provider_id,a.status,count(distinct a.source_url) distinct_urls,
                      count(distinct c.candidate_id) candidates
                 from candidates c join assets a on a.source_url=c.source_url
                group by c.provider_id,a.status order by c.provider_id,a.status"""
        )]
        failures = [dict(row) for row in connection.execute(
            """select status,error,count(*) rows from assets where status in ('fail','not_found','retryable_error')
                group by status,error order by rows desc,status,error"""
        )]
        cache = dict(connection.execute(
            """select count(distinct sha256) cached_objects,coalesce(sum(byte_size),0) observed_bytes
                 from assets where status='pass'"""
        ).fetchone())
        report = {
            "schema_version": 1, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "checkpoint": str(args.checkpoint.resolve()), "status": status, "providers": providers,
            "failure_classes": failures, "cache": cache,
            "security_note": "Transient fetch_url values are intentionally excluded from this report.",
        }
    finally:
        connection.close()
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    lines = [
        "# Image validation checkpoint", "", f"- Checkpoint: `{report['checkpoint']}`",
        f"- Cached objects: `{cache['cached_objects']}`", f"- Observed bytes: `{cache['observed_bytes']}`", "",
        "## Asset outcomes", "", "| Outcome | Distinct URLs |", "|---|---:|",
    ]
    lines.extend(f"| {key} | {value:,} |" for key, value in status.items())
    lines.extend(["", "## Provider outcomes", "", "| Provider | Outcome | URLs | Candidates |", "|---|---|---:|---:|"])
    lines.extend(
        f"| {row['provider_id']} | {row['status']} | {row['distinct_urls']:,} | {row['candidates']:,} |"
        for row in providers
    )
    lines.extend(["", "## Failure classes", "", "| Outcome | Error | URLs |", "|---|---|---:|"])
    lines.extend(
        f"| {row['status']} | {(row['error'] or '').replace('|', '\\|')} | {row['rows']:,} |"
        for row in failures
    )
    lines.extend(["", report["security_note"]])
    args.markdown.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": status, "providers": len(providers), "cache": cache}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

