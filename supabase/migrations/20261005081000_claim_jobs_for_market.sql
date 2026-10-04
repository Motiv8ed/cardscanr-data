-- Claim queued pricing jobs for one market only so a US/GB/CA canary
-- cannot steal AU queued work.

create or replace function public.claim_market_price_refresh_jobs_for_market(
  p_worker_id text,
  p_max_jobs integer default 1,
  p_market_country text default null
)
returns setof public.market_price_refresh_jobs
language plpgsql
security definer
set search_path = public
as $$
begin
  if p_market_country is null or char_length(trim(p_market_country)) = 0 then
    raise exception 'market_country required' using errcode = '22023';
  end if;

  return query
  with candidates as (
    select j.id
    from public.market_price_refresh_jobs j
    join public.market_price_keys k on k.id = j.price_key_id
    where j.status = 'queued'
      and lower(k.market_country) = lower(trim(p_market_country))
    order by j.priority asc, j.requested_at asc
    for update of j skip locked
    limit greatest(1, least(coalesce(p_max_jobs, 1), 100))
  ),
  claimed as (
    update public.market_price_refresh_jobs as j
    set
      status = 'running',
      attempt_count = j.attempt_count + 1,
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
    insert into public.market_price_cache (
      price_key_id,
      refresh_status,
      market_country,
      currency,
      last_error_message,
      updated_at
    )
    select
      claimed.price_key_id,
      'running',
      upper(k.market_country),
      upper(k.currency),
      null,
      now()
    from claimed
    join public.market_price_keys as k on k.id = claimed.price_key_id
    on conflict (price_key_id) do update
    set
      refresh_status = 'running',
      market_country = coalesce(public.market_price_cache.market_country, excluded.market_country),
      currency = coalesce(public.market_price_cache.currency, excluded.currency),
      last_error_message = null,
      updated_at = now()
    returning id
  )
  select * from claimed
  order by priority asc, requested_at asc;
end;
$$;

revoke all on function public.claim_market_price_refresh_jobs_for_market(text, integer, text) from public, anon;
grant execute on function public.claim_market_price_refresh_jobs_for_market(text, integer, text) to service_role;
