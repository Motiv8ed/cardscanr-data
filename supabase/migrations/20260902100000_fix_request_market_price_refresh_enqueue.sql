-- Fix enqueue_market_price_refresh priority cast (integer overload removed).
-- Pass optional PPID through to get_or_create_market_price_key.

drop function if exists public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text,
  boolean, text, text, jsonb
);

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
  p_force_refresh boolean default false,
  p_canonical_name_en text default null,
  p_original_name_ja text default null,
  p_aliases jsonb default '[]'::jsonb,
  p_physical_printing_id text default null,
  p_identity_model_version text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
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
  v_requested_reason text;
  v_force_allowed boolean := false;
  v_dedupe_key text;
  v_supported_route boolean := false;
  v_cache_state text := 'missing';
  v_aliases jsonb;
  v_recent_user_jobs integer;
begin
  if auth.uid() is null and coalesce(auth.role(), '') <> 'service_role' then
    raise exception 'Authentication required' using errcode = '42501';
  end if;

  -- Reject abusive force_refresh loops from clients.
  if coalesce(p_force_refresh, false) then
    v_force_allowed := coalesce(auth.role(), '') = 'service_role';
    if not v_force_allowed then
      raise exception 'force_refresh is reserved for service_role'
        using errcode = '42501';
    end if;
  end if;

  -- Payload size validation (reject oversized; truncate optional long fields).
  if p_game is null or char_length(trim(p_game)) = 0 or char_length(p_game) > 64 then
    raise exception 'game must be 1..64 characters' using errcode = '22023';
  end if;
  if p_card_name is null or char_length(trim(p_card_name)) = 0
     or char_length(p_card_name) > 300 then
    raise exception 'card_name must be 1..300 characters' using errcode = '22023';
  end if;
  if p_normalized_card_name is null or char_length(trim(p_normalized_card_name)) = 0
     or char_length(p_normalized_card_name) > 300 then
    raise exception 'normalized_card_name must be 1..300 characters'
      using errcode = '22023';
  end if;
  if p_set_name is null or char_length(trim(p_set_name)) = 0
     or char_length(p_set_name) > 300 then
    raise exception 'set_name must be 1..300 characters' using errcode = '22023';
  end if;
  if p_set_code is not null and char_length(p_set_code) > 64 then
    raise exception 'set_code must be <= 64 characters' using errcode = '22023';
  end if;
  if p_collector_number is null or char_length(trim(p_collector_number)) = 0
     or char_length(p_collector_number) > 64 then
    raise exception 'collector_number must be 1..64 characters' using errcode = '22023';
  end if;
  if p_language is null or char_length(trim(p_language)) = 0
     or char_length(p_language) > 16 then
    raise exception 'language must be 1..16 characters' using errcode = '22023';
  end if;
  if p_variant is null or char_length(trim(p_variant)) = 0
     or char_length(p_variant) > 120 then
    raise exception 'variant must be 1..120 characters' using errcode = '22023';
  end if;
  if p_condition is null or char_length(trim(p_condition)) = 0
     or char_length(p_condition) > 120 then
    raise exception 'condition must be 1..120 characters' using errcode = '22023';
  end if;
  if p_market_country is null or char_length(trim(p_market_country)) = 0
     or char_length(p_market_country) > 8 then
    raise exception 'market_country must be 1..8 characters' using errcode = '22023';
  end if;
  if p_currency is null or char_length(trim(p_currency)) = 0
     or char_length(p_currency) > 8 then
    raise exception 'currency must be 1..8 characters' using errcode = '22023';
  end if;
  if p_fingerprint is null or char_length(trim(p_fingerprint)) = 0
     or char_length(p_fingerprint) > 200 then
    raise exception 'fingerprint must be 1..200 characters' using errcode = '22023';
  end if;
  if p_canonical_name_en is not null and char_length(p_canonical_name_en) > 300 then
    raise exception 'canonical_name_en must be <= 300 characters' using errcode = '22023';
  end if;
  if p_original_name_ja is not null and char_length(p_original_name_ja) > 300 then
    raise exception 'original_name_ja must be <= 300 characters' using errcode = '22023';
  end if;

  v_requested_reason := left(
    coalesce(nullif(trim(p_reason), ''), 'user_refresh'),
    120
  );

  v_aliases := case
    when jsonb_typeof(coalesce(p_aliases, '[]'::jsonb)) = 'array'
      then coalesce(p_aliases, '[]'::jsonb)
    else '[]'::jsonb
  end;
  if jsonb_array_length(v_aliases) > 50 then
    raise exception 'aliases must contain at most 50 entries' using errcode = '22023';
  end if;
  if pg_column_size(v_aliases) > 8192 then
    raise exception 'aliases payload too large' using errcode = '22023';
  end if;

  -- Soft per-user enqueue rate limit (reuse job table; does not bypass key cooldown).
  if auth.uid() is not null and coalesce(auth.role(), '') <> 'service_role' then
    select count(*) into v_recent_user_jobs
    from public.market_price_refresh_jobs
    where requested_by_user_id = auth.uid()
      and requested_at > now() - interval '1 minute';
    if coalesce(v_recent_user_jobs, 0) >= 10 then
      raise exception 'Too many refresh requests; try again shortly'
        using errcode = '54000';
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
    now(),
    p_canonical_name_en,
    p_original_name_ja,
    v_aliases,
    p_physical_printing_id,
    p_identity_model_version
  );

  select * into v_key
  from public.market_price_keys
  where id = v_price_key_id
  limit 1;

  v_supported_route := public.market_price_supported_route(
    v_key.market_country, v_key.currency, 'ebay'
  );

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
      'state', case
        when v_active_job.status = 'running' then 'refresh_running'
        else 'refresh_queued'
      end,
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
      'current_market_evidence_available',
        v_cache.current_market_price is not null and v_cache.sample_size > 0,
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

  -- Existing per-key cooldown (fresh cache blocks enqueue unless service force).
  if v_cache.id is not null and v_cache_is_fresh and not coalesce(p_force_refresh, false) then
    return jsonb_build_object(
      'action', 'cache_fresh',
      'state', case
        when coalesce(v_cache.sample_size, 0) = 0 and v_cache.last_updated_at is not null
          then 'no_evidence_found'
        when v_cache.current_market_price is null and v_cache.last_updated_at is not null
          then 'no_current_market_evidence'
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
      'current_market_evidence_available',
        v_cache.current_market_price is not null and v_cache.sample_size > 0,
      'stale_cache_available', false,
      'active_refresh_job', null
    );
  end if;

  v_dedupe_key := 'request_market_price_refresh:'
    || v_price_key_id::text || ':' || gen_random_uuid()::text;

  v_job := public.enqueue_market_price_refresh(
    v_price_key_id,
    v_requested_reason,
    10::smallint,
    auth.uid(),
    v_dedupe_key
  );

  if v_job.dedupe_key is distinct from v_dedupe_key then
    return jsonb_build_object(
      'action', 'active_job_exists',
      'state', case
        when v_job.status = 'running' then 'refresh_running'
        else 'refresh_queued'
      end,
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
      'current_market_evidence_available',
        v_cache.current_market_price is not null and v_cache.sample_size > 0,
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
    'state', case
      when v_cache_state = 'stale' then 'stale_cache_refresh_queued'
      else 'refresh_queued'
    end,
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
    'current_market_evidence_available',
      v_cache.current_market_price is not null and v_cache.sample_size > 0,
    'stale_cache_available', v_cache_state = 'stale',
    'active_refresh_job', jsonb_build_object(
      'id', v_job.id,
      'status', v_job.status,
      'priority', v_job.priority,
      'reason', v_job.reason,
      'requested_at', v_job.requested_at
    )
  );
end;
$$;


revoke all on function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text,
  boolean, text, text, jsonb, text, text
) from public, anon;
grant execute on function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text,
  boolean, text, text, jsonb, text, text
) to authenticated, service_role;

comment on function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text,
  boolean, text, text, jsonb, text, text
) is
  'Authenticated market refresh request. enqueue uses smallint priority; optional PPID.';
