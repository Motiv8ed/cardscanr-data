#!/usr/bin/env python3
"""Report EN current-price Stage 1 migration progress."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EN_CURRENT_DIR = ROOT / "public" / "v1" / "prices" / "current" / "pokemon" / "en"
STAGE1_EN_CURRENT_PRICE_ADDITIVE_FIELDS = {
    "canonicalCardId",
    "priceIdentityId",
    "market",
    "country",
    "sourceCurrency",
    "targetCurrency",
    "conversionPolicy",
    "status",
    "confidence",
    "diagnostics",
}


def load_json(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            return payload
    except (OSError, json.JSONDecodeError):
        return None
    return None


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def main() -> None:
    if not EN_CURRENT_DIR.exists():
        print("EN current price directory not found:", EN_CURRENT_DIR)
        sys.exit(1)

    set_files = sorted(p for p in EN_CURRENT_DIR.glob("*.json") if p.name != "status.json")
    if not set_files:
        print("No EN current price set files found.")
        sys.exit(0)

    total_records = 0
    stage1_records = 0
    legacy_records = 0
    partial_stage1_records = 0

    fully_migrated_set_files = []
    legacy_only_set_files = []
    mixed_set_files = []
    legacy_set_files_for_regen = []

    for set_file in set_files:
        data = load_json(set_file)
        prices = data.get("prices") if isinstance(data, dict) else []
        if not isinstance(prices, list):
            prices = []

        file_stage1 = 0
        file_legacy = 0
        file_partial = 0

        for entry in prices:
            if not isinstance(entry, dict):
                continue
            total_records += 1
            present_stage1_fields = STAGE1_EN_CURRENT_PRICE_ADDITIVE_FIELDS & set(entry.keys())
            if present_stage1_fields == STAGE1_EN_CURRENT_PRICE_ADDITIVE_FIELDS:
                stage1_records += 1
                file_stage1 += 1
            elif not present_stage1_fields:
                legacy_records += 1
                file_legacy += 1
            else:
                partial_stage1_records += 1
                file_partial += 1

        if file_stage1 > 0 and file_legacy == 0 and file_partial == 0:
            fully_migrated_set_files.append(rel(set_file))
        elif file_legacy > 0 and file_stage1 == 0 and file_partial == 0:
            legacy_only_set_files.append(rel(set_file))
        else:
            mixed_set_files.append(rel(set_file))

        if file_legacy > 0:
            legacy_set_files_for_regen.append(rel(set_file))

    migrated_percent = (stage1_records / total_records * 100.0) if total_records else 0.0

    print("EN current price migration progress")
    print(f"- total EN current price files: {len(set_files)}")
    print(f"- total EN current price records: {total_records}")
    print(f"- legacy record count: {legacy_records}")
    print(f"- Stage 1 record count: {stage1_records}")
    print(f"- percentage migrated: {migrated_percent:.2f}%")
    print(f"- fully migrated set files: {len(fully_migrated_set_files)}")
    print(f"- legacy-only set files: {len(legacy_only_set_files)}")
    print(f"- mixed set files: {len(mixed_set_files)}")
    print("- first 10 legacy set files still needing regeneration:")
    for path in legacy_set_files_for_regen[:10]:
        print(f"  - {path}")

    if partial_stage1_records > 0:
        print(
            "- note: found "
            f"{partial_stage1_records} record(s) with partial Stage 1 fields (neither legacy nor fully Stage 1)"
        )


if __name__ == "__main__":
    main()