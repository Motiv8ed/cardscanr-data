# Supabase worldwide catalogue cleanup

Project: `qstcdlczasmvexpgbpjk`  
Migration: `remove_worldwide_catalogue_from_supabase` (`20260803080000`)

## Before

- Database size: **857 MB** (`899,026,067` bytes)
- Residual catalogue rows (prior “truncated” claim was incomplete):

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

## Backup

- Root: `D:\CardScanR_backups\cloudflare_migration_prechange_20260803_074320`
- Foundation schema SQL copied
- Full ID exports for residual large tables (`*.ids.jsonl.gz`)
- Small-table full JSONL exports where completed
- Canonical catalogue remains staging SQLite SHA-256 `24bb6bf7a8776435164b20059cc93c9622f4bed7b7d45c0012fb83bfb666ff24`

## After

- Database size: **67 MB** (`70,683,795` bytes)
- Public base tables retained (17):  
  `card_image_manifests`, `customer_*` (6), `market_price_*` (5), `pokemon_card_image_records`, `scan_sessions`, `user_cards`, `user_collections`, `user_profiles`
- Catalogue foundation tables: **absent** (`card_printings` / `franchises` / `source_records` → null)
- Retained counts unchanged: user_profiles=3, user_collections=3, user_cards=60, card_image_manifests=23742, pokemon_card_image_records=607
- Auth users: 3
- Writes: DML against retained tables succeeds; project `ACTIVE_HEALTHY`
- `WORLDWIDE_CATALOGUE_ROWS_IN_SUPABASE = 0`

## Security advisors

Pre-existing WARN findings remain on customer SECURITY DEFINER RPCs and leaked-password protection. No new catalogue-table exposure.

## Loader guard

`tools/publish_staging_to_supabase.py` now requires:
- `--i-understand-this-writes-catalogue-to-supabase`
- and rejects production ref unless `--allow-production-catalogue-load`

Validated: execute against production without opt-in → blocked.
