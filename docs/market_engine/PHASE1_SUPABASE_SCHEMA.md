# Market Price Engine — Phase 1 Supabase Schema

This document describes the Phase 1 Supabase/Postgres implementation for the Market Price Engine.

## Scope

Phase 1 includes:

- Database tables for shared market price identities, cache, snapshots, sold-listing evidence, and refresh jobs.
- Constraints and indexes for data integrity and queue performance.
- Row Level Security (RLS) policies and grants.
- Queue and key management RPC functions.
- A read helper RPC for bundle retrieval.

Phase 1 does **not** include:

- Flutter code changes.
- Real eBay/browser scraping integration.
- Worker/provider implementation logic.

## Migration file

- `/home/runner/work/cardscanr-data/cardscanr-data/supabase/migrations/20260525025800_market_price_engine_phase1.sql`

## Tables

### `public.market_price_keys`

Shared normalized identity for pricing.

- Unique key: `fingerprint` (lowercase, non-empty).
- Tracks identity fields (`game`, `language`, `set_name`, `collector_number`, `condition`, etc.).
- Tracks usage metadata (`popularity_score`, `inventory_count`, `last_seen_at`).

### `public.market_price_cache`

Latest app-visible cached price per identity.

- One row per `price_key_id` (`unique`).
- Stores summary pricing fields, confidence, provider/market metadata, freshness timestamps, refresh status, and last error.
- References latest snapshot via `latest_snapshot_id`.

### `public.market_price_snapshots`

Immutable historical snapshots.

- Append-only by application policy.
- Stores diagnostics and aggregate stats (`sample_size`, `included_count`, `rejected_count`, etc.).

### `public.market_sold_listing_evidence`

Sold-listing evidence rows for explainability.

- Linked to both `price_key_id` and `snapshot_id`.
- Includes included/excluded flags and rejection reason.
- Keeps raw provider payload in `raw_json` for worker/service use.

### `public.market_price_refresh_jobs`

Queue for refresh requests.

- Status lifecycle: `queued`, `running`, `completed`, `failed`, `cancelled`.
- Partial unique index enforces one active (`queued` or `running`) job per `price_key_id`.
- Includes user origin (`requested_by_user_id`), worker lock metadata, attempts, and error details.

## Queue and helper RPCs

### `public.get_or_create_market_price_key(...) -> uuid`

- Upserts a key by lowercase fingerprint.
- Updates identity details and `last_seen_at` when a key already exists.

### `public.enqueue_market_price_refresh(...) -> market_price_refresh_jobs`

- Enqueues a queued job with priority.
- Handles active-job dedupe by returning existing active job on unique violation.

### `public.claim_market_price_refresh_jobs(worker_id, max_jobs)`

- Claims queued jobs with `FOR UPDATE SKIP LOCKED`.
- Marks claimed jobs as `running`.
- Updates `market_price_cache.refresh_status` to `running` for claimed keys.

### `public.complete_market_price_refresh_job(...)`

- Marks running job completed.
- Updates cache freshness/status pointers without changing prior values unless provided.

### `public.fail_market_price_refresh_job(...)`

- Marks running job failed or re-queued depending on retry settings.
- Increments `attempt_count`.
- Updates cache status/error while preserving old price values.

### `public.get_market_price_bundle(fingerprint, evidence_limit)`

- Read helper returning key + cache + latest snapshot + sold evidence list.
- Intended for read-side integration and diagnostics.

## RLS design

### Public read tables

`anon` + `authenticated` can `select` from:

- `market_price_keys`
- `market_price_cache`
- `market_price_snapshots`
- `market_sold_listing_evidence`

### Refresh job table

- `authenticated` can `select` only their own rows (`requested_by_user_id = auth.uid()`).
- `service_role` has full access.
- Inserts are expected through RPCs, not direct app table inserts.

### Service role

- `service_role` policies allow full access to all market tables.
- Service/worker execution should use service-role credentials.

## Index and constraint highlights

- Active dedupe: `idx_market_price_refresh_jobs_one_active_per_key` partial unique index.
- Queue pickup: `(status, priority desc, requested_at asc)`.
- Snapshot/history queries: `(price_key_id, created_at desc)`.
- Evidence reads: `(price_key_id, sold_date desc)` and snapshot index.
- Soft dedupe for listing URLs per provider/marketplace via partial unique index.

## Worker vs Supabase responsibility split

### Supabase/Postgres

- Shared identity uniqueness.
- Queue dedupe and safe claim semantics.
- Transactional state transitions for queue + cache pointers.
- Access control via RLS and role grants.

### Worker (future phases)

- Provider calls.
- Listing filtering and scoring.
- Price/confidence calculation.
- Snapshot and evidence writes.
- Cache update payload composition.

## Local SQL validation checklist

Run these checks in a Supabase SQL environment after applying migration:

1. Confirm tables exist:
   - `market_price_keys`
   - `market_price_cache`
   - `market_price_snapshots`
   - `market_sold_listing_evidence`
   - `market_price_refresh_jobs`
2. Confirm active dedupe index exists and is partial:
   - `idx_market_price_refresh_jobs_one_active_per_key`
3. Confirm RLS is enabled on all market tables.
4. Confirm functions exist and are executable by intended roles:
   - `get_or_create_market_price_key`
   - `enqueue_market_price_refresh`
   - `claim_market_price_refresh_jobs`
   - `complete_market_price_refresh_job`
   - `fail_market_price_refresh_job`
   - `get_market_price_bundle`
5. Smoke test key + queue path:
   - Call `get_or_create_market_price_key` with a test fingerprint.
   - Enqueue twice for same `price_key_id`; verify second call returns existing active job.
   - Claim with `claim_market_price_refresh_jobs`; verify status changes to `running`.
   - Call fail/complete RPC and verify queue/cache status transitions.
6. Confirm direct app user read restrictions on `market_price_refresh_jobs` (own rows only).

## Open decisions carried forward

- Whether `raw_json` should be exposed directly to app clients or only via service-side views.
- Whether future fingerprint needs explicit graded-card dimensions.
- Whether marketplace region (for example `ebay_au`) should be in the fingerprint beyond country/currency.
