# CardScanR Global Image Acquisition Report

Generated: `2026-10-13T07:38:09Z`  
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
| de | 661 |
| es | 207 |
| es-419 | 205 |
| fr | 205 |
| id | 204 |
| it | 205 |
| pt-BR | 204 |
| th | 203 |
| zh-Hant | 204 |
| **Total newly staged unique printings** | **2298** |

Progress ledger: `{'ok': 790, 'skip_existing': 1820, 'fail': 2}`

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
python tools\global_image_staging_acquire.py --execute --exclude-languages en,ja --concurrency 8
```
