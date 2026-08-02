#!/usr/bin/env python3
"""Export residual worldwide catalogue rows from Supabase before cleanup."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

CATALOGUE_TABLES = [
    "franchises",
    "languages",
    "regions",
    "source_providers",
    "import_runs",
    "source_snapshots",
    "source_records",
    "eras",
    "series",
    "sets",
    "set_releases",
    "card_designs",
    "card_printings",
    "card_variants",
    "card_text_localisations",
    "abilities",
    "attacks",
    "card_images",
    "sealed_products",
    "accessories",
    "sealed_product_variants",
    "product_contents",
    "product_images",
    "marketplace_mappings",
    "image_validation_results",
    "image_acquisition_attempts",
    "record_provenance",
    "publication_runs",
    "publication_artifacts",
    "unresolved_items",
    "_cardscanr_mcp_sql_chunks",
]

USER_TABLES = [
    "user_profiles",
    "user_collections",
    "user_cards",
    "scan_sessions",
    "customer_sync_preferences",
    "customer_collection_items",
    "customer_binders",
    "customer_binder_memberships",
    "customer_sync_operations",
    "customer_sync_checkpoints",
    "pokemon_card_image_records",
    "card_image_manifests",
    "market_price_keys",
    "market_price_snapshots",
    "market_price_cache",
    "market_sold_listing_evidence",
    "market_price_refresh_jobs",
]


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_all(client: httpx.Client, table: str, page_size: int = 1000) -> list[dict]:
    rows: list[dict] = []
    start = 0
    while True:
        end = start + page_size - 1
        response = client.get(
            f"/rest/v1/{table}",
            headers={"Range-Unit": "items", "Range": f"{start}-{end}", "Prefer": "count=exact"},
        )
        if response.status_code in (404,):
            return rows
        if response.status_code not in (200, 206):
            raise RuntimeError(f"{table}: HTTP {response.status_code}: {response.text[:500]}")
        batch = response.json()
        if not isinstance(batch, list):
            raise RuntimeError(f"{table}: unexpected payload")
        rows.extend(batch)
        content_range = response.headers.get("content-range") or ""
        if len(batch) < page_size:
            break
        if "/" in content_range:
            total_s = content_range.split("/")[-1]
            if total_s.isdigit() and len(rows) >= int(total_s):
                break
        start += page_size
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--supabase-url", default=os.environ.get("SUPABASE_URL"))
    parser.add_argument(
        "--service-role-key-env",
        default="SUPABASE_SECRET_KEY",
        help="Env var holding the secret/service-role key",
    )
    args = parser.parse_args()
    if not args.supabase_url:
        print("missing SUPABASE_URL", file=sys.stderr)
        return 2
    key = os.environ.get(args.service_role_key_env) or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        print(f"missing key env {args.service_role_key_env}", file=sys.stderr)
        return 2

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    summary: dict = {
        "generatedAt": utc_iso(),
        "supabaseUrlHost": args.supabase_url.split("//", 1)[-1].split("/", 1)[0],
        "catalogue": {},
        "retained": {},
        "notes": [
            "Residual worldwide catalogue rows are a failed partial load of staging.",
            "Canonical catalogue remains local staging SQLite; this export is forensics only.",
        ],
    }
    with httpx.Client(base_url=args.supabase_url.rstrip("/"), headers=headers, timeout=120) as client:
        for table in CATALOGUE_TABLES:
            rows = fetch_all(client, table)
            path = out / f"{table}.jsonl.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            summary["catalogue"][table] = {"rows": len(rows), "file": path.name}
            print(f"catalogue {table}={len(rows)}", flush=True)
        for table in USER_TABLES:
            # Counts only for retained tables; do not dump user PII unless empty/small metadata.
            response = client.get(
                f"/rest/v1/{table}",
                params={"select": "count"},
                headers={"Prefer": "count=exact", "Range": "0-0"},
            )
            content_range = response.headers.get("content-range") or "*/0"
            total_s = content_range.split("/")[-1]
            count = int(total_s) if total_s.isdigit() else -1
            summary["retained"][table] = {"rows": count}
            print(f"retained {table}={count}", flush=True)

    (out / "RESIDUAL_EXPORT_SUMMARY.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({"ok": True, "output": str(out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
