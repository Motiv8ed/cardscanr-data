#!/usr/bin/env python3
from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

import httpx

OUT = Path(r"D:\CardScanR_backups\cloudflare_migration_prechange_20260803_074320\catalogue_residual_export")
TABLES = [
    ("source_records", "id"),
    ("card_designs", "id"),
    ("card_printings", "id"),
    ("sets", "id"),
    ("set_releases", "id"),
    ("series", "id"),
]


def main() -> int:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ.get("SUPABASE_SECRET_KEY") or os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}
    log = OUT / "large_ids.stdout.log"
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=url, headers=headers, timeout=180.0) as client, log.open("w", encoding="utf-8") as lh:
        for table, pk in TABLES:
            path = OUT / f"{table}.ids.jsonl.gz"
            rows = 0
            start = 0
            page = 1000
            total = None
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                while True:
                    end = start + page - 1
                    response = client.get(
                        f"/rest/v1/{table}",
                        params={"select": pk, "order": f"{pk}.asc"},
                        headers={"Range-Unit": "items", "Range": f"{start}-{end}", "Prefer": "count=exact"},
                    )
                    if response.status_code not in (200, 206):
                        raise RuntimeError(f"{table} HTTP {response.status_code}: {response.text[:300]}")
                    batch = response.json()
                    content_range = response.headers.get("content-range") or ""
                    if "/" in content_range:
                        total_s = content_range.split("/")[-1]
                        if total_s.isdigit():
                            total = int(total_s)
                    for row in batch:
                        handle.write(json.dumps(row, separators=(",", ":")) + "\n")
                        rows += 1
                    lh.write(f"{table} fetched={rows} total={total} range={content_range}\n")
                    lh.flush()
                    if not batch:
                        break
                    if total is not None and rows >= total:
                        break
                    if len(batch) == 0:
                        break
                    start += len(batch)
            lh.write(f"DONE {table}={rows} expected={total}\n")
            lh.flush()
            print(f"DONE {table}={rows} expected={total}", flush=True)
            if total is not None and rows != total:
                raise RuntimeError(f"{table} incomplete {rows}!={total}")
    print("ALL_OK", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        Path(OUT / "large_ids.stderr.log").write_text(repr(exc) + "\n", encoding="utf-8")
        print(repr(exc), file=sys.stderr)
        raise
