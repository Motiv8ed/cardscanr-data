-- Fan out owned printings to browser-ready verified-local markets.
-- Identity remains printing × market × currency. JP/EU are not included
-- (BLOCKED_NEEDS_PROVIDER). Owner home market is retained for diagnostics
-- and does not replace US/GB/CA fingerprints.

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
  if auth.role() <> 'service_role' then
    begin
      perform public.beta_require_admin();
    exception when others then
      raise exception 'Admin or service role required' using errcode = '42501';
    end;
  end if;

  with owned_printings as (
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
      r.market_country as home_market_country,
      r.currency as home_currency,
      r.resolution_source as home_resolution_source
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
  browser_markets as (
    select *
    from (
      values
        ('au', 'aud'),
        ('us', 'usd'),
        ('gb', 'gbp'),
        ('ca', 'cad')
    ) as m(market_country, currency)
  ),
  owned_rows as (
    select
      p.user_id,
      p.physical_printing_id,
      p.card_name,
      p.set_name,
      p.set_id,
      p.collector_number,
      p.language,
      p.variant,
      p.condition,
      p.quantity,
      m.market_country,
      m.currency,
      case
        when m.market_country = p.home_market_country then p.home_resolution_source
        else 'browser_ready_market_fanout'
      end as resolution_source
    from owned_printings p
    cross join browser_markets m
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
        when lower(coalesce(cache.display_price_source, '')) in (
          'reference', 'pending_verification'
        ) then 'reference_only'
        when lower(coalesce(cache.display_price_source, '')) = 'international_estimate'
          then 'structured_fallback'
        when lower(coalesce(cache.display_price_source, '')) in (
          'verified_local', 'verified_au', 'local_verified', 'market'
        ) then 'verified_local'
        when lower(coalesce(cache.provider, '')) in (
          'static_reference', 'tcgdex_reference', 'pokemon_tcg_api_reference',
          'tcgdex_tcgplayer', 'tcgdex_cardmarket', 'pokemon_tcg_api'
        ) then 'reference_only'
        when lower(coalesce(cache.provider, '')) in ('ebay_browser', 'ebay')
          then 'verified_local'
        when cache.current_market_price is not null then 'reference_only'
        else 'unknown'
      end as source_class,
      case
        when cache.current_market_price is null or cache.current_market_price <= 0 then 'unpriced'
        when cache.stale_after is not null and cache.stale_after <= now() then 'stale'
        when cache.last_updated_at is not null
          and cache.last_updated_at <= (now() - interval '24 hours') then 'stale_gt_24h'
        when cache.last_updated_at is not null then 'fresh_lt_24h'
        else 'unknown'
      end as price_state,
      case
        when cache.current_market_price is null or cache.current_market_price <= 0
          then 'P0_NEVER_PRICED'
        when lower(coalesce(cache.display_price_source, '')) in (
          'reference', 'pending_verification', 'international_estimate'
        ) then 'P0_NEEDS_VERIFIED_LOCAL'
        when lower(coalesce(cache.display_price_source, '')) not in (
          'verified_local', 'verified_au', 'local_verified', 'market'
        )
          and lower(coalesce(cache.provider, '')) not in ('ebay_browser', 'ebay')
          then 'P0_NEEDS_VERIFIED_LOCAL'
        when lower(coalesce(cache.refresh_status, '')) = 'failed'
          then 'P2_FAILED_RETRY'
        when cache.last_updated_at is null
          or cache.last_updated_at <= (now() - interval '24 hours')
          then 'P1_STALE_GT_24H'
        when cache.last_updated_at <= (now() - interval '22 hours')
          then 'P3_APPROACHING_DUE'
        else 'FRESH_SKIP'
      end as owned_priority_band,
      case
        when cache.current_market_price is null or cache.current_market_price <= 0
          then true
        when lower(coalesce(cache.display_price_source, '')) in (
          'reference', 'pending_verification', 'international_estimate'
        ) then true
        when lower(coalesce(cache.display_price_source, '')) not in (
          'verified_local', 'verified_au', 'local_verified', 'market'
        )
          and lower(coalesce(cache.provider, '')) not in ('ebay_browser', 'ebay')
          then true
        when lower(coalesce(cache.refresh_status, '')) = 'failed'
          and (
            cache.next_refresh_due_at is null
            or cache.next_refresh_due_at <= now()
          ) then true
        when cache.last_updated_at is null
          or cache.last_updated_at <= (now() - interval '24 hours') then true
        when cache.last_updated_at <= (now() - interval '22 hours') then true
        else false
      end as due_for_owned_daily
    from aggregated a
    left join public.market_price_keys k
      on lower(k.fingerprint) = lower(a.fingerprint)
    left join public.market_price_cache cache
      on cache.price_key_id = k.id
    where p_include_zero_owners or a.owner_count > 0
  )
  select coalesce(jsonb_agg(to_jsonb(j) order by
    case j.owned_priority_band
      when 'P0_NEVER_PRICED' then 0
      when 'P0_NEEDS_VERIFIED_LOCAL' then 0
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

comment on function public.list_owned_market_pricing_targets(boolean) is
  'Owned pricing targets fanned out to browser-ready markets AU/US/GB/CA; JP/EU excluded. Fingerprint join is case-insensitive; source-aware bands aligned with Python owned_daily_source_policy.';
