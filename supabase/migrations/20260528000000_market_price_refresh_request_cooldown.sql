-- Shared cache cooldown and app-safe market refresh request gate.

create or replace function public.market_price_refresh_cooldown_hours(
  p_cache public.market_price_cache,
  p_key public.market_price_keys
)
returns table(cooldown_hours integer, cooldown_reason text)
language plpgsql
stable
set search_path = public
as $$
declare
  v_default integer := 6;
  v_high_value integer := 4;
  v_popular integer := 4;
  v_hot_card integer := 2;
  v_low_value integer := 12;
  v_is_high_value boolean;
  v_is_popular boolean;
  v_is_low_value_common boolean;
begin
  v_is_high_value :=
    coalesce(p_cache.current_market_price >= 100, false)
    or coalesce(p_cache.recommended_price >= 100, false);

  v_is_popular :=
    coalesce(p_key.popularity_score, 0) >= 10
    or coalesce(p_key.inventory_count, 0) >= 10;

  v_is_low_value_common :=
    p_cache.current_market_price is not null
    and p_cache.recommended_price is not null
    and p_cache.current_market_price < 10
    and p_cache.recommended_price < 10
    and coalesce(p_key.popularity_score, 0) < 3
    and coalesce(p_key.inventory_count, 0) < 3;

  if v_is_high_value and v_is_popular then
    cooldown_hours := v_hot_card;
    cooldown_reason := 'hot_card';
  elsif v_is_high_value then
    cooldown_hours := v_high_value;
    cooldown_reason := 'high_value';
  elsif v_is_popular then
    cooldown_hours := v_popular;
    cooldown_reason := 'popular';
  elsif v_is_low_value_common then
    cooldown_hours := v_low_value;
    cooldown_reason := 'low_value_common';
  else
    cooldown_hours := v_default;
    cooldown_reason := 'default';
  end if;

  return next;
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

  select * into v_cache
  from public.market_price_cache
  where price_key_id = v_price_key_id
  limit 1;

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
    return jsonb_build_object(
      'action', 'active_job_exists',
      'price_key_id', v_price_key_id,
      'job_id', v_active_job.id,
      'job_status', v_active_job.status,
      'cache_last_updated_at', v_cache.last_updated_at,
      'cooldown_hours', v_cooldown_hours,
      'cooldown_until', v_cooldown_until,
      'cooldown_reason', v_cooldown_reason,
      'cache_is_fresh', v_cache_is_fresh,
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
      'price_key_id', v_price_key_id,
      'job_id', null,
      'job_status', null,
      'cache_last_updated_at', v_cache.last_updated_at,
      'cooldown_hours', v_cooldown_hours,
      'cooldown_until', v_cooldown_until,
      'cooldown_reason', v_cooldown_reason,
      'cache_is_fresh', true,
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
      'price_key_id', v_price_key_id,
      'job_id', v_job.id,
      'job_status', v_job.status,
      'cache_last_updated_at', v_cache.last_updated_at,
      'cooldown_hours', v_cooldown_hours,
      'cooldown_until', v_cooldown_until,
      'cooldown_reason', v_cooldown_reason,
      'cache_is_fresh', v_cache_is_fresh,
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
    'price_key_id', v_price_key_id,
    'job_id', v_job.id,
    'job_status', v_job.status,
    'cache_last_updated_at', v_cache.last_updated_at,
    'cooldown_hours', v_cooldown_hours,
    'cooldown_until', v_cooldown_until,
    'cooldown_reason', v_cooldown_reason,
    'cache_is_fresh', v_cache_is_fresh,
    'active_refresh_job', null
  );
end;
$$;

revoke all on function public.market_price_refresh_cooldown_hours(
  public.market_price_cache,
  public.market_price_keys
) from public;
revoke all on function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text, boolean
) from public;

grant execute on function public.market_price_refresh_cooldown_hours(
  public.market_price_cache,
  public.market_price_keys
) to service_role;
grant execute on function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text, boolean
) to authenticated, service_role;
