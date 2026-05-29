# Phase 5E: Live Worker Batch Validation

Phase 5E adds a controlled, small-batch live eBay worker validation path. It is not scheduler live mode and it does not process arbitrary queue backlog.

The script enqueues selected card/market refreshes through `request_market_price_refresh(...)`, then processes only the job IDs returned for those requested keys. Normal app cooldown behavior is unchanged.

## Required Flags

The batch refuses to run unless all are set:

- `MARKET_LOOKUP_PROVIDER=ebay_browser`
- `ENABLE_EBAY_REAL_LOOKUP=true`
- `CONFIRM_LIVE_EBAY_WRITE=true`
- `CONFIRM_LIVE_EBAY_WORKER=true`

The PowerShell wrapper sets the local Chrome profile configuration:

- `EBAY_BROWSER_PROFILE_NAME=cardscanr`
- `EBAY_BROWSER_USER_DATA_DIR=D:\cardscanr-data\.browser_profiles\cardscanr`

## AU-Only Default

```powershell
$env:CONFIRM_LIVE_EBAY_WRITE = "true"
$env:CONFIRM_LIVE_EBAY_WORKER = "true"
.\scripts\run_ebay_browser_live_worker_batch.ps1
```

Defaults:

- Card: `Charizard ex`
- Collector number: `125/197`
- Set: `Obsidian Flames`
- Markets: `AU`
- Max jobs: `1`

## AU/US/GB/CA Batch

```powershell
$env:CONFIRM_LIVE_EBAY_WRITE = "true"
$env:CONFIRM_LIVE_EBAY_WORKER = "true"
.\scripts\run_ebay_browser_live_worker_batch.ps1 -ForceRefresh -Markets AU,US,GB,CA -MaxJobs 4 -PauseBetweenJobsSeconds 20
```

Use `-ForceRefresh` only for backend/service-role validation. Without it, fresh cache entries return `cache_fresh` and are not processed.

## Reports

Reports are written to:

- `reports/ebay_browser_live_worker_batch_latest.json`
- `reports/ebay_browser_live_worker_batch_runs.jsonl`
- `reports/chatgpt_uploads/ebay_browser_live_worker_batch_latest.zip`
- `reports/ebay_browser_debug/live_worker_batch/latest/<market>/`

Each market includes the refresh RPC result, returned job id, worker result, cache/evidence summary, item-price view, landed-price view, rejected/included counts, errors, and debug artifact directory.

## Safety

The script does not run the scheduler. It claims exact job IDs with `claim_specific_refresh_job(...)` and verifies the job `price_key_id` matches the requested key before processing.

`MaxJobs` defaults to `1` and exists to avoid accidental bulk processing. The confirmation flags make live browser work explicit because the provider uses real eBay pages and the local Chrome profile.

Scheduler live eBay mode remains disabled until this controlled batch path has been validated.
