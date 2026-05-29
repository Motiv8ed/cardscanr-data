# Phase 5F: Live Scheduler Guardrails

Phase 5F adds a guarded live eBay scheduler smoke path. It is disabled by default and is intended to prepare a tiny number of cooldown-aware refresh jobs for later worker processing.

This is different from the controlled worker batch:

- Worker batch enqueues selected card/market jobs and processes those exact jobs.
- Live scheduler smoke scans missing/stale cache candidates and may enqueue jobs, but it does not run the worker and does not open Chrome.

## Required Env Vars

Real live enqueue requires all of these:

- `MARKET_LOOKUP_PROVIDER=ebay_browser`
- `ENABLE_EBAY_REAL_LOOKUP=true`
- `ENABLE_LIVE_EBAY_SCHEDULER=true`
- `CONFIRM_LIVE_EBAY_SCHEDULER=true`
- `LIVE_EBAY_SCHEDULER_DRY_RUN=false`

Defaults are intentionally conservative:

- `ENABLE_LIVE_EBAY_SCHEDULER=false`
- `CONFIRM_LIVE_EBAY_SCHEDULER=false`
- `LIVE_EBAY_SCHEDULER_MARKETS=AU`
- `LIVE_EBAY_SCHEDULER_MAX_ENQUEUES_PER_RUN=2`
- `LIVE_EBAY_SCHEDULER_MAX_KEYS_SCANNED_PER_RUN=25`
- `LIVE_EBAY_SCHEDULER_MIN_COOLDOWN_HOURS=6`
- `LIVE_EBAY_SCHEDULER_ALLOW_FORCE_REFRESH=false`
- `LIVE_EBAY_SCHEDULER_DRY_RUN=true`
- `LIVE_EBAY_SCHEDULER_DAILY_ENQUEUE_CAP=20`

The scheduler must never force refresh. It calls `request_market_price_refresh(...)` with `force_refresh=false`, so shared cooldown and active-job gating still apply.

## Dry Run

Dry-run scans candidates and reports what would be enqueued:

```powershell
.\scripts\run_ebay_browser_live_scheduler_smoke.ps1 -DryRun -Markets AU -MaxEnqueues 2
```

Dry-run does not enqueue jobs, does not run the worker, and does not open Chrome.

## Real Enqueue

Real enqueue is explicit:

```powershell
$env:ENABLE_LIVE_EBAY_SCHEDULER = "true"
$env:CONFIRM_LIVE_EBAY_SCHEDULER = "true"
.\scripts\run_ebay_browser_live_scheduler_smoke.ps1 -Markets AU -MaxEnqueues 2
```

This enqueues at most the configured limit. It still does not run the worker. Use the controlled worker batch or worker commands separately after inspecting reports.

## Reports

Reports are written to:

- `reports/ebay_browser_live_scheduler_latest.json`
- `reports/ebay_browser_live_scheduler_runs.jsonl`
- `reports/chatgpt_uploads/ebay_browser_live_scheduler_latest.zip`

Candidate skip reasons include:

- `cache_fresh`
- `active_job_exists`
- `market_not_allowed`
- `daily_cap_reached`
- `max_enqueues_reached`
- `not_stale`
- `missing_cache`
- `stale_cache`

## Why Bulk Live Mode Stays Off

Live eBay browser lookups are local, slow, and can be blocked by eBay. The scheduler therefore remains capped, dry-run by default, market-allowlisted, and cooldown-aware. Do not enable broad live scheduling until the small-batch reports show stable quality and rate behavior.
