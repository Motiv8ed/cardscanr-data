# Supabase load disk blocker

- Project: `qstcdlczasmvexpgbpjk`
- Dry-run: PASS (`3,479,319` planned rows)
- Production backup: `D:\CardScanR_backups\worldwide_prechange_20260802T131755Z` (17 accessible resources; user-data tables skipped as forbidden)
- Foundation schema: applied via MCP migrations
- Execute attempt: **FAILED** while upserting `card_printings`

```text
HTTP 503 code 53100
could not extend file "base/5/26819": No space left on device
hint: Check free disk space.
```

Progress before failure: `card_designs` 188,833 loaded; `card_printings` ~96,500 of 296,463.

## Mitigation performed

All worldwide catalogue foundation tables were truncated (cascade) to free the partially loaded rows and restore project headroom. Pre-existing app tables (user/market/customer) were not truncated.

## Resume condition

Upgrade/expand the Supabase project disk (or load into a larger project/branch), then re-run:

```bat
C:\Python314\python.exe -u tools\publish_staging_to_supabase.py ^
  --database D:\CardScanR_worldwide_runtime_20260802\staging\worldwide-catalogue.sqlite ^
  --report reports\worldwide_catalogue\SUPABASE_LOAD_EXECUTED_20260802.json ^
  --execute --project-ref qstcdlczasmvexpgbpjk --confirm-project-ref qstcdlczasmvexpgbpjk ^
  --supabase-url https://qstcdlczasmvexpgbpjk.supabase.co
```

App publication does not depend on this normalized Supabase load; the immutable R2 canary already holds the searchable catalogue.
