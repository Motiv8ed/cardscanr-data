# Market Price Engine — Phase 4B: Local Engine Runner

## Purpose

Phase 4B adds a **local orchestration runner** that lets your development PC behave like a temporary backend engine for the Market Price Engine.  It coordinates the existing **scheduler** and **mock worker** in a configurable multi-cycle loop — no server required.

This is not a production deployment.  It is a developer convenience that lets you:

- Validate that the scheduler enqueues the right jobs.
- Validate that the worker processes those jobs correctly.
- Generate realistic mock reports for UI development.
- Iterate quickly without deploying to a cloud server.

---

## How it simulates a temporary local backend

A production Market Price Engine would run the scheduler and worker as separate long-lived services.  Phase 4B collapses them into a single process:

```
for each cycle:
    1. run_scheduler_once()    → enqueue refresh jobs
    2. run_worker_once()       → process refresh jobs (mock provider)
    3. sleep(poll_seconds)     → optional pause
```

The runner uses the same `MarketPriceRefreshScheduler` and `MarketPriceJobRunner` classes as the production workers — it only wraps them.

---

## Required environment variables

| Variable | Notes |
|----------|-------|
| `SUPABASE_URL` | Your Supabase project REST URL. Required for live/local Supabase-backed runs. |
| `SUPABASE_SERVICE_ROLE_KEY` | Service role key (never commit). Required for live/local runs. |
| `MARKET_LOOKUP_PROVIDER` | Must be `mock` (or unset — defaults to `mock`). Live providers are blocked. |

> **Cloud/Codex note:** If `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` are not configured, live Supabase execution will fail.  Use `--dry-run` for cloud tests without credentials.  Unit tests do not require real credentials.

---

## Commands

### Dry-run (no DB writes, no Supabase calls)

```bash
python workers/market_price_engine_local.py --dry-run
```

PowerShell:

```powershell
.\scripts\run_market_price_engine_local.ps1 -DryRun
```

---

### One-shot cycle (scheduler + worker, one cycle)

```bash
python workers/market_price_engine_local.py --cycles 1
```

PowerShell:

```powershell
.\scripts\run_market_price_engine_local.ps1 -Cycles 1
```

---

### Repeated local loop

```bash
python workers/market_price_engine_local.py --cycles 5 --poll-seconds 30
```

PowerShell:

```powershell
.\scripts\run_market_price_engine_local.ps1 -Cycles 5 -PollSeconds 30
```

---

### Full options

```bash
python workers/market_price_engine_local.py \
    --cycles 3 \
    --poll-seconds 60 \
    --scheduler-max-keys 100 \
    --scheduler-max-enqueues 50 \
    --worker-max-jobs 50 \
    --dry-run \
    --reports-dir reports
```

PowerShell:

```powershell
.\scripts\run_market_price_engine_local.ps1 `
    -Cycles 3 `
    -PollSeconds 60 `
    -SchedulerMaxKeys 100 `
    -SchedulerMaxEnqueues 50 `
    -WorkerMaxJobs 50 `
    -DryRun
```

---

## CLI options

| Option | Default | Description |
|--------|---------|-------------|
| `--cycles` | `1` | Number of scheduler+worker cycles to run. |
| `--poll-seconds` | `0` | Sleep between cycles (seconds). |
| `--scheduler-max-keys` | `100` | Max candidate keys per scheduler run. |
| `--scheduler-max-enqueues` | `50` | Max enqueues per scheduler run. |
| `--worker-max-jobs` | `50` | Max jobs per worker run. |
| `--dry-run` | off | Dry-run: no DB writes, no job processing. |
| `--reports-dir` | `reports` | Directory for output report files. |

---

## How it works with the scheduler and worker

The runner delegates entirely to the existing engine components:

1. **Scheduler** (`cardscanr_market_engine/scheduler.py`, `MarketPriceRefreshScheduler`): scans for missing/stale price cache keys and enqueues refresh jobs.
2. **Worker** (`cardscanr_market_engine/job_runner.py`, `MarketPriceJobRunner`): claims queued jobs, calls the mock provider, writes snapshots, evidence, and cache rows.

No logic is duplicated — the runner only coordinates their execution order and timing.

---

## Mock safety

- `MARKET_LOOKUP_PROVIDER` must be `mock` (or unset — the default is `mock`).
- The runner will **refuse to start** if a live provider is configured.
- No real eBay calls are made.
- No browser automation is used.

---

## How reports are written

Two report files are written after each complete run:

| File | Description |
|------|-------------|
| `reports/market_price_engine_local_latest.json` | Latest run report (overwritten each run). |
| `reports/market_price_engine_local_runs.jsonl` | Append-only history of all runs. |

### Report schema

```json
{
  "started_at": "2026-05-26T00:00:00Z",
  "completed_at": "2026-05-26T00:00:05Z",
  "cycles_requested": 2,
  "cycles_completed": 2,
  "dry_run": false,
  "total_jobs_enqueued": 4,
  "total_jobs_processed": 4,
  "total_jobs_completed": 4,
  "total_jobs_failed": 0,
  "errors": [],
  "scheduler_summaries": [
    {
      "cycle": 1,
      "status": "success",
      "candidatesScanned": 5,
      "jobsEnqueued": 4,
      "jobsDryRunOnly": 0
    }
  ],
  "worker_summaries": [
    {
      "cycle": 1,
      "status": "success",
      "jobCount": 4,
      "jobsCompleted": 4,
      "jobsFailed": 0
    }
  ],
  "env_summary": {
    "supabase_url_present": true,
    "supabase_service_role_key_present": true,
    "market_lookup_provider": "mock",
    "market_worker_id": "market-price-worker"
  },
  "supabase_env_present": true,
  "config": {
    "scheduler_max_keys": 100,
    "scheduler_max_enqueues": 50,
    "worker_max_jobs": 50,
    "poll_seconds": 0
  }
}
```

Sensitive values (`SUPABASE_SERVICE_ROLE_KEY`, API keys, tokens) are **never written to reports**.  The report only records whether the env vars were present (`true`/`false`).

---

## Unit tests

```bash
python -m unittest tests/test_market_engine_local_runner.py -v
```

Or with the discover pattern:

```bash
python -m unittest discover -s tests -p "test_market_engine_*.py"
```

Tests cover:
- One-cycle orchestration calls scheduler then worker.
- Dry-run does not process worker jobs.
- Multiple cycles aggregate summaries.
- No-jobs is successful.
- Worker failure appears in report.
- Report redacts secrets.
- Mock provider enforcement.
- Missing Supabase env is handled clearly.
- Cloud-safe tests — no real credentials required.

---

## Limitations

- **Mock provider only** — no real eBay sold-listing data.
- Requires Supabase credentials for live runs.  Use `--dry-run` in environments without credentials.
- Runs scheduler and worker sequentially (single-threaded).
- Does not delete any data; safe to run repeatedly.
- Does not mutate cached price values except through the standard worker pipeline.
- Dry-run does not enqueue or process jobs — scheduler is still called but with `dry_run=True`.

---

## Next recommended phase

**Phase 5**: Connect to a live eBay sold-listings provider.  At that point the local runner remains useful for local smoke testing, but production execution should move to a cloud scheduler (e.g., Supabase Edge Functions, GitHub Actions cron, or a dedicated server).

---

## Cloud/Codex limitation

Live Supabase execution requires `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` to be set in the environment.  These secrets must **never** be committed to source code.  If they are not available in the CI/cloud environment, use `--dry-run` or skip integration-only tests.  Unit tests in `tests/test_market_engine_local_runner.py` are fully cloud-safe and do not require credentials.
