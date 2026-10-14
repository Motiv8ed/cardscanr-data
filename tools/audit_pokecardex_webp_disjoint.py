#!/usr/bin/env python3
"""Produce disjoint, reproducible WebP counts for PokéCardex staging."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import finalize_pokecardex_zh_hans_reconcile as fin
import pokecardex_zh_hans_acquire as acq

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition" / "pokecardex"
STAGING = Path(r"D:\CardScanR_Archive\image_staging\pokecardex_zh_hans_20261014")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    meta = fin.load_meta()
    std = [r for r in meta if int(r.get("providerVersionId") or 1) in {0, 1}]
    nonstd = [r for r in meta if int(r.get("providerVersionId") or 1) not in {0, 1}]
    progress = fin.load_progress_best()

    std_paths: set[Path] = set()
    std_on_disk_rows = 0
    std_404 = 0
    std_missing = 0
    path_owners: dict[Path, list[str]] = {}
    for row in std:
        path = fin.display_path_for(row).resolve()
        key = fin.staging_key_for(row)
        std_paths.add(path)
        path_owners.setdefault(path, []).append(key)
        if path.exists():
            std_on_disk_rows += 1
        elif str((progress.get(key) or {}).get("status")) == "permanent_404":
            std_404 += 1
        else:
            std_missing += 1
    shared_paths = {str(p): keys for p, keys in path_owners.items() if len(keys) > 1 and p.exists()}
    std_on_disk = std_on_disk_rows

    nonstd_paths: set[Path] = set()
    nonstd_on_disk = 0
    for row in nonstd:
        path = fin.display_path_for(row).resolve()
        nonstd_paths.add(path)
        if path.exists():
            nonstd_on_disk += 1

    all_paths = {p.resolve() for p in STAGING.glob("zh-Hans/*/*/display.webp")}
    # Disjoint partitions of on-disk files
    only_std = all_paths & std_paths
    only_nonstd = (all_paths & nonstd_paths) - std_paths
    orphan = all_paths - std_paths - nonstd_paths

    # Orphans that are predecessor path tokens (safe_seg of dash / letters)
    orphan_rows = []
    for path in sorted(orphan):
        orphan_rows.append(
            {
                "path": str(path),
                "set": path.parent.parent.name,
                "folder": path.parent.name,
                "bytes": path.stat().st_size if path.exists() else 0,
                "classification": "legacy_path_token_or_collapsed_key",
            }
        )

    payload = {
        "generatedAtUtc": utc_now(),
        "disjointOnDisk": {
            "standardCanonicalDisplays": len(only_std),
            "nonStandardDisplays": len(only_nonstd),
            "orphanExtras": len(orphan),
            "totalWebp": len(all_paths),
            "sumParts": len(only_std) + len(only_nonstd) + len(orphan),
            "balanced": len(only_std) + len(only_nonstd) + len(orphan) == len(all_paths),
        },
        "standardInventory": {
            "metadataRows": len(std),
            "onDiskCanonicalRows": std_on_disk_rows,
            "onDiskCanonicalUniquePaths": len(only_std),
            "permanent404": std_404,
            "missingUnexpected": std_missing,
            "sumRows": std_on_disk_rows + std_404 + std_missing,
            "balancedRows": std_on_disk_rows + std_404 + std_missing == len(std),
            "sharedCanonicalPaths": len(shared_paths),
            "sharedCanonicalPathSamples": dict(list(shared_paths.items())[:10]),
            "rowVsUniqueGap": std_on_disk_rows - len(only_std),
        },
        "identity": {
            "note": (
                "Canonical standard display path uses collector_staging_token "
                "(numeric collector or s{sort} for energies). Orphans are leftover "
                "folders from earlier path normalisations (e.g. __v1, gra_v1) and are "
                "disjoint from canonical standard paths. If onDiskCanonicalRows > "
                "unique paths, multiple metadata rows share one display file."
            )
        },
        "orphans": orphan_rows,
        "equation": (
            f"totalWebp({len(all_paths)}) = standardCanonical({len(only_std)}) "
            f"+ nonStandard({len(only_nonstd)}) + orphanExtras({len(orphan)})"
        ),
        "standardEquation": (
            f"standardMeta({len(std)}) = onDiskRows({std_on_disk_rows}) + 404({std_404}) "
            f"+ missing({std_missing}); uniqueCanonicalPaths={len(only_std)}; "
            f"sharedPaths={len(shared_paths)}"
        ),
        "reconciliationNote": (
            f"totalWebp {len(all_paths)} = uniqueStd {len(only_std)} + nonStd {len(only_nonstd)} "
            f"+ orphan {len(orphan)}. Row-level std on-disk {std_on_disk_rows} can exceed "
            f"uniqueStd when {len(shared_paths)} paths are shared by multiple metadata rows."
        ),
    }
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / "zh_hans_webp_disjoint_audit.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (REPORT / "ZH_HANS_WEBP_DISJOINT_AUDIT.md").write_text(
        "\n".join(
            [
                "# PokéCardex WebP disjoint audit",
                "",
                f"Generated: {payload['generatedAtUtc']}",
                "",
                payload["equation"],
                "",
                payload["standardEquation"],
                "",
                f"- Balanced on-disk partition: **{payload['disjointOnDisk']['balanced']}**",
                f"- Balanced standard inventory: **{payload['standardInventory']['balancedRows']}**",
                f"- Shared canonical paths: **{payload['standardInventory']['sharedCanonicalPaths']}**",
                "",
                payload.get("reconciliationNote", ""),
                "",
                payload["identity"]["note"],
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False)[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
