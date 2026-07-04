-- Preserve structured Japanese source identity on market price keys.

alter table public.market_price_keys
  add column if not exists canonical_name_en text,
  add column if not exists original_name_ja text,
  add column if not exists aliases jsonb not null default '[]'::jsonb;

drop function if exists public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text, boolean
);

drop function if exists public.get_or_create_market_price_key(
  text, text, text, text, text, text, text, text, text, text, text, text, timestamptz
);

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
  p_last_seen_at timestamptz default now(),
  p_canonical_name_en text default null,
  p_original_name_ja text default null,
  p_aliases jsonb default '[]'::jsonb
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id uuid;
  v_aliases jsonb := case
    when jsonb_typeof(coalesce(p_aliases, '[]'::jsonb)) = 'array'
      then coalesce(p_aliases, '[]'::jsonb)
    else '[]'::jsonb
  end;
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
    last_seen_at,
    canonical_name_en,
    original_name_ja,
    aliases
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
    p_last_seen_at,
    nullif(trim(p_canonical_name_en), ''),
    nullif(trim(p_original_name_ja), ''),
    v_aliases
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
    canonical_name_en = coalesce(excluded.canonical_name_en, public.market_price_keys.canonical_name_en),
    original_name_ja = coalesce(excluded.original_name_ja, public.market_price_keys.original_name_ja),
    aliases = case
      when excluded.aliases = '[]'::jsonb then public.market_price_keys.aliases
      else excluded.aliases
    end,
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
  p_aliases jsonb default '[]'::jsonb
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
    now(),
    p_canonical_name_en,
    p_original_name_ja,
    p_aliases
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

grant execute on function public.get_or_create_market_price_key(
  text, text, text, text, text, text, text, text, text, text, text, text, timestamptz, text, text, jsonb
) to service_role;

grant execute on function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text, boolean, text, text, jsonb
) to authenticated, service_role;
