# Phase 1 current-state and preservation record

Captured on 2026-08-02 (Australia/Brisbane) before any worldwide migration or publication was applied.

## Repository isolation

- Canonical repository: `D:\cardscanr-data`
- Canonical starting commit: `94fe6a41` (owner `.gitignore` change preserved and untouched)
- Isolated worktree: `D:\CardScanR_worktrees\worldwide_catalogue_products_20260802`
- Branch: `feature/worldwide-pokemon-catalogue-products-20260802`
- Flutter repository: inspected separately; no app worktree or app mutation was made during preservation.
- Historical backfill runtime: `D:\CardScanR_image_backfill`; checkpoints and payloads were preserved and no healthy worker was terminated.

The machine-readable repository and production audit is in `reports/global_rollout/current_state.json`; its Markdown rendering is `reports/global_rollout/current_state.md`.

## Verified pre-change backup

- Backup root: `D:\CardScanR_backups\worldwide_prechange_20260801T230701Z`
- Manifest SHA-256: `451fe86990ff6bf43f7e1f533bdf5047fa48acdf4220062b58dc2704e88e054f`
- Supabase resources: 22
- Supabase rows: 81,485
- Accessible R2 catalogue objects inventoried: 12 (1,416,096,322 bytes)
- R2 publication manifests copied: 3
- Local manifests copied: 6
- Payload checksums checked: 32; failures: 0
- Credentials in backup payload: none

Five RLS-protected tables required a temporary, SELECT-only `service_role` grant for export. The grants were revoked immediately after export; the post-backup audit found zero remaining `service_role` SELECT grants on those tables and RLS remained enabled. Full details and the restore procedure are in `reports/worldwide_catalogue/PRECHANGE_BACKUP_20260802.md`.

## Existing app catalogue baseline

| Language | Sets | Cards | Card files |
|---|---:|---:|---:|
| English (`en`) | 173 | 46,417 | 388 |
| Japanese (`jp`) | 570 | 28,161 | 473 |
| Chinese (`zh`) | 58 | 6,439 | 58 |

- Existing public search database: 74,578 rows (`en` 46,417; `jp` 28,161); Chinese is not represented in the search database.
- Existing missing-image package: internally consistent at 2,907 unresolved records (2,857 Japanese and 50 English/international promos) against its historical 26,605-record target.
- The inherited Japanese records include demonstrably implausible source names, so inherited identity and text are subject to quarantine and corroboration rather than blind promotion.

## Existing image state

- `pokemon_card_image_records`: 607 rows (591 completed, 16 provider-unavailable); none have `verified_at` populated.
- Current `card_image_manifests`: 23,735 current/verified rows (`en` 20,346; `ja` 3,389).
- Current provider hosts: `images.pokemontcg.io` 19,768; `assets.tcgdex.net` 3,389; `images.scrydex.com` 541; null 37.
- All 23,735 current rows have display and thumbnail keys, content SHA-256, and verification timestamps; none has an original-object key.
- Manifest rows identify the image bucket as `cardscanr-card-images`. The locally available Cloudflare credentials can enumerate only `cardscanr-catalog`; bucket enumeration is denied and the account API token fails authentication. This is a verification/access gap, not evidence that the image objects are absent.

## Safety state

- No production catalogue was replaced.
- No Supabase worldwide migration was applied.
- No R2 object or rollback release was deleted.
- No Flutter application file was modified.
- No existing image or backfill payload was overwritten.
