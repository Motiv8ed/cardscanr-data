#!/usr/bin/env python3
"""Report language pack coverage from production manifest and optional local sqlite."""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

MANIFEST_URL = (
    "https://assets.cardscanr.com/v2/catalog/pokemon/packs/active/"
    "catalogue.packs.manifest.json"
)


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    manifest = fetch_json(MANIFEST_URL)
    print("Catalogue language pack health")
    print(f"release: {manifest.get('catalogueReleaseId')}")
    print(f"generatedAt: {manifest.get('generatedAt')}")
    print()
    print(f"{'Language':<12} {'PackId':<14} {'Cards':>8} {'Bytes':>12} Status")
    print("-" * 60)
    for pack in manifest.get("packs", []):
        if pack.get("kind") != "language":
            continue
        languages = pack.get("languages") or []
        lang = languages[0] if languages else pack.get("packId", "")
        print(
            f"{lang:<12} {pack.get('packId', ''):<14} "
            f"{pack.get('recordCount', 0):>8} "
            f"{pack.get('rawSqliteBytes', 0):>12} published"
        )
    optional = manifest.get("optionalPackIds") or []
    print()
    print("optionalPackIds:", ", ".join(optional))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
