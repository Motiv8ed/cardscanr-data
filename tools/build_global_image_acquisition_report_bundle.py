#!/usr/bin/env python3
"""Build coverage report + ZIP evidence bundle in Downloads."""

from __future__ import annotations

import argparse
import json
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "reports" / "global_image_acquisition"
DEFAULT_LIVE = Path(r"D:\CardScanR_Data\cardscanr-data")
DEFAULT_STAGING = Path(r"D:\CardScanR_Archive\image_staging\global_acq_20261013")
DOWNLOADS = Path.home() / "Downloads"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_progress(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.open(encoding="utf-8"):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def verify_staging(staging_root: Path) -> dict:
    by_lang = Counter()
    sha_dupes = Counter()
    missing_display = 0
    quarantine = 0
    for asset in staging_root.rglob("asset.json"):
        if "_quarantine" in asset.parts:
            quarantine += 1
            continue
        meta = json.loads(asset.read_text(encoding="utf-8"))
        display = asset.parent / "display.webp"
        if not display.exists():
            missing_display += 1
            continue
        by_lang[str(meta.get("language") or "?")] += 1
        sha = str(meta.get("sha256") or "")
        if sha:
            sha_dupes[sha] += 1
    duplicate_sha_groups = sum(1 for n in sha_dupes.values() if n > 1)
    return {
        "verifiedDisplayFilesByLanguage": dict(sorted(by_lang.items())),
        "verifiedDisplayFilesTotal": sum(by_lang.values()),
        "missingDisplayBesideAssetJson": missing_display,
        "quarantineSidecars": quarantine,
        "duplicateContentShaGroups": duplicate_sha_groups,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-root", type=Path, default=DEFAULT_LIVE)
    parser.add_argument("--staging-root", type=Path, default=DEFAULT_STAGING)
    args = parser.parse_args()
    REPORT.mkdir(parents=True, exist_ok=True)
    staging = args.staging_root
    before_path = REPORT / "before_coverage.json"
    before = json.loads(before_path.read_text(encoding="utf-8")) if before_path.exists() else {}
    progress = load_progress(staging / "progress.jsonl")
    status = Counter(r.get("status") for r in progress)
    ok_langs = Counter(r.get("language") for r in progress if r.get("status") == "ok")
    verified = verify_staging(staging)
    after = {
        "generatedAtUtc": utc_now(),
        "productionMasterVerifiedFiles": before.get("productionMasterVerifiedFiles"),
        "stagingVerifiedFilesAfter": verified["verifiedDisplayFilesByLanguage"],
        "stagingVerifiedTotal": verified["verifiedDisplayFilesTotal"],
        "progressStatusCounts": dict(status),
        "progressOkByLanguage": dict(sorted(ok_langs.items())),
        "integrity": {
            "missingDisplayBesideAssetJson": verified["missingDisplayBesideAssetJson"],
            "quarantineSidecars": verified["quarantineSidecars"],
            "duplicateContentShaGroups": verified["duplicateContentShaGroups"],
        },
        "globalCatalogueWithoutImageCandidate": before.get("globalCatalogueWithoutImageCandidate"),
        "notes": [
            "Staging files are verified on disk with asset.json provenance.",
            "Production promotion of TCGdex staging assets is NOT performed.",
            "EN/JA production master remains 100% from prior independence work.",
        ],
    }
    (REPORT / "after_coverage.json").write_text(json.dumps(after, indent=2), encoding="utf-8")

    newly = verified["verifiedDisplayFilesTotal"]
    md = f"""# CardScanR Global Image Acquisition Report

Generated: `{after['generatedAtUtc']}`  
Branch: `task/global-image-acquisition-20261013`  
Scope: isolated staging only — **no production master/CDN writes**

## Before (verified)

| Bucket | Count |
| --- | ---: |
| Production EN master files | {(before.get('productionMasterVerifiedFiles') or {}).get('en', 0)} |
| Production JA master files | {(before.get('productionMasterVerifiedFiles') or {}).get('ja', 0)} |
| Staging verified files | 0 |
| Global catalogue printings | {sum((before.get('globalCataloguePrintingsByLanguage') or {}).values())} |
| Global printings with image URL candidate | {sum((before.get('globalCatalogueWithImageCandidate') or {}).values())} |
| Global printings with no image candidate | {sum((before.get('globalCatalogueWithoutImageCandidate') or {}).values())} |

URL/metadata candidates are **not** counted as coverage.

## After (verified on disk this run)

| Language | Verified staging display.webp |
| --- | ---: |
"""
    for lang, n in sorted(verified["verifiedDisplayFilesByLanguage"].items()):
        md += f"| {lang} | {n} |\n"
    md += f"| **Total newly staged unique printings** | **{newly}** |\n"
    md += f"""
Progress ledger: `{dict(status)}`

Integrity: missing_display={verified['missingDisplayBesideAssetJson']}, quarantine={verified['quarantineSidecars']}, duplicate_sha_groups={verified['duplicateContentShaGroups']}

## Unresolved gaps (still need other sources / permissions)

See `before_coverage.json` → `globalCatalogueWithoutImageCandidate` and `ADDITIONAL_SOURCES_DISCOVERY.md`.
Notable zero-candidate languages: **ko**, **zh-Hans**.

## Rights

See `SOURCE_RIGHTS_ACCESS_MATRIX.md`. TCGdex staging tagged `staging_only_mirror_permission_pending`. Scrydex written auth retained for EN/JA (already complete in production).

## Next batch

See `RANKED_NEXT_OPPORTUNITIES.md`. Resume:

```powershell
cd D:\\CardScanR_Data_worktrees\\global-image-acq-20261013
$env:CARDSCANR_IMAGE_STAGING_ROOT = 'D:\\CardScanR_Archive\\image_staging\\global_acq_20261013'
python tools\\global_image_staging_acquire.py --execute --exclude-languages en,ja --concurrency 3 --min-interval 0.40
python tools\\reconcile_global_image_universe.py
python tools\\validate_staging_images_and_contact_sheets.py
python tools\\build_global_image_acquisition_report_bundle.py
```

Do **not** promote staging to production CDN without written TCGdex clarification.
"""
    recon = REPORT / "universe_reconciliation.json"
    if recon.exists():
        md += "\n## Universe reconciliation\n\nSee `universe_reconciliation.json` / `.md`.\n"
    val = REPORT / "staging_validation_summary.json"
    if val.exists():
        md += "\n## Post-acquire validation\n\nSee `staging_validation_summary.json` and `contact_sheets/`.\n"
    (REPORT / "CARDSCANR_GLOBAL_IMAGE_ACQUISITION_REPORT.md").write_text(md, encoding="utf-8")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    zip_path = DOWNLOADS / f"CardScanR_global_image_acquisition_{stamp}.zip"
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(REPORT.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=f"reports/global_image_acquisition/{path.relative_to(REPORT).as_posix()}")
        # Include a small sample of staging provenance (not full binaries)
        sample_assets = list(staging.rglob("asset.json"))[:40]
        for asset in sample_assets:
            if "_quarantine" in asset.parts:
                continue
            zf.write(asset, arcname=f"staging_samples/{asset.relative_to(staging).as_posix()}")
        # Progress summary only (full progress may be large)
        prog = staging / "progress.jsonl"
        if prog.exists():
            zf.write(prog, arcname="staging/progress.jsonl")
    manifest = {
        "generatedAtUtc": utc_now(),
        "zipPath": str(zip_path),
        "verifiedStagingTotal": newly,
        "reportDir": str(REPORT),
    }
    (REPORT / "evidence_bundle_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
