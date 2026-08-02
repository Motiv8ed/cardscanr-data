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
    parser.add_argument("--product-database", type=Path)
    parser.add_argument("--product-provider")
    args = parser.parse_args()
    if bool(args.product_database) != bool(args.product_provider):
        parser.error("--product-database and --product-provider must be supplied together")
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
    product_coverage = None
    if args.product_database:
        catalogue = sqlite3.connect(f"file:{args.product_database.resolve()}?mode=ro", uri=True)
        try:
            total = catalogue.execute(
                "select count(*) from sealed_product where provider_id=?", (args.product_provider,),
            ).fetchone()[0]
            verified = catalogue.execute(
                """select count(distinct sp.id)
                     from sealed_product sp
                     join sealed_product_variant spv on spv.sealed_product_id=sp.id
                     join product_image_candidate pic on pic.sealed_product_variant_id=spv.id
                    where sp.provider_id=?
                      and pic.validation_status in ('verified','acquired','published')""",
                (args.product_provider,),
            ).fetchone()[0]
            product_coverage = {
                "provider_id": args.product_provider,
                "total_products": total,
                "products_with_verified_image": verified,
                "products_without_verified_image": total - verified,
            }
            report["product_coverage"] = product_coverage
        finally:
            catalogue.close()
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
    if product_coverage:
        lines.extend([
            "", "## Product coverage", "",
            f"- Provider: `{product_coverage['provider_id']}`",
            f"- Total products: `{product_coverage['total_products']:,}`",
            f"- Products with at least one verified image: `{product_coverage['products_with_verified_image']:,}`",
            f"- Products without a verified image: `{product_coverage['products_without_verified_image']:,}`",
        ])
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
