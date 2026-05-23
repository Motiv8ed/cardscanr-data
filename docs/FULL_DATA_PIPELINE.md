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

## Stage Notes

- Provider catalogue: `scripts/run_pokewallet_catalog_cycle.ps1` or `scripts/run_pokewallet_catalog_worker_loop.ps1 -UntilComplete`.
- App catalogue: `tools/build_price_cache.py app_catalogue`.
- Images: `tools/build_image_cache.py`; `-IncludeZh` adds ZH provider image references as skipped/auth-required references, not public app-ready images.
- EN prices: `tools/build_price_cache.py current_prices`; Stage 1 schema is preserved.
- JP prices: `tools/build_pokewallet_jp_prices.py`; this is non-eBay, controlled, and writes records only when source data matches confidently.
- History: `tools/build_price_history_snapshots.py`; reads current price files and does not call providers.
- Index: `tools/refresh_public_index.py`; preserves timestamps when hashes/material content did not change.
- Health: `tools/report_data_health.py`.

## Out Of Scope

No eBay scraping, eBay sold-listing pricing, marketplace scraping, or AU sold-listing ingestion is implemented by this runner.

## Image Cache

`-DownloadImages` writes a bounded local cache under `.cache/cardscanr-images/`, which is ignored by Git. The default image path is URL-manifest only.
