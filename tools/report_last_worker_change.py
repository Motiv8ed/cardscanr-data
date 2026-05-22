#!/usr/bin/env python3
"""Print a compact summary of the latest worker-related change."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
V1 = ROOT / "public" / "v1"
PROVIDER_STATUS = V1 / "provider-catalog" / "pokewallet" / "status.json"
IMAGE_MANIFEST = V1 / "images" / "cards-manifest.json"
PRICES_CURRENT = V1 / "prices" / "current" / "pokemon"
CYCLE_REPORT = ROOT / "reports" / "latest_pokewallet_worker_cycle.json"


def run_git(args: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    return completed.stdout


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else None


def commit_header(commit: str) -> dict[str, str]:
    out = run_git(["show", "-s", "--format=%H%n%h%n%ci%n%s", commit]).strip().splitlines()
    while len(out) < 4:
        out.append("")
    return {
        "hash": out[0],
        "short": out[1],
        "date": out[2],
        "subject": out[3],
    }


def changed_files(commit: str) -> list[str]:
    out = run_git(["show", "--name-only", "--pretty=format:", commit])
    return [line.strip() for line in out.splitlines() if line.strip()]


def numstat(commit: str) -> tuple[int, int]:
    out = run_git(["show", "--numstat", "--pretty=format:", commit])
    insertions = 0
    deletions = 0
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 2:
            continue
        if parts[0].isdigit():
            insertions += int(parts[0])
        if parts[1].isdigit():
            deletions += int(parts[1])
    return insertions, deletions


def provider_totals_by_language() -> dict[str, int]:
    status = load_json(PROVIDER_STATUS) or {}
    languages = status.get("languages")
    if not isinstance(languages, dict):
        return {}
    totals: dict[str, int] = {}
    for lang, payload in sorted(languages.items()):
        if isinstance(payload, dict):
            totals[str(lang)] = int(payload.get("cardCount") or 0)
    return totals


def image_manifest_count() -> int:
    manifest = load_json(IMAGE_MANIFEST) or {}
    count = manifest.get("recordCount")
    if isinstance(count, int):
        return count
    records = manifest.get("records")
    if isinstance(records, list):
        return len(records)
    return 0


def price_record_count_by_language() -> dict[str, int]:
    counts: dict[str, int] = {}
    if not PRICES_CURRENT.exists():
        return counts
    for lang_dir in sorted(path for path in PRICES_CURRENT.iterdir() if path.is_dir()):
        lang = lang_dir.name
        total = 0
        for path in sorted(lang_dir.glob("*.json")):
            if path.name == "status.json":
                continue
            data = load_json(path)
            if not data:
                continue
            records = data.get("records")
            if isinstance(records, list):
                total += len(records)
        counts[lang] = total
    return counts


def file_group_counts(paths: list[str]) -> dict[str, int]:
    groups = Counter()
    for path in paths:
        if path.startswith("public/v1/provider-catalog/pokewallet/cards/"):
            groups["provider_cards"] += 1
        elif path.startswith("public/v1/provider-catalog/pokewallet/"):
            groups["provider_summaries"] += 1
        elif path.startswith("public/v1/diagnostics/"):
            groups["diagnostics"] += 1
        elif path == "public/v1/index.json":
            groups["index"] += 1
        elif path.startswith("public/v1/prices/"):
            groups["prices"] += 1
        elif path.startswith("public/v1/images/"):
            groups["images"] += 1
        elif path.startswith("data/"):
            groups["data_state"] += 1
        else:
            groups["other"] += 1
    return dict(sorted(groups.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Report latest worker change summary.")
    parser.add_argument("--commit", default="HEAD", help="Commit hash or ref to inspect (default: HEAD)")
    args = parser.parse_args()

    header = commit_header(args.commit)
    files = changed_files(args.commit)
    insertions, deletions = numstat(args.commit)
    groups = file_group_counts(files)
    provider_totals = provider_totals_by_language()
    image_count = image_manifest_count()
    price_counts = price_record_count_by_language()
    cycle_report = load_json(CYCLE_REPORT) or {}
    validation = cycle_report.get("validationResult", "unknown")

    print("Last worker change summary")
    print("=" * 32)
    print(f"commit: {header['short']} ({header['hash']})")
    print(f"date:   {header['date']}")
    print(f"title:  {header['subject']}")
    print(f"files changed: {len(files)}")
    print(f"insertions/deletions: +{insertions} / -{deletions}")
    print("file groups:")
    for key, value in groups.items():
        print(f"  {key}: {value}")
    print("provider card totals by language:")
    if provider_totals:
        for lang, value in provider_totals.items():
            print(f"  {lang}: {value}")
    else:
        print("  none")
    print(f"image manifest records: {image_count}")
    print("current price records by language:")
    if price_counts:
        for lang, value in sorted(price_counts.items()):
            print(f"  {lang}: {value}")
    else:
        print("  none")
    print(f"validation status (from latest cycle report): {validation}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
