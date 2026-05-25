-- Phase 1: Market Price Engine schema + queue RPCs

create extension if not exists pgcrypto;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create table if not exists public.market_price_keys (
  id uuid primary key default gen_random_uuid(),
  game text not null,
  card_name text not null,
  normalized_card_name text not null,
  set_name text not null,
  set_code text,
  collector_number text not null,
  language text not null,
  variant text not null,
  condition text not null,
  market_country text not null,
  currency text not null,
  fingerprint text not null unique,
  popularity_score integer not null default 0,
  inventory_count integer not null default 0,
  last_seen_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint market_price_keys_non_negative_popularity check (popularity_score >= 0),
  constraint market_price_keys_non_negative_inventory check (inventory_count >= 0),
  constraint market_price_keys_game_lowercase check (game = lower(game)),
  constraint market_price_keys_language_lowercase check (language = lower(language)),
  constraint market_price_keys_variant_lowercase check (variant = lower(variant)),
  constraint market_price_keys_condition_lowercase check (condition = lower(condition)),
  constraint market_price_keys_country_lowercase check (market_country = lower(market_country)),
  constraint market_price_keys_currency_lowercase check (currency = lower(currency)),
  constraint market_price_keys_fingerprint_lowercase check (fingerprint = lower(fingerprint)),
  constraint market_price_keys_fingerprint_not_blank check (length(trim(fingerprint)) > 0)
);

create table if not exists public.market_price_snapshots (
  id uuid primary key default gen_random_uuid(),
  price_key_id uuid not null references public.market_price_keys(id) on delete cascade,
  provider text not null,
  marketplace text not null,
  query_used text,
  median_price numeric(12,2),
  low_price numeric(12,2),
  average_price numeric(12,2),
  high_price numeric(12,2),
  recommended_price numeric(12,2),
  sample_size integer not null default 0,
  confidence text not null default 'unknown',
  included_count integer not null default 0,
  rejected_count integer not null default 0,
  diagnostics_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint market_price_snapshots_non_negative_sample_size check (sample_size >= 0),
  constraint market_price_snapshots_non_negative_included_count check (included_count >= 0),
  constraint market_price_snapshots_non_negative_rejected_count check (rejected_count >= 0),
  constraint market_price_snapshots_confidence_valid check (confidence in ('high', 'medium', 'low', 'unknown')),
  constraint market_price_snapshots_median_non_negative check (median_price is null or median_price >= 0),
  constraint market_price_snapshots_low_non_negative check (low_price is null or low_price >= 0),
  constraint market_price_snapshots_average_non_negative check (average_price is null or average_price >= 0),
  constraint market_price_snapshots_high_non_negative check (high_price is null or high_price >= 0),
  constraint market_price_snapshots_recommended_non_negative check (recommended_price is null or recommended_price >= 0)
);

create table if not exists public.market_price_cache (
  id uuid primary key default gen_random_uuid(),
  price_key_id uuid not null unique references public.market_price_keys(id) on delete cascade,
  current_market_price numeric(12,2),
  median_price numeric(12,2),
  low_price numeric(12,2),
  average_price numeric(12,2),
  high_price numeric(12,2),
  recommended_price numeric(12,2),
  sample_size integer not null default 0,
  confidence text not null default 'unknown',
  provider text,
  marketplace text,
  market_country text,
  currency text,
  last_updated_at timestamptz,
  stale_after timestamptz,
  next_refresh_due_at timestamptz,
  refresh_status text not null default 'queued',
  latest_snapshot_id uuid references public.market_price_snapshots(id) on delete set null,
  last_error_message text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint market_price_cache_non_negative_sample_size check (sample_size >= 0),
  constraint market_price_cache_confidence_valid check (confidence in ('high', 'medium', 'low', 'unknown')),
  constraint market_price_cache_refresh_status_valid check (refresh_status in ('queued', 'running', 'completed', 'failed', 'stale', 'disabled')),
  constraint market_price_cache_current_non_negative check (current_market_price is null or current_market_price >= 0),
  constraint market_price_cache_median_non_negative check (median_price is null or median_price >= 0),
  constraint market_price_cache_low_non_negative check (low_price is null or low_price >= 0),
  constraint market_price_cache_average_non_negative check (average_price is null or average_price >= 0),
  constraint market_price_cache_high_non_negative check (high_price is null or high_price >= 0),
  constraint market_price_cache_recommended_non_negative check (recommended_price is null or recommended_price >= 0)
);

create table if not exists public.market_sold_listing_evidence (
  id uuid primary key default gen_random_uuid(),
  price_key_id uuid not null references public.market_price_keys(id) on delete cascade,
  snapshot_id uuid not null references public.market_price_snapshots(id) on delete cascade,
  provider text not null,
  marketplace text not null,
  title text not null,
  sold_price numeric(12,2),
  shipping_price numeric(12,2),
  total_price numeric(12,2),
  currency text,
  sold_date timestamptz,
  listing_url text,
  condition_text text,
  match_score numeric(5,4),
  included_in_estimate boolean not null,
  rejection_reason text,
  raw_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint market_sold_listing_evidence_sold_non_negative check (sold_price is null or sold_price >= 0),
  constraint market_sold_listing_evidence_shipping_non_negative check (shipping_price is null or shipping_price >= 0),
  constraint market_sold_listing_evidence_total_non_negative check (total_price is null or total_price >= 0),
  constraint market_sold_listing_evidence_match_score_range check (match_score is null or (match_score >= 0 and match_score <= 1))
);

create table if not exists public.market_price_refresh_jobs (
  id uuid primary key default gen_random_uuid(),
  price_key_id uuid not null references public.market_price_keys(id) on delete cascade,
  requested_by_user_id uuid references auth.users(id) on delete set null,
  reason text not null,
  priority smallint not null default 40,
  status text not null default 'queued',
  attempt_count integer not null default 0,
  requested_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz,
  error_message text,
  created_snapshot_id uuid references public.market_price_snapshots(id) on delete set null,
  worker_id text,
  locked_at timestamptz,
  dedupe_key text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint market_price_refresh_jobs_priority_valid check (priority >= 0 and priority <= 100),
  constraint market_price_refresh_jobs_status_valid check (status in ('queued', 'running', 'completed', 'failed', 'cancelled')),
  constraint market_price_refresh_jobs_attempt_count_non_negative check (attempt_count >= 0)
);

create index if not exists idx_market_price_keys_fingerprint on public.market_price_keys (fingerprint);
create index if not exists idx_market_price_keys_lookup on public.market_price_keys (game, language, market_country, currency);
create index if not exists idx_market_price_keys_updated_at on public.market_price_keys (updated_at desc);

create index if not exists idx_market_price_cache_next_refresh on public.market_price_cache (next_refresh_due_at, refresh_status);
create index if not exists idx_market_price_cache_last_updated on public.market_price_cache (last_updated_at desc);
create index if not exists idx_market_price_cache_confidence_sample on public.market_price_cache (confidence, sample_size);

create index if not exists idx_market_price_snapshots_key_created on public.market_price_snapshots (price_key_id, created_at desc);
create index if not exists idx_market_price_snapshots_provider_market_created on public.market_price_snapshots (provider, marketplace, created_at desc);

create index if not exists idx_market_sold_listing_evidence_snapshot on public.market_sold_listing_evidence (snapshot_id);
create index if not exists idx_market_sold_listing_evidence_key_sold_date on public.market_sold_listing_evidence (price_key_id, sold_date desc);
create index if not exists idx_market_sold_listing_evidence_included on public.market_sold_listing_evidence (price_key_id, sold_date desc) where included_in_estimate = true;
create unique index if not exists idx_market_sold_listing_evidence_provider_market_url_unique
  on public.market_sold_listing_evidence (provider, marketplace, listing_url)
  where listing_url is not null and length(trim(listing_url)) > 0;

create index if not exists idx_market_price_refresh_jobs_status_priority_requested
  on public.market_price_refresh_jobs (status, priority desc, requested_at asc);
create index if not exists idx_market_price_refresh_jobs_price_key_status
  on public.market_price_refresh_jobs (price_key_id, status);
create index if not exists idx_market_price_refresh_jobs_requested_by_requested_at
  on public.market_price_refresh_jobs (requested_by_user_id, requested_at desc);
create unique index if not exists idx_market_price_refresh_jobs_one_active_per_key
  on public.market_price_refresh_jobs (price_key_id)
  where status in ('queued', 'running');

create trigger trg_market_price_keys_set_updated_at
before update on public.market_price_keys
for each row execute function public.set_updated_at();

create trigger trg_market_price_cache_set_updated_at
before update on public.market_price_cache
for each row execute function public.set_updated_at();

create trigger trg_market_price_refresh_jobs_set_updated_at
before update on public.market_price_refresh_jobs
for each row execute function public.set_updated_at();

alter table public.market_price_keys enable row level security;
alter table public.market_price_cache enable row level security;
alter table public.market_price_snapshots enable row level security;
alter table public.market_sold_listing_evidence enable row level security;
alter table public.market_price_refresh_jobs enable row level security;

create policy if not exists market_price_keys_read_public
on public.market_price_keys
for select to anon, authenticated
using (true);

create policy if not exists market_price_keys_service_role_all
on public.market_price_keys
for all to service_role
using (true)
with check (true);

create policy if not exists market_price_cache_read_public
on public.market_price_cache
for select to anon, authenticated
using (true);

create policy if not exists market_price_cache_service_role_all
on public.market_price_cache
for all to service_role
using (true)
with check (true);

create policy if not exists market_price_snapshots_read_public
on public.market_price_snapshots
for select to anon, authenticated
using (true);

create policy if not exists market_price_snapshots_service_role_all
on public.market_price_snapshots
for all to service_role
using (true)
with check (true);

create policy if not exists market_sold_listing_evidence_read_public
on public.market_sold_listing_evidence
for select to anon, authenticated
using (true);

create policy if not exists market_sold_listing_evidence_service_role_all
on public.market_sold_listing_evidence
for all to service_role
using (true)
with check (true);

create policy if not exists market_price_refresh_jobs_read_own
on public.market_price_refresh_jobs
for select to authenticated
using (requested_by_user_id = auth.uid());

create policy if not exists market_price_refresh_jobs_service_role_all
on public.market_price_refresh_jobs
for all to service_role
using (true)
with check (true);

grant select on public.market_price_keys to anon, authenticated;
grant select on public.market_price_cache to anon, authenticated;
grant select on public.market_price_snapshots to anon, authenticated;
grant select on public.market_sold_listing_evidence to anon, authenticated;
grant select on public.market_price_refresh_jobs to authenticated;
grant all privileges on public.market_price_keys to service_role;
grant all privileges on public.market_price_cache to service_role;
grant all privileges on public.market_price_snapshots to service_role;
grant all privileges on public.market_sold_listing_evidence to service_role;
grant all privileges on public.market_price_refresh_jobs to service_role;

create or replace function public.get_or_create_market_price_key(
  p_game text,
  p_card_name text,
  p_normalized_card_name text,
  p_set_name text,
  p_set_code text,
  p_collector_number text,
  p_language text,
  p_variant text,
  p_condition text,
  p_market_country text,
  p_currency text,
  p_fingerprint text,
  p_last_seen_at timestamptz default now()
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id uuid;
begin
  insert into public.market_price_keys (
    game,
    card_name,
    normalized_card_name,
    set_name,
    set_code,
    collector_number,
    language,
    variant,
    condition,
    market_country,
    currency,
    fingerprint,
    last_seen_at
  )
  values (
    lower(trim(p_game)),
    trim(p_card_name),
    lower(trim(p_normalized_card_name)),
    trim(p_set_name),
    nullif(trim(p_set_code), ''),
    trim(p_collector_number),
    lower(trim(p_language)),
    lower(trim(p_variant)),
    lower(trim(p_condition)),
    lower(trim(p_market_country)),
    lower(trim(p_currency)),
    lower(trim(p_fingerprint)),
    p_last_seen_at
  )
  on conflict (fingerprint) do update
  set
    card_name = excluded.card_name,
    normalized_card_name = excluded.normalized_card_name,
    set_name = excluded.set_name,
    set_code = excluded.set_code,
    collector_number = excluded.collector_number,
    language = excluded.language,
    variant = excluded.variant,
    condition = excluded.condition,
    market_country = excluded.market_country,
    currency = excluded.currency,
    last_seen_at = case
      when public.market_price_keys.last_seen_at is null then excluded.last_seen_at
      when excluded.last_seen_at is null then public.market_price_keys.last_seen_at
      else greatest(public.market_price_keys.last_seen_at, excluded.last_seen_at)
    end,
    updated_at = now()
  returning id into v_id;

  return v_id;
end;
$$;

create or replace function public.enqueue_market_price_refresh(
  p_price_key_id uuid,
  p_reason text,
  p_priority smallint default 40,
  p_requested_by_user_id uuid default null,
  p_dedupe_key text default null
)
returns public.market_price_refresh_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
  v_job public.market_price_refresh_jobs;
  v_requested_by_user_id uuid;
begin
  if p_priority < 0 or p_priority > 100 then
    raise exception 'priority must be between 0 and 100';
  end if;

  v_requested_by_user_id := coalesce(p_requested_by_user_id, auth.uid());

  begin
    insert into public.market_price_refresh_jobs (
      price_key_id,
      requested_by_user_id,
      reason,
      priority,
      status,
      requested_at,
      dedupe_key
    )
    values (
      p_price_key_id,
      v_requested_by_user_id,
      nullif(trim(p_reason), ''),
      p_priority,
      'queued',
      now(),
      nullif(trim(p_dedupe_key), '')
    )
    returning * into v_job;
  exception when unique_violation then
    select * into v_job
    from public.market_price_refresh_jobs
    where price_key_id = p_price_key_id
      and status in ('queued', 'running')
    order by
      case status when 'running' then 0 else 1 end,
      priority desc,
      requested_at asc
    limit 1;
  end;

  if v_job.id is null then
    raise exception 'failed to enqueue or locate active refresh job for key %', p_price_key_id;
  end if;

  return v_job;
end;
$$;

create or replace function public.claim_market_price_refresh_jobs(
  p_worker_id text,
  p_max_jobs integer default 1
)
returns setof public.market_price_refresh_jobs
language plpgsql
security definer
set search_path = public
as $$
begin
  return query
  with candidates as (
    select id
    from public.market_price_refresh_jobs
    where status = 'queued'
    order by priority desc, requested_at asc
    for update skip locked
    limit greatest(1, least(coalesce(p_max_jobs, 1), 100))
  ),
  claimed as (
    update public.market_price_refresh_jobs as j
    set
      status = 'running',
      started_at = coalesce(j.started_at, now()),
      worker_id = coalesce(nullif(trim(p_worker_id), ''), 'market-worker'),
      locked_at = now(),
      error_message = null,
      updated_at = now()
    from candidates
    where j.id = candidates.id
    returning j.*
  ),
  cache_state as (
    update public.market_price_cache as c
    set
      refresh_status = 'running',
      updated_at = now()
    where c.price_key_id in (select price_key_id from claimed)
    returning c.id
  )
  select * from claimed
  order by priority desc, requested_at asc;
end;
$$;

create or replace function public.complete_market_price_refresh_job(
  p_job_id uuid,
  p_snapshot_id uuid,
  p_cache_updated_at timestamptz default now(),
  p_stale_after timestamptz default null,
  p_next_refresh_due_at timestamptz default null
)
returns public.market_price_refresh_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
  v_job public.market_price_refresh_jobs;
begin
  update public.market_price_refresh_jobs
  set
    status = 'completed',
    completed_at = now(),
    created_snapshot_id = coalesce(p_snapshot_id, created_snapshot_id),
    error_message = null,
    worker_id = null,
    locked_at = null,
    updated_at = now()
  where id = p_job_id
    and status = 'running'
  returning * into v_job;

  if v_job.id is null then
    raise exception 'running refresh job not found for id %', p_job_id;
  end if;

  update public.market_price_cache
  set
    latest_snapshot_id = coalesce(p_snapshot_id, latest_snapshot_id),
    last_updated_at = coalesce(p_cache_updated_at, now()),
    stale_after = coalesce(p_stale_after, stale_after),
    next_refresh_due_at = coalesce(p_next_refresh_due_at, next_refresh_due_at),
    refresh_status = 'completed',
    last_error_message = null,
    updated_at = now()
  where price_key_id = v_job.price_key_id;

  return v_job;
end;
$$;

create or replace function public.fail_market_price_refresh_job(
  p_job_id uuid,
  p_error_message text,
  p_retryable boolean default true,
  p_retry_delay_minutes integer default 15,
  p_max_attempts integer default 3
)
returns public.market_price_refresh_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
  v_job public.market_price_refresh_jobs;
  v_next_status text;
begin
  update public.market_price_refresh_jobs
  set
    attempt_count = attempt_count + 1,
    status = case
      when p_retryable and (attempt_count + 1) < greatest(1, p_max_attempts) then 'queued'
      else 'failed'
    end,
    completed_at = case
      when p_retryable and (attempt_count + 1) < greatest(1, p_max_attempts) then null
      else now()
    end,
    error_message = p_error_message,
    worker_id = null,
    locked_at = null,
    updated_at = now()
  where id = p_job_id
    and status = 'running'
  returning * into v_job;

  if v_job.id is null then
    raise exception 'running refresh job not found for id %', p_job_id;
  end if;

  v_next_status := v_job.status;

  update public.market_price_cache
  set
    refresh_status = case when v_next_status = 'queued' then 'queued' else 'failed' end,
    last_error_message = p_error_message,
    next_refresh_due_at = case
      when v_next_status = 'queued' then now() + make_interval(mins => greatest(1, p_retry_delay_minutes))
      else next_refresh_due_at
    end,
    updated_at = now()
  where price_key_id = v_job.price_key_id;

  return v_job;
end;
$$;

create or replace function public.get_market_price_bundle(
  p_fingerprint text,
  p_evidence_limit integer default 50
)
returns jsonb
language plpgsql
security definer
stable
set search_path = public
as $$
declare
  v_key public.market_price_keys;
  v_cache public.market_price_cache;
  v_snapshot public.market_price_snapshots;
  v_evidence jsonb := '[]'::jsonb;
begin
  select * into v_key
  from public.market_price_keys
  where fingerprint = lower(trim(p_fingerprint))
  limit 1;

  if v_key.id is null then
    return null;
  end if;

  select * into v_cache
  from public.market_price_cache
  where price_key_id = v_key.id
  limit 1;

  if v_cache.latest_snapshot_id is not null then
    select * into v_snapshot
    from public.market_price_snapshots
    where id = v_cache.latest_snapshot_id
    limit 1;
  else
    select * into v_snapshot
    from public.market_price_snapshots
    where price_key_id = v_key.id
    order by created_at desc
    limit 1;
  end if;

  select coalesce(
    jsonb_agg(
      jsonb_build_object(
        'id', e.id,
        'provider', e.provider,
        'marketplace', e.marketplace,
        'title', e.title,
        'sold_price', e.sold_price,
        'shipping_price', e.shipping_price,
        'total_price', e.total_price,
        'currency', e.currency,
        'sold_date', e.sold_date,
        'listing_url', e.listing_url,
        'condition_text', e.condition_text,
        'match_score', e.match_score,
        'included_in_estimate', e.included_in_estimate,
        'rejection_reason', e.rejection_reason,
        'created_at', e.created_at
      )
    ),
    '[]'::jsonb
  )
  into v_evidence
  from (
    select e.*
    from public.market_sold_listing_evidence as e
    where e.price_key_id = v_key.id
      and (v_snapshot.id is null or e.snapshot_id = v_snapshot.id)
    order by e.sold_date desc nulls last, e.created_at desc
    limit greatest(1, least(coalesce(p_evidence_limit, 50), 500))
  ) as e;

  return jsonb_build_object(
    'price_key', to_jsonb(v_key),
    'cache', to_jsonb(v_cache),
    'latest_snapshot', to_jsonb(v_snapshot),
    'sold_listing_evidence', v_evidence
  );
end;
$$;

revoke all on function public.get_or_create_market_price_key(
  text, text, text, text, text, text, text, text, text, text, text, text, timestamptz
) from public;
revoke all on function public.enqueue_market_price_refresh(uuid, text, smallint, uuid, text) from public;
revoke all on function public.claim_market_price_refresh_jobs(text, integer) from public;
revoke all on function public.complete_market_price_refresh_job(uuid, uuid, timestamptz, timestamptz, timestamptz) from public;
revoke all on function public.fail_market_price_refresh_job(uuid, text, boolean, integer, integer) from public;
revoke all on function public.get_market_price_bundle(text, integer) from public;

grant execute on function public.get_or_create_market_price_key(
  text, text, text, text, text, text, text, text, text, text, text, text, timestamptz
) to authenticated, service_role;
grant execute on function public.enqueue_market_price_refresh(uuid, text, smallint, uuid, text) to authenticated, service_role;
grant execute on function public.claim_market_price_refresh_jobs(text, integer) to service_role;
grant execute on function public.complete_market_price_refresh_job(uuid, uuid, timestamptz, timestamptz, timestamptz) to service_role;
grant execute on function public.fail_market_price_refresh_job(uuid, text, boolean, integer, integer) to service_role;
grant execute on function public.get_market_price_bundle(text, integer) to anon, authenticated, service_role;
