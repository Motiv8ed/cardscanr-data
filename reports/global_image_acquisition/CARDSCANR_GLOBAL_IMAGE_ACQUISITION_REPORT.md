# CardScanR Global Image Acquisition Report

Generated: `2026-10-13T08:23:01Z`  
Branch: `task/global-image-acquisition-20261013`  
Scope: isolated staging only — **no production master/CDN writes**

## Before (verified)

| Bucket | Count |
| --- | ---: |
| Production EN master files | 39558 |
| Production JA master files | 28453 |
| Staging verified files | 0 |
| Global catalogue printings | 117665 |
| Global printings with image URL candidate | 92817 |
| Global printings with no image candidate | 24848 |

URL/metadata candidates are **not** counted as coverage.

## After (verified on disk this run)

| Language | Verified staging display.webp |
| --- | ---: |
| de | 914 |
| es | 522 |
| es-419 | 493 |
| fr | 445 |
| id | 515 |
| it | 517 |
| pt-BR | 515 |
| th | 512 |
| zh-Hant | 513 |
| **Total newly staged unique printings** | **4946** |

Progress ledger: `{'skip_existing': 1845, 'ok': 3136, 'quarantine': 168}`

Integrity: missing_display=0, quarantine=0, duplicate_sha_groups=0

## Unresolved gaps (still need other sources / permissions)

See `before_coverage.json` → `globalCatalogueWithoutImageCandidate` and `ADDITIONAL_SOURCES_DISCOVERY.md`.
Notable zero-candidate languages: **ko**, **zh-Hans**.

## Rights

See `SOURCE_RIGHTS_ACCESS_MATRIX.md`. TCGdex staging tagged `staging_only_mirror_permission_pending`. Scrydex written auth retained for EN/JA (already complete in production).

## Next batch

See `RANKED_NEXT_OPPORTUNITIES.md`. Resume:

```powershell
cd D:\CardScanR_Data_worktrees\global-image-acq-20261013
$env:CARDSCANR_IMAGE_STAGING_ROOT = 'D:\CardScanR_Archive\image_staging\global_acq_20261013'
python tools\global_image_staging_acquire.py --execute --exclude-languages en,ja --concurrency 3 --min-interval 0.40
python tools\reconcile_global_image_universe.py
python tools\validate_staging_images_and_contact_sheets.py
python tools\build_global_image_acquisition_report_bundle.py
```

Do **not** promote staging to production CDN without written TCGdex clarification.

## Universe reconciliation

See `universe_reconciliation.json` / `.md`.

## Post-acquire validation

See `staging_validation_summary.json` and `contact_sheets/`.
