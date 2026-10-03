-- Harden owned bootstrap: resolve home market from profile preference chain,
-- enqueue when missing OR stale (>24h success) OR failed-retry due.
-- Does not mutate ownership. No PII returned.

create or replace function public.customer_bootstrap_collection_price_keys(
  p_market_country text default null,
  p_currency text default null,
  p_max_refresh integer default 10
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_uid uuid := auth.uid();
  v_profile public.user_profiles%rowtype;
  v_resolved record;
  v_market text;
  v_currency text;
  v_resolution_source text;
  v_limit integer := greatest(0, least(coalesce(p_max_refresh, 10), 15));
  v_keys_ensured integer := 0;
  v_keys_created_or_touched integer := 0;
  v_enqueued integer := 0;
  v_already_priced integer := 0;
  v_reused_fresh integer := 0;
  v_remaining_unpriced integer := 0;
  v_active_jobs integer := 0;
  v_row record;
  v_fp text;
  v_key_id uuid;
  v_has_price boolean;
  v_last_updated timestamptz;
  v_refresh_status text;
  v_next_due timestamptz;
  v_needs_refresh boolean;
  v_refresh jsonb;
  v_norm_variant text;
  v_norm_condition text;
begin
  if v_uid is null then
    raise exception 'Authentication required' using errcode = '42501';
  end if;

  select * into v_profile from public.user_profiles where id = v_uid;

  if lower(trim(coalesce(p_market_country, ''))) in ('au', 'us', 'gb', 'ca') then
    v_market := lower(trim(p_market_country));
    v_currency := lower(trim(coalesce(nullif(p_currency, ''),
      case v_market
        when 'au' then 'aud'
        when 'us' then 'usd'
        when 'gb' then 'gbp'
        when 'ca' then 'cad'
      end
    )));
    v_resolution_source := 'explicit_call_arg';
  else
    select * into v_resolved
    from public.cardscanr_resolve_owner_home_pricing_market(
      v_profile.pricing_market,
      v_profile.country_code,
      v_profile.currency_code
    );
    v_market := v_resolved.market_country;
    v_currency := v_resolved.currency;
    v_resolution_source := v_resolved.resolution_source;
  end if;

  if v_market not in ('au', 'us', 'gb', 'ca') or v_currency not in ('aud', 'usd', 'gbp', 'cad') then
    raise exception 'Unsupported market/currency' using errcode = '22023';
  end if;

  for v_row in
    select
      c.id,
      c.card_name,
      c.set_name,
      c.set_id,
      c.collector_number,
      c.language,
      c.variant,
      c.condition,
      nullif(trim(c.physical_printing_id), '') as physical_printing_id,
      nullif(trim(c.identity_model_version), '') as identity_model_version
    from public.customer_collection_items c
    where c.user_id = v_uid
      and c.deleted_at is null
      and greatest(coalesce(c.quantity, 0), 0) > 0
    order by c.updated_at desc nulls last
  loop
    v_fp := public.cardscanr_market_price_fingerprint(
      v_row.card_name,
      v_row.set_name,
      v_row.set_id,
      v_row.collector_number,
      v_row.language,
      v_row.variant,
      v_row.condition,
      v_market,
      v_currency
    );

    v_norm_variant := coalesce(nullif(trim(v_row.variant), ''), 'Normal');
    v_norm_condition := coalesce(nullif(trim(v_row.condition), ''), 'Unreviewed');

    v_key_id := public.get_or_create_market_price_key(
      'pokemon',
      v_row.card_name,
      public.cardscanr_normalize_price_text(v_row.card_name),
      v_row.set_name,
      v_row.set_id,
      v_row.collector_number,
      coalesce(nullif(trim(v_row.language), ''), 'en'),
      v_norm_variant,
      v_norm_condition,
      v_market,
      v_currency,
      v_fp,
      now(),
      null,
      null,
      '[]'::jsonb,
      v_row.physical_printing_id,
      v_row.identity_model_version
    );
    v_keys_ensured := v_keys_ensured + 1;
    v_keys_created_or_touched := v_keys_created_or_touched + 1;

    select
      (cache.current_market_price is not null and cache.current_market_price > 0),
      cache.last_updated_at,
      lower(coalesce(cache.refresh_status, '')),
      cache.next_refresh_due_at
    into v_has_price, v_last_updated, v_refresh_status, v_next_due
    from public.market_price_cache cache
    where cache.price_key_id = v_key_id;

    v_needs_refresh := false;
    if not coalesce(v_has_price, false) then
      v_needs_refresh := true;
    elsif v_refresh_status = 'failed'
      and (v_next_due is null or v_next_due <= now()) then
      v_needs_refresh := true;
    elsif v_last_updated is null
      or v_last_updated <= (now() - interval '24 hours') then
      v_needs_refresh := true;
    else
      v_already_priced := v_already_priced + 1;
      v_reused_fresh := v_reused_fresh + 1;
      continue;
    end if;

    if exists (
      select 1
      from public.market_price_refresh_jobs j
      where j.price_key_id = v_key_id
        and j.status in ('queued', 'running')
    ) then
      v_active_jobs := v_active_jobs + 1;
      continue;
    end if;

    if not v_needs_refresh or v_enqueued >= v_limit then
      continue;
    end if;

    begin
      v_refresh := public.request_market_price_refresh(
        'pokemon',
        v_row.card_name,
        public.cardscanr_normalize_price_text(v_row.card_name),
        v_row.set_name,
        v_row.set_id,
        v_row.collector_number,
        coalesce(nullif(trim(v_row.language), ''), 'en'),
        v_norm_variant,
        v_norm_condition,
        v_market,
        v_currency,
        v_fp,
        'portal_owned_new_or_stale',
        false,
        null,
        null,
        '[]'::jsonb,
        v_row.physical_printing_id,
        v_row.identity_model_version
      );
      if coalesce(v_refresh->>'action', '') in ('job_enqueued', 'active_job_exists') then
        v_enqueued := v_enqueued + 1;
      elsif coalesce(v_refresh->>'action', '') = 'cache_fresh'
        and coalesce((v_refresh->>'cache_has_current_price')::boolean, false) then
        v_already_priced := v_already_priced + 1;
        v_reused_fresh := v_reused_fresh + 1;
      end if;
    exception
      when others then
        null;
    end;
  end loop;

  select count(*)::integer
  into v_remaining_unpriced
  from public.customer_collection_items c
  where c.user_id = v_uid
    and c.deleted_at is null
    and greatest(coalesce(c.quantity, 0), 0) > 0
    and not exists (
      select 1
      from public.market_price_cache cache
      where cache.price_key_id = public.resolve_market_price_key_id(
        nullif(trim(c.physical_printing_id), ''),
        public.cardscanr_market_price_fingerprint(
          c.card_name, c.set_name, c.set_id, c.collector_number, c.language,
          c.variant, c.condition, v_market, v_currency
        ),
        v_market,
        v_currency
      )
        and cache.current_market_price is not null
        and cache.current_market_price > 0
    );

  return jsonb_build_object(
    'market_country', v_market,
    'currency', upper(v_currency),
    'resolution_source', v_resolution_source,
    'keys_ensured', v_keys_ensured,
    'keys_touched', v_keys_created_or_touched,
    'refresh_actions', v_enqueued,
    'already_priced', v_already_priced,
    'reused_fresh_shared_cache', v_reused_fresh,
    'active_jobs_seen', v_active_jobs,
    'remaining_unpriced', v_remaining_unpriced,
    'max_refresh', v_limit,
    'processed_all_owned', true
  );
end;
$$;

comment on function public.customer_bootstrap_collection_price_keys(text, text, integer) is
  'Ensure owned printing×home-market keys; enqueue when missing/stale/failed. Reuses fresh shared cache.';
