# Market Price Engine — Phase 2.5 Supabase Smoke Tests

## Purpose

Phase 2.5 adds an end-to-end smoke test that proves the Phase 1 Supabase schema/RPCs and the Phase 2 mock worker pipeline work together against a real Supabase project.

This is a runtime verification script, not a production feature.

## Required environment variables

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `MARKET_LOOKUP_PROVIDER=mock`

Optional worker/runtime env vars from Phase 2 still apply (poll, max jobs, stale window settings), but are not required for the one-shot smoke script.

## How to run the smoke test

### Python

```bash
python scripts/smoke_market_price_engine.py
```

### PowerShell

```powershell
.\scripts\run_market_price_engine_smoke.ps1
```

The PowerShell wrapper fails fast with a clear error when required env vars are missing or provider mode is not `mock`.

## What the smoke script does

It uses a deterministic smoke identity:

- game: `pokemon`
- card_name: `Smoke Test Charizard ex`
- set_name: `Smoke Test Set`
- set_code: `smoke-test`
- collector_number: `001/999`
- language: `en`
- variant: `raw`
- condition: `raw`
- market_country: `au`
- currency: `aud`

Workflow:

1. Validates env vars and provider mode.
2. Calls `get_or_create_market_price_key`.
3. Calls `enqueue_market_price_refresh` twice and verifies active-job dedupe (same active job reused).
4. Runs the mock worker once.
5. Calls `get_market_price_bundle`.
6. Verifies cache, snapshot, evidence, confidence/freshness/sample fields, and no queued/running active job for the processed refresh.
7. Enqueues another refresh and verifies a new active job is allowed after completion.
8. Writes run reports.

## Expected output

Console:

- Success line: `[market-engine-smoke] SUCCESS ...`
- Structured JSON summary for the run.

Report files:

- `reports/market_price_engine_smoke_latest.json`
- `reports/market_price_engine_smoke_runs.jsonl`

Report payloads are sanitized so secret-like fields are redacted.

## How to inspect Supabase rows

Use the smoke fingerprint from script output and run:

```sql
select * from public.market_price_keys where fingerprint = '<smoke_fingerprint>';
select * from public.market_price_cache where price_key_id = '<price_key_uuid>';
select * from public.market_price_snapshots where price_key_id = '<price_key_uuid>' order by created_at desc limit 5;
select * from public.market_sold_listing_evidence where price_key_id = '<price_key_uuid>' order by created_at desc limit 20;
select * from public.market_price_refresh_jobs where price_key_id = '<price_key_uuid>' order by requested_at desc limit 20;
select public.get_market_price_bundle('<smoke_fingerprint>', 50);
```

## Common failures

- Missing env vars (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`)
- `MARKET_LOOKUP_PROVIDER` not set to `mock`
- Supabase RPC/table permission/config mismatch
- Migration not applied in target Supabase project
- Worker job failure caused by invalid/missing key or data shape mismatch

## What success means

A successful run demonstrates:

- RPC key creation/upsert works
- active-job dedupe works for queued/running jobs
- worker can claim, process, write snapshot/evidence/cache, and complete job
- bundle RPC returns coherent read-side data
- follow-up refresh enqueue is allowed after completion

## What this does not prove yet

- Real marketplace/eBay integrations
- Browser automation flows
- Paid/provider API integrations
- Flutter app integration behavior
- High-concurrency or high-volume production performance behavior
