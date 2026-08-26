-- Hybrid bulk/reference pricing: provenance columns + provider sync state.

alter table public.market_price_cache
  add column if not exists reference_price numeric(12,2),
  add column if not exists reference_provider text,
  add column if not exists reference_updated_at timestamptz,
  add column if not exists display_price_source text,
  add column if not exists verification_required boolean not null default false,
  add column if not exists verification_reason text;

create table if not exists public.market_price_provider_sync_runs (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running',
  keys_scanned integer not null default 0,
  keys_matched integer not null default 0,
  keys_updated integer not null default 0,
  keys_unchanged integer not null default 0,
  keys_quarantined integer not null default 0,
  keys_unresolved integer not null default 0,
  keys_ambiguous integer not null default 0,
  verification_enqueued integer not null default 0,
  errors integer not null default 0,
  duration_ms integer,
  bulk_keys_per_hour numeric(12,2),
  diagnostics_json jsonb not null default '{}'::jsonb,
  constraint market_price_provider_sync_runs_status_valid
    check (status in ('running', 'success', 'failed'))
);

create index if not exists market_price_provider_sync_runs_provider_started_idx
  on public.market_price_provider_sync_runs (provider, started_at desc);

alter table public.market_price_provider_sync_runs enable row level security;

revoke all on table public.market_price_provider_sync_runs from public, anon, authenticated;
grant select, insert, update, delete on table public.market_price_provider_sync_runs to service_role;

comment on table public.market_price_provider_sync_runs is
  'Bulk/reference provider sync telemetry for CardScanR hybrid pricing.';
