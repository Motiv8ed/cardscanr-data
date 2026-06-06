-- Make refresh/cache state explicit for app reads.
--
-- A refresh request can legitimately succeed before current market evidence
-- exists.  This migration ensures those intermediate and failed states still
-- have a cache row, without fabricating prices.

drop index if exists public.idx_market_sold_listing_evidence_provider_market_url_unique;

create unique index if not exists idx_market_sold_listing_evidence_snapshot_provider_market_url_unique
  on public.market_sold_listing_evidence (snapshot_id, provider, marketplace, listing_url)
  where listing_url is not null and length(trim(listing_url)) > 0;

create or replace function public.market_price_supported_route(
  p_market_country text,
  p_currency text,
  p_marketplace text default 'ebay'
)
returns boolean
language sql
stable
set search_path = public
as $$
  select lower(coalesce(nullif(trim(p_marketplace), ''), 'ebay')) = 'ebay'
    and (upper(trim(coalesce(p_market_country, ''))), upper(trim(coalesce(p_currency, '')))) in (
      ('AU', 'AUD'),
      ('US', 'USD'),
      ('GB', 'GBP'),
      ('CA', 'CAD')
    );
$$;

create or replace function public.upsert_market_price_refresh_cache_state(
  p_price_key_id uuid,
  p_refresh_status text,
  p_market_country text,
  p_currency text,
  p_error_message text default null
)
returns public.market_price_cache
language plpgsql
security definer
set search_path = public
as $$
declare
  v_cache public.market_price_cache;
begin
  insert into public.market_price_cache (
    price_key_id,
    refresh_status,
    market_country,
    currency,
    last_error_message,
    updated_at
  )
  values (
    p_price_key_id,
    p_refresh_status,
    nullif(upper(trim(coalesce(p_market_country, ''))), ''),
    nullif(upper(trim(coalesce(p_currency, ''))), ''),
    p_error_message,
    now()
  )
  on conflict (price_key_id) do update
  set
    refresh_status = excluded.refresh_status,
    market_country = coalesce(public.market_price_cache.market_country, excluded.market_country),
    currency = coalesce(public.market_price_cache.currency, excluded.currency),
    last_error_message = case
      when excluded.refresh_status in ('queued', 'running', 'completed') then null
      else excluded.last_error_message
    end,
    updated_at = now()
  returning * into v_cache;

  return v_cache;
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
  v_key public.market_price_keys;
  v_requested_by_user_id uuid;
begin
  if p_priority < 0 or p_priority > 100 then
    raise exception 'priority must be between 0 and 100';
  end if;

  select * into v_key
  from public.market_price_keys
  where id = p_price_key_id
  limit 1;

  if v_key.id is null then
    raise exception 'market price key not found for id %', p_price_key_id;
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
      priority asc,
      requested_at asc
    limit 1;
  end;

  if v_job.id is null then
    raise exception 'failed to enqueue or locate active refresh job for key %', p_price_key_id;
  end if;

  perform public.upsert_market_price_refresh_cache_state(
    v_job.price_key_id,
    v_job.status,
    v_key.market_country,
    v_key.currency,
    null
  );

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
    order by priority asc, requested_at asc
    for update skip locked
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
  v_key public.market_price_keys;
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

  select * into v_key
  from public.market_price_keys
  where id = v_job.price_key_id
  limit 1;

  insert into public.market_price_cache (
    price_key_id,
    refresh_status,
    market_country,
    currency,
    latest_snapshot_id,
    last_updated_at,
    stale_after,
    next_refresh_due_at,
    last_error_message,
    updated_at
  )
  values (
    v_job.price_key_id,
    'completed',
    upper(v_key.market_country),
    upper(v_key.currency),
    p_snapshot_id,
    coalesce(p_cache_updated_at, now()),
    p_stale_after,
    p_next_refresh_due_at,
    null,
    now()
  )
  on conflict (price_key_id) do update
  set
    latest_snapshot_id = coalesce(excluded.latest_snapshot_id, public.market_price_cache.latest_snapshot_id),
    last_updated_at = coalesce(excluded.last_updated_at, now()),
    stale_after = coalesce(excluded.stale_after, public.market_price_cache.stale_after),
    next_refresh_due_at = coalesce(excluded.next_refresh_due_at, public.market_price_cache.next_refresh_due_at),
    refresh_status = 'completed',
    market_country = coalesce(public.market_price_cache.market_country, excluded.market_country),
    currency = coalesce(public.market_price_cache.currency, excluded.currency),
    last_error_message = null,
    updated_at = now();

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
  v_key public.market_price_keys;
begin
  update public.market_price_refresh_jobs
  set
    status = 'failed',
    completed_at = now(),
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

  select * into v_key
  from public.market_price_keys
  where id = v_job.price_key_id
  limit 1;

  insert into public.market_price_cache (
    price_key_id,
    refresh_status,
    market_country,
    currency,
    last_error_message,
    updated_at
  )
  values (
    v_job.price_key_id,
    'failed',
    upper(v_key.market_country),
    upper(v_key.currency),
    p_error_message,
    now()
  )
  on conflict (price_key_id) do update
  set
    refresh_status = 'failed',
    market_country = coalesce(public.market_price_cache.market_country, excluded.market_country),
    currency = coalesce(public.market_price_cache.currency, excluded.currency),
    last_error_message = excluded.last_error_message,
    updated_at = now();

  return v_job;
end;
$$;

create or replace function public.request_market_price_refresh(
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
  p_reason text default 'user_refresh',
  p_force_refresh boolean default false
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_price_key_id uuid;
  v_key public.market_price_keys;
  v_cache public.market_price_cache;
  v_active_job public.market_price_refresh_jobs;
  v_job public.market_price_refresh_jobs;
  v_cooldown_hours integer := 6;
  v_cooldown_reason text := 'default';
  v_cooldown_until timestamptz;
  v_cache_is_fresh boolean := false;
  v_requested_reason text := coalesce(nullif(trim(p_reason), ''), 'user_refresh');
  v_force_allowed boolean := false;
  v_dedupe_key text;
  v_supported_route boolean := false;
  v_cache_state text := 'missing';
begin
  if coalesce(p_force_refresh, false) then
    v_force_allowed := coalesce(auth.role(), '') = 'service_role';
    if not v_force_allowed then
      raise exception 'force_refresh is reserved for service_role'
        using errcode = '42501';
    end if;
  end if;

  v_price_key_id := public.get_or_create_market_price_key(
    p_game,
    p_card_name,
    p_normalized_card_name,
    p_set_name,
    p_set_code,
    p_collector_number,
    p_language,
    p_variant,
    p_condition,
    p_market_country,
    p_currency,
    p_fingerprint,
    now()
  );

  select * into v_key
  from public.market_price_keys
  where id = v_price_key_id
  limit 1;

  v_supported_route := public.market_price_supported_route(v_key.market_country, v_key.currency, 'ebay');

  select * into v_cache
  from public.market_price_cache
  where price_key_id = v_price_key_id
  limit 1;

  if v_cache.id is not null then
    if v_cache.refresh_status = 'failed' then
      v_cache_state := 'failed';
    elsif v_cache.last_updated_at is null then
      v_cache_state := coalesce(v_cache.refresh_status, 'missing');
    elsif v_cache.stale_after is not null and now() >= v_cache.stale_after then
      v_cache_state := 'stale';
    else
      v_cache_state := 'fresh';
    end if;
  end if;

  if not v_supported_route then
    v_cache := public.upsert_market_price_refresh_cache_state(
      v_price_key_id,
      'disabled',
      v_key.market_country,
      v_key.currency,
      'unsupported_market'
    );
    return jsonb_build_object(
      'action', 'unsupported_market',
      'state', 'unsupported_market',
      'price_key_id', v_price_key_id,
      'job_id', null,
      'job_status', null,
      'cache_last_updated_at', v_cache.last_updated_at,
      'cooldown_hours', v_cooldown_hours,
      'cooldown_until', null,
      'cooldown_reason', 'unsupported_market',
      'cache_is_fresh', false,
      'cache_state', 'unsupported_market',
      'refresh_state', 'disabled',
      'cache_has_current_price', false,
      'current_market_evidence_available', false,
      'stale_cache_available', false,
      'active_refresh_job', null
    );
  end if;

  select * into v_active_job
  from public.market_price_refresh_jobs
  where price_key_id = v_price_key_id
    and status in ('queued', 'running')
  order by
    case status when 'running' then 0 else 1 end,
    priority asc,
    requested_at asc
  limit 1;

  select c.cooldown_hours, c.cooldown_reason
  into v_cooldown_hours, v_cooldown_reason
  from public.market_price_refresh_cooldown_hours(v_cache, v_key) as c
  limit 1;

  if v_cache.last_updated_at is not null then
    v_cooldown_until := v_cache.last_updated_at + make_interval(hours => v_cooldown_hours);
    v_cache_is_fresh := now() < v_cooldown_until;
  end if;

  if v_active_job.id is not null then
    v_cache := public.upsert_market_price_refresh_cache_state(
      v_price_key_id,
      v_active_job.status,
      v_key.market_country,
      v_key.currency,
      null
    );
    return jsonb_build_object(
      'action', 'active_job_exists',
      'state', case when v_active_job.status = 'running' then 'refresh_running' else 'refresh_queued' end,
      'price_key_id', v_price_key_id,
      'job_id', v_active_job.id,
      'job_status', v_active_job.status,
      'cache_last_updated_at', v_cache.last_updated_at,
      'cooldown_hours', v_cooldown_hours,
      'cooldown_until', v_cooldown_until,
      'cooldown_reason', v_cooldown_reason,
      'cache_is_fresh', v_cache_is_fresh,
      'cache_state', v_cache_state,
      'refresh_state', v_active_job.status,
      'cache_has_current_price', v_cache.current_market_price is not null,
      'current_market_evidence_available', v_cache.current_market_price is not null and v_cache.sample_size > 0,
      'stale_cache_available', v_cache_state = 'stale',
      'active_refresh_job', jsonb_build_object(
        'id', v_active_job.id,
        'status', v_active_job.status,
        'priority', v_active_job.priority,
        'reason', v_active_job.reason,
        'requested_at', v_active_job.requested_at
      )
    );
  end if;

  if v_cache.id is not null and v_cache_is_fresh and not coalesce(p_force_refresh, false) then
    return jsonb_build_object(
      'action', 'cache_fresh',
      'state', case
        when coalesce(v_cache.sample_size, 0) = 0 and v_cache.last_updated_at is not null then 'no_evidence_found'
        when v_cache.current_market_price is null and v_cache.last_updated_at is not null then 'no_current_market_evidence'
        else 'existing_fresh_cache'
      end,
      'price_key_id', v_price_key_id,
      'job_id', null,
      'job_status', null,
      'cache_last_updated_at', v_cache.last_updated_at,
      'cooldown_hours', v_cooldown_hours,
      'cooldown_until', v_cooldown_until,
      'cooldown_reason', v_cooldown_reason,
      'cache_is_fresh', true,
      'cache_state', 'fresh',
      'refresh_state', 'cooldown',
      'cache_has_current_price', v_cache.current_market_price is not null,
      'current_market_evidence_available', v_cache.current_market_price is not null and v_cache.sample_size > 0,
      'stale_cache_available', false,
      'active_refresh_job', null
    );
  end if;

  v_dedupe_key := 'request_market_price_refresh:' || v_price_key_id::text || ':' || gen_random_uuid()::text;

  v_job := public.enqueue_market_price_refresh(
    v_price_key_id,
    v_requested_reason,
    10,
    auth.uid(),
    v_dedupe_key
  );

  if v_job.dedupe_key is distinct from v_dedupe_key then
    return jsonb_build_object(
      'action', 'active_job_exists',
      'state', case when v_job.status = 'running' then 'refresh_running' else 'refresh_queued' end,
      'price_key_id', v_price_key_id,
      'job_id', v_job.id,
      'job_status', v_job.status,
      'cache_last_updated_at', v_cache.last_updated_at,
      'cooldown_hours', v_cooldown_hours,
      'cooldown_until', v_cooldown_until,
      'cooldown_reason', v_cooldown_reason,
      'cache_is_fresh', v_cache_is_fresh,
      'cache_state', v_cache_state,
      'refresh_state', v_job.status,
      'cache_has_current_price', v_cache.current_market_price is not null,
      'current_market_evidence_available', v_cache.current_market_price is not null and v_cache.sample_size > 0,
      'stale_cache_available', v_cache_state = 'stale',
      'active_refresh_job', jsonb_build_object(
        'id', v_job.id,
        'status', v_job.status,
        'priority', v_job.priority,
        'reason', v_job.reason,
        'requested_at', v_job.requested_at
      )
    );
  end if;

  return jsonb_build_object(
    'action', 'job_enqueued',
    'state', case when v_cache_state = 'stale' then 'stale_cache_refresh_queued' else 'refresh_queued' end,
    'price_key_id', v_price_key_id,
    'job_id', v_job.id,
    'job_status', v_job.status,
    'cache_last_updated_at', v_cache.last_updated_at,
    'cooldown_hours', v_cooldown_hours,
    'cooldown_until', v_cooldown_until,
    'cooldown_reason', v_cooldown_reason,
    'cache_is_fresh', v_cache_is_fresh,
    'cache_state', v_cache_state,
    'refresh_state', 'queued',
    'cache_has_current_price', v_cache.current_market_price is not null,
    'current_market_evidence_available', v_cache.current_market_price is not null and v_cache.sample_size > 0,
    'stale_cache_available', v_cache_state = 'stale',
    'active_refresh_job', null
  );
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
  v_active_job public.market_price_refresh_jobs;
  v_evidence jsonb := '[]'::jsonb;
  v_cache_state text := 'missing';
  v_state text := 'cache_missing_unexpected';
  v_no_reliable_price_reason text;
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

  v_no_reliable_price_reason := v_snapshot.diagnostics_json->>'no_reliable_price_reason';

  select * into v_active_job
  from public.market_price_refresh_jobs
  where price_key_id = v_key.id
    and status in ('queued', 'running')
  order by
    case status when 'running' then 0 else 1 end,
    priority asc,
    requested_at asc
  limit 1;

  if v_cache.id is not null then
    if v_cache.refresh_status = 'disabled' then
      v_cache_state := 'unsupported_market';
    elsif v_cache.refresh_status = 'failed' then
      v_cache_state := 'failed';
    elsif v_cache.refresh_status in ('queued', 'running') then
      v_cache_state := v_cache.refresh_status;
    elsif v_cache.last_updated_at is not null and v_cache.stale_after is not null and now() >= v_cache.stale_after then
      v_cache_state := 'stale';
    elsif v_cache.last_updated_at is not null then
      v_cache_state := 'fresh';
    else
      v_cache_state := 'missing';
    end if;
  end if;

  if v_active_job.id is not null then
    v_state := case when v_active_job.status = 'running' then 'refresh_running' else 'refresh_queued' end;
  elsif v_cache_state = 'unsupported_market' then
    v_state := 'unsupported_market';
  elsif v_cache_state = 'failed' then
    v_state := 'provider_failed';
  elsif v_cache.id is null then
    v_state := 'cache_missing_unexpected';
  elsif v_cache_state = 'stale' then
    v_state := 'stale_cache';
  elsif coalesce(v_cache.sample_size, 0) = 0 and v_cache.last_updated_at is not null then
    v_state := 'no_evidence_found';
  elsif v_cache.current_market_price is null and v_cache.last_updated_at is not null then
    v_state := coalesce(v_no_reliable_price_reason, 'no_current_market_evidence');
  else
    v_state := 'existing_fresh_cache';
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
    'active_refresh_job', to_jsonb(v_active_job),
    'sold_listing_evidence', v_evidence,
    'state', v_state,
    'cache_state', v_cache_state,
    'refresh_state', coalesce(v_active_job.status, v_cache.refresh_status),
    'cache_has_current_price', coalesce(v_cache.current_market_price is not null, false),
    'current_market_evidence_available', coalesce(v_cache.current_market_price is not null and v_cache.sample_size > 0, false),
    'stale_cache_available', v_cache_state = 'stale',
    'no_reliable_price_reason', v_no_reliable_price_reason
  );
end;
$$;

revoke all on function public.market_price_supported_route(text, text, text) from public;
revoke all on function public.upsert_market_price_refresh_cache_state(uuid, text, text, text, text) from public;

grant execute on function public.market_price_supported_route(text, text, text) to authenticated, service_role;
grant execute on function public.upsert_market_price_refresh_cache_state(uuid, text, text, text, text) to service_role;
