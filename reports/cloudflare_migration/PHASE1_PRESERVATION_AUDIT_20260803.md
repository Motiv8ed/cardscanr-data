# Phase 1 preservation audit — Cloudflare catalogue migration

Generated: `2026-08-03T07:45:00Z` (approx local start)

## Owner completion package

Path: `C:\Users\andyg\Downloads\CardScanR_Worldwide_Catalogue_Completion_20260802_235816`  
Terminal status in package: `WORLDWIDE_CATALOGUE_EXTERNAL_BLOCKERS_REMAIN`

Reports read: README, CONTINUATION_STATUS, SUPABASE_LOAD_DISK_BLOCKER, PUBLICATION_CANARY_CANARY2 (+JSON), RELEASE_GATE_AUDIT, FLUTTER_COMPAT, EXTERNAL_BLOCKER_FINALIZATION, and remaining classification reports in the package.

## Worktrees / HEADs

| Tree | Path | Branch / state |
|---|---|---|
| Data | `D:\CardScanR_worktrees\worldwide_catalogue_products_20260802` | `feature/worldwide-pokemon-catalogue-products-20260802` @ `28b489c9…`; dirty: `reports/global_rollout/global_search_index.json` |
| Flutter worldwide | `D:\CardScanR_worktrees\flutter_worldwide_catalogue_20260802` | **Non-git copy** (no `.git`); schema 2.1.0/2.2.0 already accepted |
| Original Flutter | `D:\Card Scanner App\card_scanner_app` | `main` @ `b5149b55…`; left untouched |
| Runtime | `D:\CardScanR_worldwide_runtime_20260802` | Not a git repo |
| Main data checkout | `D:\cardscanr-data` | `main` @ `94fe6a41…`; dirty `.gitignore` only |

## Staging SQLite

- Path: `D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite`
- Bytes: `3,267,706,880`
- SHA-256: `24bb6bf7a8776435164b20059cc93c9622f4bed7b7d45c0012fb83bfb666ff24`
- Tables: 27 staging tables (singular names), including `card_printing` 296,463 / `sealed_product` 4,618
- WAL present but empty; shm present (not actively written by a collector at audit time)

## R2 / manifests

- Buckets: `cardscanr-catalog`, `cardscanr-card-images`
- Canary2 DB HEAD: **200**, `897,101,824` bytes, `Cache-Control: public, max-age=31536000, immutable`, content-type `application/vnd.sqlite3`
- SHA-256: `89c07376b30e9b0edf8ee1ad74c8b53583dc12a11f5f3fb71ec5d8419db5428b`
- Production active v2 manifest `…/catalogue.manifest.json`: **404** (never activated — production pointer unchanged)
- `images.cardscanr.com`: DNS NXDOMAIN; live image host remains `cardscanr-images.andygore149.workers.dev` / r2.dev

## Supabase live verification (do not trust prior “truncated” claim)

Project `qstcdlczasmvexpgbpjk` status: `ACTIVE_HEALTHY`  
Database size before cleanup: **857 MB** (`899,026,067` bytes)  
Writes: verified via temporary write probe (project not read-only)

Exact residual worldwide catalogue rows (NOT empty):

| Table | Rows |
|---|---:|
| source_records | 240,088 |
| card_designs | 188,833 |
| card_printings | 96,500 |
| set_releases | 3,218 |
| sets | 1,952 |
| series | 68 |
| import_runs | 50 |
| source_snapshots | 47 |
| regions | 29 |
| source_providers | 28 |
| languages | 17 |
| franchises | 1 |
| all other catalogue foundation tables | 0 |

Retained application counts at audit:

| Table | Rows |
|---|---:|
| user_profiles | 3 |
| user_collections | 3 |
| user_cards | 60 |
| customer_* | 0 |
| pokemon_card_image_records | 607 |
| card_image_manifests | 23,742 |

FK analysis: catalogue tables reference only other catalogue tables. No FK from user/customer tables into catalogue foundation tables.

Migrations applied include `worldwide_catalogue_foundation` + 4 chunk migrations.

## Active processes

- `python -m aio_uploader run --start-now` (pid 18884) — unrelated image uploader started 2026-08-01; **not** a worldwide catalogue collector writing staging
- No running import_run collectors detected against staging
- Multiple Chrome/Node/Dart processes (IDE / tooling)

## Backup roots

- Prior: `D:\CardScanR_backups\worldwide_prechange_20260802T131755Z` (and earlier)
- New: `D:\CardScanR_backups\cloudflare_migration_prechange_20260803_074320`
- Residual catalogue export tool: `tools/export_catalogue_residual_backup.py`
- Foundation schema copy stored in backup root

## Canary image URL contract (pre-mirror)

Search canary2 currently embeds third-party runtime hosts:

- Cards with non-CardScanR image hosts: **95,142**
- Cards with empty image URLs: **177,869**
- Products with non-CardScanR image hosts: **3,696**
- Products with empty image URLs: **922**
- Signed-looking card URLs: **0**

`THIRD_PARTY_RUNTIME_IMAGE_URLS` is **not** zero yet; Phase 8 must mirror and rebuild.

## Decision after audit

1. Do **not** retry the 3.47M-row Supabase catalogue load.
2. Export residual catalogue rows, then drop catalogue foundation tables.
3. Publish catalogue exclusively via Cloudflare R2.
4. Continue image mirroring + Flutter Cloudflare-native QA.
