# CardScanR Scripts

## Run This For eBay Price Updates

When the app queues a market price update after a scan or a manual refresh, keep this worker running:

```powershell
.\scripts\run_ebay_price_worker.ps1
```

Double-click friendly wrapper:

```powershell
.\scripts\run_ebay_price_worker.bat
```

Useful variants:

```powershell
.\scripts\run_ebay_price_worker.ps1 -Headed
.\scripts\run_ebay_price_worker.ps1 -Once
.\scripts\run_ebay_price_worker.ps1 -DryRun
.\scripts\run_ebay_price_worker.ps1 -SkipConfigCheck
```

This is a friendly wrapper around `start_live_ebay_worker.ps1`, which delegates to `run_market_price_worker.ps1`. Keep the wrapper as the normal operator entrypoint and keep the lower-level scripts for diagnostics or tests.

## Common Script Groups

- `run_ebay_price_worker.ps1`: normal eBay price worker for queued app scan/refresh jobs.
- `start_live_ebay_worker.ps1`: guarded live eBay worker implementation.
- `check_live_ebay_worker_config.ps1`: verifies local Chrome/Playwright/Supabase setup without claiming jobs.
- `run_market_price_worker.ps1`: lower-level Supabase queue worker.
- `run_cardscanr_full_data_pipeline.ps1`: catalog/static data pipeline.
- `run_local_price_update.ps1`: local rotating static price updater.
- `debug_*.ps1` and `smoke_*.ps1`: diagnostics and controlled validation scripts.
