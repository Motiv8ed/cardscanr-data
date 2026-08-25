-- Market price pipeline heartbeats + abandoned running-job recovery.
-- Additive. Does not rewrite historical pricing rows.

create table if not exists public.market_price_pipeline_heartbeats (
  component text primary key,
  worker_id text,
  version text,
  state text not null default 'unknown',
  last_heartbeat_at timestamptz not null default now(),
  meta jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint market_price_pipeline_heartbeats_component_nonempty
    check (length(trim(component)) > 0),
  constraint market_price_pipeline_heartbeats_meta_object
    check (jsonb_typeof(meta) = 'object')
);

comment on table public.market_price_pipeline_heartbeats is
  'Lightweight scheduler/worker heartbeat for pricing pipeline health.';

create index if not exists idx_market_price_pipeline_heartbeats_last
  on public.market_price_pipeline_heartbeats (last_heartbeat_at desc);

alter table public.market_price_pipeline_heartbeats enable row level security;

grant select, insert, update, delete on table public.market_price_pipeline_heartbeats to service_role;
grant select on table public.market_price_pipeline_heartbeats to authenticated;
revoke all on table public.market_price_pipeline_heartbeats from anon;
revoke insert, update, delete, truncate, references, trigger on table public.market_price_pipeline_heartbeats from authenticated;

drop policy if exists market_price_pipeline_heartbeats_service_role_all
  on public.market_price_pipeline_heartbeats;
create policy market_price_pipeline_heartbeats_service_role_all
  on public.market_price_pipeline_heartbeats
  for all to service_role
  using (true)
  with check (true);

drop policy if exists market_price_pipeline_heartbeats_admin_read
  on public.market_price_pipeline_heartbeats;
create policy market_price_pipeline_heartbeats_admin_read
  on public.market_price_pipeline_heartbeats
  for select to authenticated
  using (public.is_beta_admin());

create or replace function public.recover_abandoned_market_price_refresh_jobs(
  p_stale_after_minutes integer default 90,
  p_max_jobs integer default 25
)
returns setof public.market_price_refresh_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
  v_cutoff timestamptz;
begin
  v_cutoff := now() - make_interval(mins => greatest(15, least(coalesce(p_stale_after_minutes, 90), 24 * 60)));

  return query
  with stuck as (
    select j.id
    from public.market_price_refresh_jobs j
    where j.status = 'running'
      and coalesce(j.locked_at, j.started_at, j.requested_at) < v_cutoff
    order by coalesce(j.locked_at, j.started_at, j.requested_at) asc nulls first
    for update skip locked
    limit greatest(1, least(coalesce(p_max_jobs, 25), 100))
  ),
  failed as (
    update public.market_price_refresh_jobs as j
    set
      status = 'failed',
      completed_at = now(),
      locked_at = null,
      worker_id = null,
      error_message = left(
        coalesce(j.error_message || ' | ', '')
        || 'abandoned_stale_lock:worker_lock_exceeded_threshold',
        1000
      ),
      updated_at = now()
    from stuck
    where j.id = stuck.id
    returning j.*
  ),
  cache_state as (
    update public.market_price_cache as c
    set
      refresh_status = 'failed',
      last_error_message = 'abandoned_stale_lock:worker_lock_exceeded_threshold',
      updated_at = now()
    where c.price_key_id in (select price_key_id from failed)
    returning c.price_key_id
  )
  select * from failed;
end;
$$;

revoke all on function public.recover_abandoned_market_price_refresh_jobs(integer, integer)
  from public, anon, authenticated;
grant execute on function public.recover_abandoned_market_price_refresh_jobs(integer, integer)
  to service_role;

comment on function public.recover_abandoned_market_price_refresh_jobs(integer, integer) is
  'Fails abandoned running refresh jobs whose locks exceeded the stale threshold.';
