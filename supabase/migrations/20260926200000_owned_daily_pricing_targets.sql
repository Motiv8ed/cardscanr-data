-- Daily owned-card pricing targets.
-- Distinct owned canonical printings x home pricing market (AU/US/GB/CA).
-- No PII in returned payloads. Service-role / admin ops only for listing.

create or replace function public.cardscanr_resolve_owner_home_pricing_market(
  p_pricing_market text,
  p_country_code text,
  p_currency_code text default null
)
returns table (
  market_country text,
  currency text,
  resolution_source text,
  has_native_sold_route boolean
)
language plpgsql
immutable
set search_path = public, pg_temp
as $$
declare
  v_pricing text := upper(trim(coalesce(p_pricing_market, '')));
  v_country text := upper(trim(coalesce(p_country_code, '')));
  v_currency text := upper(trim(coalesce(p_currency_code, '')));
  v_market text;
  v_cur text;
  v_source text;
begin
  -- 1) Explicit pricing-market preference (EBAY_* or country-like).
  if v_pricing in ('EBAY_AU', 'AU', 'AUSTRALIA') then
    market_country := 'au'; currency := 'aud'; resolution_source := 'explicit_pricing_market';
    has_native_sold_route := true; return next; return;
  elsif v_pricing in ('EBAY_US', 'US', 'USA', 'UNITED STATES') then
    market_country := 'us'; currency := 'usd'; resolution_source := 'explicit_pricing_market';
    has_native_sold_route := true; return next; return;
  elsif v_pricing in ('EBAY_GB', 'GB', 'UK', 'UNITED KINGDOM') then
    market_country := 'gb'; currency := 'gbp'; resolution_source := 'explicit_pricing_market';
    has_native_sold_route := true; return next; return;
  elsif v_pricing in ('EBAY_CA', 'CA', 'CANADA') then
    market_country := 'ca'; currency := 'cad'; resolution_source := 'explicit_pricing_market';
    has_native_sold_route := true; return next; return;
  end if;

  -- 2) Account region / country.
  if v_country in ('AU', 'AUSTRALIA') then
    v_market := 'au'; v_cur := 'aud'; v_source := 'account_region';
  elsif v_country in ('US', 'USA', 'UNITED STATES') then
    v_market := 'us'; v_cur := 'usd'; v_source := 'account_region';
  elsif v_country in ('GB', 'UK', 'UNITED KINGDOM') then
    v_market := 'gb'; v_cur := 'gbp'; v_source := 'account_region';
  elsif v_country in ('CA', 'CANADA') then
    v_market := 'ca'; v_cur := 'cad'; v_source := 'account_region';
  elsif v_country <> '' and v_country <> 'GLOBAL' then
    -- Non-native sold home: still need a sold market key; AU/AUD final sold fallback.
    v_market := 'au'; v_cur := 'aud'; v_source := 'account_region_no_native_sold_fallback_au';
  else
    v_market := null;
  end if;

  if v_market is not null then
    if v_currency in ('AUD', 'USD', 'GBP', 'CAD') then
      -- Prefer account currency only when it matches the native market pairing.
      if (v_market = 'au' and v_currency = 'AUD')
         or (v_market = 'us' and v_currency = 'USD')
         or (v_market = 'gb' and v_currency = 'GBP')
         or (v_market = 'ca' and v_currency = 'CAD') then
        v_cur := lower(v_currency);
      end if;
    end if;
    market_country := v_market;
    currency := v_cur;
    resolution_source := v_source;
    has_native_sold_route := v_source = 'account_region';
    return next;
    return;
  end if;

  -- 3) Application default (device locale is client-only; not available server-side here).
  market_country := 'au';
  currency := 'aud';
  resolution_source := 'application_default';
  has_native_sold_route := true;
  return next;
end;
$$;

revoke all on function public.cardscanr_resolve_owner_home_pricing_market(text, text, text)
  from public, anon;
grant execute on function public.cardscanr_resolve_owner_home_pricing_market(text, text, text)
  to authenticated, service_role;

-- ---------------------------------------------------------------------------
-- List distinct owned printing x market contexts (no user ids).
-- ---------------------------------------------------------------------------
create or replace function public.list_owned_market_pricing_targets(
  p_include_zero_owners boolean default false
)
returns jsonb
language plpgsql
security definer
stable
set search_path = public, pg_temp
as $$
declare
  v_result jsonb;
begin
  -- Prefer service_role / admin for fleet listing. Authenticated non-admin may not see fleet.
  if auth.role() <> 'service_role' then
    begin
      perform public.beta_require_admin();
    exception when others then
      raise exception 'Admin or service role required' using errcode = '42501';
    end;
  end if;

  with owned_rows as (
    select
      c.user_id,
      nullif(trim(c.physical_printing_id), '') as physical_printing_id,
      coalesce(nullif(trim(c.card_name), ''), '') as card_name,
      coalesce(nullif(trim(c.set_name), ''), '') as set_name,
      coalesce(nullif(trim(c.set_id), ''), '') as set_id,
      coalesce(nullif(trim(c.collector_number), ''), '') as collector_number,
      coalesce(nullif(trim(c.language), ''), 'en') as language,
      coalesce(nullif(trim(c.variant), ''), 'raw') as variant,
      coalesce(nullif(trim(c.condition), ''), 'raw') as condition,
      greatest(coalesce(c.quantity, 0), 0)::integer as quantity,
      r.market_country,
      r.currency,
      r.resolution_source
    from public.customer_collection_items c
    left join public.user_profiles p on p.id = c.user_id
    cross join lateral public.cardscanr_resolve_owner_home_pricing_market(
      p.pricing_market,
      p.country_code,
      p.currency_code
    ) r
    where c.deleted_at is null
      and coalesce(trim(c.card_name), '') <> ''
      and coalesce(trim(c.collector_number), '') <> ''
      and (
        coalesce(trim(c.set_id), '') <> ''
        or coalesce(trim(c.set_name), '') <> ''
      )
      and greatest(coalesce(c.quantity, 0), 0) > 0
  ),
  keyed as (
    select
      o.*,
      public.cardscanr_market_price_fingerprint(
        o.card_name,
        o.set_name,
        o.set_id,
        o.collector_number,
        o.language,
        o.variant,
        o.condition,
        o.market_country,
        o.currency
      ) as fingerprint
    from owned_rows o
  ),
  aggregated as (
    select
      fingerprint,
      market_country,
      currency,
      max(physical_printing_id) filter (where physical_printing_id is not null) as physical_printing_id,
      max(card_name) as card_name,
      max(set_name) as set_name,
      max(set_id) as set_id,
      max(collector_number) as collector_number,
      max(language) as language,
      max(variant) as variant,
      max(condition) as condition,
      count(distinct user_id)::integer as owner_count,
      sum(quantity)::integer as total_owned_quantity,
      max(resolution_source) as sample_resolution_source
    from keyed
    group by fingerprint, market_country, currency
  ),
  joined as (
    select
      a.*,
      k.id as market_price_key_id,
      cache.current_market_price,
      cache.provider,
      cache.display_price_source,
      cache.refresh_status,
      cache.last_updated_at,
      cache.stale_after,
      cache.next_refresh_due_at,
      cache.last_error_message,
      cache.international_source_market,
      case
        when cache.current_market_price is null or cache.current_market_price <= 0 then 'unpriced'
        when cache.stale_after is not null and cache.stale_after <= now() then 'stale'
        when cache.last_updated_at is not null
          and cache.last_updated_at <= (now() - interval '24 hours') then 'stale_gt_24h'
        when cache.last_updated_at is not null then 'fresh_lt_24h'
        else 'unknown'
      end as price_state,
      case
        when cache.current_market_price is null or cache.current_market_price <= 0 then 'P0_NEVER_PRICED'
        when lower(coalesce(cache.refresh_status, '')) = 'failed' then 'P2_FAILED_RETRY'
        when cache.last_updated_at is null
          or cache.last_updated_at <= (now() - interval '24 hours')
          or (cache.stale_after is not null and cache.stale_after <= now())
          then 'P1_STALE_GT_24H'
        when cache.last_updated_at is not null
          and cache.last_updated_at <= (now() - interval '22 hours')
          then 'P3_APPROACHING_DUE'
        else 'FRESH_SKIP'
      end as owned_priority_band,
      case
        when cache.current_market_price is null or cache.current_market_price <= 0 then true
        when lower(coalesce(cache.refresh_status, '')) = 'failed'
          and (
            cache.next_refresh_due_at is null
            or cache.next_refresh_due_at <= now()
          ) then true
        when cache.last_updated_at is null
          or cache.last_updated_at <= (now() - interval '24 hours') then true
        when cache.stale_after is not null and cache.stale_after <= now()
          and cache.last_updated_at <= (now() - interval '24 hours') then true
        else false
      end as due_for_owned_daily
    from aggregated a
    left join public.market_price_keys k
      on k.fingerprint = a.fingerprint
    left join public.market_price_cache cache
      on cache.price_key_id = k.id
    where p_include_zero_owners or a.owner_count > 0
  )
  select coalesce(jsonb_agg(to_jsonb(j) order by
    case j.owned_priority_band
      when 'P0_NEVER_PRICED' then 0
      when 'P1_STALE_GT_24H' then 1
      when 'P2_FAILED_RETRY' then 2
      when 'P3_APPROACHING_DUE' then 3
      else 9
    end,
    j.owner_count desc,
    j.total_owned_quantity desc,
    j.last_updated_at asc nulls first,
    j.fingerprint
  ), '[]'::jsonb)
  into v_result
  from joined j;

  return jsonb_build_object(
    'capturedAtUtc', now(),
    'targetCount', jsonb_array_length(v_result),
    'targets', v_result
  );
end;
$$;

revoke all on function public.list_owned_market_pricing_targets(boolean) from public, anon;
grant execute on function public.list_owned_market_pricing_targets(boolean) to service_role;

-- ---------------------------------------------------------------------------
-- Ensure keys exist + stamp inventory_count for owned printing x market.
-- ---------------------------------------------------------------------------
create or replace function public.sync_owned_market_price_keys()
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_payload jsonb;
  v_targets jsonb;
  v_row jsonb;
  v_key_id uuid;
  v_ensured integer := 0;
  v_created integer := 0;
  v_stamped integer := 0;
  v_fp text;
  v_existing uuid;
begin
  if auth.role() <> 'service_role' then
    begin
      perform public.beta_require_admin();
    exception when others then
      raise exception 'Admin or service role required' using errcode = '42501';
    end;
  end if;

  v_payload := public.list_owned_market_pricing_targets(false);
  v_targets := coalesce(v_payload->'targets', '[]'::jsonb);

  -- Zero inventory on live sold markets first (owned sync scope).
  update public.market_price_keys
  set inventory_count = 0,
      updated_at = now()
  where lower(market_country) in ('au', 'us', 'gb', 'ca')
    and coalesce(inventory_count, 0) <> 0;

  for v_row in select value from jsonb_array_elements(v_targets)
  loop
    v_fp := v_row->>'fingerprint';
    if coalesce(v_fp, '') = '' then
      continue;
    end if;

    select id into v_existing
    from public.market_price_keys
    where fingerprint = v_fp
    limit 1;

    if v_existing is null then
      v_key_id := public.get_or_create_market_price_key(
        'pokemon',
        coalesce(v_row->>'card_name', ''),
        lower(replace(coalesce(v_row->>'card_name', ''), ' ', '_')),
        coalesce(v_row->>'set_name', ''),
        coalesce(v_row->>'set_id', ''),
        coalesce(v_row->>'collector_number', ''),
        coalesce(v_row->>'language', 'en'),
        coalesce(v_row->>'variant', 'raw'),
        coalesce(v_row->>'condition', 'raw'),
        coalesce(v_row->>'market_country', 'au'),
        coalesce(v_row->>'currency', 'aud'),
        v_fp,
        now(),
        null,
        null,
        '[]'::jsonb,
        nullif(v_row->>'physical_printing_id', ''),
        null
      );
      v_created := v_created + 1;
    else
      v_key_id := v_existing;
    end if;

    update public.market_price_keys
    set inventory_count = greatest(coalesce((v_row->>'total_owned_quantity')::integer, 0), 0),
        last_seen_at = now(),
        physical_printing_id = coalesce(nullif(v_row->>'physical_printing_id', ''), physical_printing_id),
        updated_at = now()
    where id = v_key_id;

    v_ensured := v_ensured + 1;
    v_stamped := v_stamped + 1;
  end loop;

  return jsonb_build_object(
    'ensuredKeys', v_ensured,
    'createdKeys', v_created,
    'stampedKeys', v_stamped,
    'targetCount', jsonb_array_length(v_targets),
    'syncedAtUtc', now()
  );
end;
$$;

revoke all on function public.sync_owned_market_price_keys() from public, anon;
grant execute on function public.sync_owned_market_price_keys() to service_role;

-- ---------------------------------------------------------------------------
-- Ops health summary (no PII).
-- ---------------------------------------------------------------------------
create or replace function public.owned_price_health_report()
returns jsonb
language plpgsql
security definer
stable
set search_path = public, pg_temp
as $$
declare
  v_targets jsonb;
  v_list jsonb;
  v_users integer := 0;
  v_copies bigint := 0;
  v_unique_printings integer := 0;
begin
  if auth.role() <> 'service_role' then
    begin
      perform public.beta_require_admin();
    exception when others then
      raise exception 'Admin or service role required' using errcode = '42501';
    end;
  end if;

  select count(distinct user_id)::integer,
         coalesce(sum(greatest(quantity, 0)), 0)::bigint
  into v_users, v_copies
  from public.customer_collection_items
  where deleted_at is null
    and greatest(coalesce(quantity, 0), 0) > 0;

  select count(*)::integer into v_unique_printings
  from (
    select distinct
      coalesce(nullif(trim(physical_printing_id), ''),
        public.cardscanr_market_price_fingerprint(
          coalesce(card_name, ''),
          coalesce(set_name, ''),
          coalesce(set_id, ''),
          coalesce(collector_number, ''),
          coalesce(nullif(trim(language), ''), 'en'),
          coalesce(nullif(trim(variant), ''), 'raw'),
          coalesce(nullif(trim(condition), ''), 'raw'),
          'au',
          'aud'
        ))
    from public.customer_collection_items
    where deleted_at is null
      and greatest(coalesce(quantity, 0), 0) > 0
      and coalesce(trim(card_name), '') <> ''
      and coalesce(trim(collector_number), '') <> ''
  ) d;

  v_targets := public.list_owned_market_pricing_targets(false);
  v_list := coalesce(v_targets->'targets', '[]'::jsonb);

  return jsonb_build_object(
    'capturedAtUtc', now(),
    'usersWithOwnedCards', v_users,
    'uniqueOwnedPrintings', v_unique_printings,
    'totalCopies', v_copies,
    'marketSpecificKeys', jsonb_array_length(v_list),
    'freshLt24h', (
      select count(*)::integer from jsonb_array_elements(v_list) t
      where t.value->>'price_state' = 'fresh_lt_24h'
    ),
    'stale', (
      select count(*)::integer from jsonb_array_elements(v_list) t
      where t.value->>'price_state' in ('stale', 'stale_gt_24h')
    ),
    'unpriced', (
      select count(*)::integer from jsonb_array_elements(v_list) t
      where t.value->>'price_state' = 'unpriced'
    ),
    'dueToday', (
      select count(*)::integer from jsonb_array_elements(v_list) t
      where (t.value->>'due_for_owned_daily')::boolean is true
    ),
    'byPriority', (
      select coalesce(jsonb_object_agg(band, cnt), '{}'::jsonb)
      from (
        select coalesce(t.value->>'owned_priority_band', 'UNKNOWN') as band,
               count(*)::integer as cnt
        from jsonb_array_elements(v_list) t
        group by 1
      ) s
    ),
    'byMarket', (
      select coalesce(jsonb_agg(row_to_json(m)), '[]'::jsonb)
      from (
        select t.value->>'market_country' as market,
               t.value->>'currency' as currency,
               count(*)::integer as keys,
               count(*) filter (where (t.value->>'due_for_owned_daily')::boolean)::integer as due
        from jsonb_array_elements(v_list) t
        group by 1, 2
        order by 1, 2
      ) m
    )
  );
end;
$$;

revoke all on function public.owned_price_health_report() from public, anon;
grant execute on function public.owned_price_health_report() to service_role;

comment on function public.list_owned_market_pricing_targets(boolean) is
  'Fleet list of distinct owned printings x home pricing market. No user PII.';
comment on function public.sync_owned_market_price_keys() is
  'Ensure market_price_keys for owned printing x market and stamp inventory_count.';
comment on function public.owned_price_health_report() is
  'Ops health for daily owned pricing coverage. No user PII.';
