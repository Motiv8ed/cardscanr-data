# Market Price Engine — Phase 2 Mock Worker

## What Phase 2 does

Phase 2 adds the first Python worker loop for the Market Price Engine.

It now:
- claims queued refresh jobs through `claim_market_price_refresh_jobs`
- loads the related `market_price_keys` row
- uses a deterministic mock comps provider
- filters/rejects bad sold comps
- calculates pricing stats and confidence
- writes `market_price_snapshots`
- writes `market_sold_listing_evidence`
- upserts `market_price_cache`
- completes jobs through `complete_market_price_refresh_job`
- fails jobs through `fail_market_price_refresh_job` without clearing old cached prices

This phase is mock-only. It does not scrape eBay, use browser automation, or use paid APIs.

## Required environment variables

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `MARKET_LOOKUP_PROVIDER=mock`
- `MARKET_WORKER_CONCURRENCY=1`
- `MARKET_WORKER_POLL_SECONDS=5`
- `MARKET_WORKER_MAX_JOBS_PER_RUN=5`
- `MARKET_CACHE_HIGH_CONFIDENCE_HOURS=24`
- `MARKET_CACHE_MEDIUM_CONFIDENCE_HOURS=12`
- `MARKET_CACHE_LOW_CONFIDENCE_HOURS=6`
- optional: `MARKET_CACHE_NO_COMPS_HOURS=3`
- optional: `MARKET_WORKER_ID=market-price-worker`

## How to run unit tests

```bash
python -m unittest discover -s tests -p "test_market_engine_*.py"
```

## Existing repository validation commands

```bash
CARDSCANR_VALIDATE_QUIET=1 python tools/validate_cache.py
python tools/test_local_price_update_budget.py
python tools/test_image_cache.py
```

## How to run the worker in mock mode

One cycle:

```bash
python workers/market_price_worker.py --once
```

Loop mode:

```bash
python workers/market_price_worker.py --max-cycles 10
```

PowerShell wrapper:

```powershell
.\scripts\run_market_price_worker.ps1 -Once
```

Runtime reports are written to:
- `reports/market_price_worker_latest.json`
- `reports/market_price_worker_runs.jsonl`

## How to enqueue a test refresh

Example SQL:

```sql
select public.enqueue_market_price_refresh(
  p_price_key_id => '<price_key_uuid>',
  p_reason => 'phase2_mock_test',
  p_priority => 10,
  p_requested_by_user_id => null,
  p_dedupe_key => 'phase2-mock-test'
);
```

If you need a key first, create or reuse one through the Phase 1 key RPC, then enqueue the returned key id.

## How to verify cache, snapshot, and evidence updates

Check the latest cache row:

```sql
select *
from public.market_price_cache
where price_key_id = '<price_key_uuid>';
```

Check the newest snapshot:

```sql
select *
from public.market_price_snapshots
where price_key_id = '<price_key_uuid>'
order by created_at desc
limit 1;
```

Check evidence linked to that snapshot:

```sql
select *
from public.market_sold_listing_evidence
where snapshot_id = '<snapshot_uuid>'
order by sold_date desc nulls last, created_at desc;
```

Or use the bundle helper:

```sql
select public.get_market_price_bundle('<fingerprint>', 50);
```

## Limitations

- provider mode is `mock` only
- no live marketplace integration is included
- filtering is intentionally basic and deterministic
- worker concurrency stays sequential in Phase 2
- unit tests cover payload preparation and pricing logic only; Supabase integration depends on env-backed runtime access

## Next phase recommendation

Phase 3 should add a real provider adapter behind the same `MarketCompsProvider` interface, improve title/collector matching, refine confidence scoring, and add optional integration tests against a disposable Supabase environment.
