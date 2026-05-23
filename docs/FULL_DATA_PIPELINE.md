# Full Data Pipeline

Run the non-eBay CardScanR data pipeline from the repo root:

```powershell
.\scripts\run_cardscanr_full_data_pipeline.ps1
```

The runner is local-worker first. It calls the existing Pokewallet catalogue worker/cycle scripts for provider collection, then builds app catalogue data, image metadata, non-eBay current prices, tracked history snapshots, index hashes, validation, and a runtime report.

## Common Options

```powershell
.\scripts\run_cardscanr_full_data_pipeline.ps1 -NoFetch
.\scripts\run_cardscanr_full_data_pipeline.ps1 -UntilComplete -MaxRequestsPerHour 90 -MaxRequestsPerDay 900
.\scripts\run_cardscanr_full_data_pipeline.ps1 -Languages en,jp
.\scripts\run_cardscanr_full_data_pipeline.ps1 -IncludeZh
.\scripts\run_cardscanr_full_data_pipeline.ps1 -BuildAppCatalogue
.\scripts\run_cardscanr_full_data_pipeline.ps1 -DownloadImages
.\scripts\run_cardscanr_full_data_pipeline.ps1 -Commit
.\scripts\run_cardscanr_full_data_pipeline.ps1 -DryRun
```

Defaults:

- Fetch/update the Pokewallet provider catalogue using the existing safe worker cycle.
- Build EN and JP app catalogue data from the existing app-catalogue builders.
- Build the image manifest, without downloading image binaries.
- Build current prices from non-eBay sources.
- Build tracked-card history from current price records already in the cache.
- Refresh `public/v1/index.json` hashes.
- Run validation/reporting commands.
- Write runtime reports under `reports/`.
- Do not commit unless `-Commit` is passed, except for existing provider-worker commit conventions.

The default provider step runs one safe provider catalogue cycle. Use `-NoFetch` when you only want to rebuild derived app data, images, prices, history, index files, and reports from the existing cache.

`-NoFetch` does not block provider-to-app catalogue promotion. The promotion stage reads already-downloaded Pokewallet provider records and safely adds only records with enough identity for the app. Use `-SkipAppCatalogue` to skip both app catalogue fetching and provider promotion.

`-UntilComplete` changes the provider step into the existing manual worker loop. That can run many provider cycles and can wait on hourly/daily request budgets. The full pipeline is intentionally sequential, so app catalogue, image, price, history, index, validation, and commit stages will not start until `provider_catalogue` finishes.

The full pipeline runner streams child output live and prints provider heartbeats while `provider_catalogue` is running. In another PowerShell window, you can also monitor provider progress directly:

```powershell
.\scripts\watch_pokewallet_catalog_worker.ps1
```

The watcher displays current priority language, next language to process, last cycle times, last status, last commit, and request budget details from `data/pokewallet_catalog_worker_status.json`.

## Stage Notes

- Provider catalogue: `scripts/run_pokewallet_catalog_cycle.ps1` or `scripts/run_pokewallet_catalog_worker_loop.ps1 -UntilComplete`.
- App catalogue: `tools/build_price_cache.py app_catalogue`.
- Provider-to-app promotion: `tools/promote_provider_catalog_to_app_catalog.py --languages en,jp`; every provider record is either represented, promoted, or blocked with a reason in `reports/provider_to_app_promotion_latest.json`.
- Images: `tools/build_image_cache.py`; `-IncludeZh` adds ZH provider image references as skipped/auth-required references, not public app-ready images.
- EN prices: `tools/build_price_cache.py current_prices`; Stage 1 schema is preserved.
- JP prices: `tools/build_pokewallet_jp_prices.py`; this is non-eBay, controlled, and writes records only when source data matches confidently.
- History: `tools/build_price_history_snapshots.py`; reads current price files and does not call providers.
- Index: `tools/refresh_public_index.py`; preserves timestamps when hashes/material content did not change.
- Health: `tools/report_data_health.py`.

## Out Of Scope

No eBay scraping, eBay sold-listing pricing, marketplace scraping, or AU sold-listing ingestion is implemented by this runner.

## Release Command

After the pipeline has generated data, use the controlled release script:

```powershell
.\scripts\release_cardscanr_data.ps1
```

Dry-run mode:

```powershell
.\scripts\release_cardscanr_data.ps1 -DryRun
```

Push mode:

```powershell
.\scripts\release_cardscanr_data.ps1 -Push
```

Optional release scope:

- `-IncludeDocs` includes `docs/` changes.
- `-IncludeReports` includes `reports/` changes.

The release script always starts by printing git status, runs health/coverage/gap/validation reports, summarizes key totals, stages only allowed generated paths, refuses cache/tmp/runtime/secrets paths, commits only when staged changes are meaningful, and only pushes when `-Push` is provided.

## ChatGPT Upload Report

After any major command, generate a concise uploadable bundle:

```powershell
# Standalone — any time
.\scripts\export_chatgpt_report.ps1

# After full pipeline run
.\scripts\run_cardscanr_full_data_pipeline.ps1 -NoFetch -BuildAppCatalogue -BuildImages -BuildHistory -Validate -ExportChatGPTReport

# After release (dry-run safe)
.\scripts\release_cardscanr_data.ps1 -DryRun -ExportChatGPTReport
```

Output in `reports/chatgpt_exports/` (git-ignored):
- `cardscanr_chatgpt_report_latest.md` — human-readable: git status, data counts, blocked records, pipeline status, next recommended action
- `cardscanr_chatgpt_report_latest.json` — structured form of the same data
- `cardscanr_chatgpt_report_latest.zip` — safe bundle including supporting status/report files from `public/v1/` and `reports/`

The export excludes `.env`, secrets, credentials, local image binaries, and runtime logs. Add `--include-large-reports` / `-IncludeLargeReports` to include the full blocked-cards and promotion JSON reports in the zip.

## Image Cache

`-DownloadImages` writes a bounded local cache under `.cache/cardscanr-images/`, which is ignored by Git. The default image path is URL-manifest only.
