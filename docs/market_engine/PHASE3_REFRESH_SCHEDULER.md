# Market Price Engine — Phase 3 Refresh Scheduler

## Purpose

Phase 3 adds a mock-safe background scheduler that scans existing market keys/cache state and enqueues useful refresh jobs.

The scheduler does **not** run provider lookups and does **not** mutate cached price values directly.

## Why the scheduler exists

Without scheduling, background refresh can become noisy and expensive by trying to refresh everything.

This phase adds deterministic candidate scoring and bounded enqueue behavior so refresh work focuses on keys that are most useful first.

## How it protects scale

- uses bounded reads (`MARKET_SCHEDULER_MAX_KEYS_PER_RUN`)
- uses bounded enqueue count (`MARKET_SCHEDULER_MAX_ENQUEUES_PER_RUN`)
- skips keys already covered by active queued/running jobs
- prioritizes missing/stale/value/popularity/recency signals
- supports dry-run mode for safe validation before enqueueing

## Priority model (lower number = higher priority)

- `50` = missing cache key (especially recently seen/newly scanned)
- `80` = stale high-value cache
- `90` = stale popular cache
- `100` = normal stale/background refresh
- `100` = low-priority old/background refresh (Phase 1 schema currently caps priority at `<=100`)

Reserved priorities remain unchanged:

- `5` force refresh (reserved)
- `10` user refresh (reserved)

## Candidate sources

The scheduler reads:

1. keys with no `market_price_cache` row (optional via env flag)
2. cache rows where `stale_after < now()` (optional via env flag)

Then it checks active jobs (`queued`/`running`) and only enqueues if no active job exists.

## Dry-run mode

Set `MARKET_SCHEDULER_DRY_RUN=true` to evaluate and report candidates without creating jobs.

Dry-run still writes scheduler reports and includes `jobsDryRunOnly` counts.

## Required environment variables

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## Scheduler environment variables

- `MARKET_SCHEDULER_MAX_KEYS_PER_RUN` (default `100`)
- `MARKET_SCHEDULER_MAX_ENQUEUES_PER_RUN` (default `50`)
- `MARKET_SCHEDULER_INCLUDE_MISSING_CACHE` (default `true`)
- `MARKET_SCHEDULER_INCLUDE_STALE_CACHE` (default `true`)
- `MARKET_SCHEDULER_MIN_POPULARITY_SCORE` (default `0`)
- `MARKET_SCHEDULER_MIN_INVENTORY_COUNT` (default `0`)
- `MARKET_SCHEDULER_DRY_RUN` (default `false`)
- optional: `MARKET_SCHEDULER_POLL_SECONDS` (default `300`)

## Commands

Python one-shot run:

```bash
python workers/market_price_scheduler.py --once
```

Python loop mode:

```bash
python workers/market_price_scheduler.py --max-cycles 10
```

PowerShell wrapper:

```powershell
.\scripts\run_market_price_scheduler.ps1 -Once
```

## Expected reports

- `reports/market_price_scheduler_latest.json`
- `reports/market_price_scheduler_runs.jsonl`

Reports include:

- candidates scanned
- jobs enqueued
- jobs skipped because already active
- jobs skipped due to enqueue limits
- top priority reasons
- per-candidate decision diagnostics

Secret-like keys are sanitized in written report payloads.

## How it works with the worker

- Scheduler enqueues refresh jobs through `enqueue_market_price_refresh`.
- Worker (`workers/market_price_worker.py`) claims and processes queued jobs.
- Scheduler and worker are decoupled; scheduler only creates queue work.

## What this phase does not do yet

- no real eBay/provider integration
- no browser automation
- no paid API integration
- no Flutter changes
- no direct cache value recalculation in scheduler

## Next recommended phase

Add controlled production-grade cadence/partitioning (for example, segment keys by game/market windows), add scheduler telemetry dashboards, and add integration tests against a disposable Supabase environment.
