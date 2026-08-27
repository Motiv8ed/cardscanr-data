-- Official ECB FX rate cache for international pricing + Admin health.

create table if not exists public.market_fx_rate_cache (
  id integer primary key default 1 check (id = 1),
  source text not null default 'ECB',
  source_label text not null default 'European Central Bank',
  source_url text,
  provider_rate_date date,
  fetched_at timestamptz,
  last_attempt_at timestamptz,
  last_success_at timestamptz,
  last_error text,
  consecutive_failures integer not null default 0,
  health text not null default 'STALE',
  allows_conversion boolean not null default false,
  block_reason text,
  currencies text[] not null default '{}',
  eur_rates jsonb not null default '{}'::jsonb,
  pair_rates jsonb not null default '{}'::jsonb,
  fetch_max_age_hours integer not null default 36,
  outage_grace_hours integer not null default 96,
  updated_at timestamptz not null default now()
);

comment on table public.market_fx_rate_cache is
  'Singleton shared ECB FX snapshot for international estimates and Admin health.';

alter table public.market_fx_rate_cache enable row level security;

drop policy if exists market_fx_rate_cache_admin_select on public.market_fx_rate_cache;
create policy market_fx_rate_cache_admin_select
  on public.market_fx_rate_cache
  for select
  to authenticated
  using (public.is_beta_admin());

drop policy if exists market_fx_rate_cache_service_role_all on public.market_fx_rate_cache;
create policy market_fx_rate_cache_service_role_all
  on public.market_fx_rate_cache
  for all
  to service_role
  using (true)
  with check (true);

grant select on public.market_fx_rate_cache to authenticated;
grant all on public.market_fx_rate_cache to service_role;

alter table public.market_price_cache
  add column if not exists fx_provider_rate_date date,
  add column if not exists fx_fetched_at timestamptz;

create or replace function public.admin_fx_health_snapshot()
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_now timestamptz := now();
  v_row public.market_fx_rate_cache%rowtype;
  v_blocked integer := 0;
  v_age_hours numeric := null;
begin
  select * into v_row from public.market_fx_rate_cache where id = 1;
  if not found then
    select count(*)::integer into v_blocked
    from public.market_price_refresh_jobs
    where status = 'failed'
      and coalesce(completed_at, started_at, requested_at) >= v_now - interval '24 hours'
      and coalesce(error_message, '') ilike '%FX_RATE_STALE_NO_SAFE_CONVERSION%';

    return jsonb_build_object(
      'source', 'ECB',
      'sourceLabel', 'European Central Bank',
      'providerRateDate', null,
      'lastChecked', null,
      'lastSuccessfulRefresh', null,
      'ageHours', null,
      'fetchMaxAgeHours', 36,
      'outageGraceHours', 96,
      'health', 'STALE',
      'stale', true,
      'allowsConversion', false,
      'blockReason', 'FX_RATE_STALE_NO_SAFE_CONVERSION',
      'currencies', jsonb_build_array('AUD', 'USD', 'EUR', 'GBP', 'NZD', 'JPY', 'CAD'),
      'refreshFailures', 0,
      'conversionsBlocked', coalesce(v_blocked, 0),
      'note', 'No ECB FX snapshot has been published yet. International conversions remain blocked.'
    );
  end if;

  select count(*)::integer into v_blocked
  from public.market_price_refresh_jobs
  where status = 'failed'
    and coalesce(completed_at, started_at, requested_at) >= v_now - interval '24 hours'
    and coalesce(error_message, '') ilike '%FX_RATE_STALE_NO_SAFE_CONVERSION%';

  if v_row.fetched_at is not null then
    v_age_hours := round(extract(epoch from (v_now - v_row.fetched_at)) / 3600.0, 2);
  end if;

  return jsonb_build_object(
    'source', coalesce(v_row.source, 'ECB'),
    'sourceLabel', coalesce(v_row.source_label, 'European Central Bank'),
    'providerRateDate', v_row.provider_rate_date,
    'lastChecked', v_row.last_attempt_at,
    'lastSuccessfulRefresh', coalesce(v_row.last_success_at, v_row.fetched_at),
    'ageHours', v_age_hours,
    'fetchMaxAgeHours', v_row.fetch_max_age_hours,
    'outageGraceHours', v_row.outage_grace_hours,
    'health', coalesce(v_row.health, 'STALE'),
    'stale', not coalesce(v_row.allows_conversion, false),
    'allowsConversion', coalesce(v_row.allows_conversion, false),
    'blockReason', v_row.block_reason,
    'currencies', to_jsonb(coalesce(v_row.currencies, '{}'::text[])),
    'refreshFailures', coalesce(v_row.consecutive_failures, 0),
    'conversionsBlocked', coalesce(v_blocked, 0),
    'lastError', v_row.last_error,
    'note', case
      when coalesce(v_row.allows_conversion, false) then
        'Official ECB reference rates. Weekends/holidays reuse the latest provider publication date while CardScanR fetch/check remains fresh.'
      else
        'ECB FX cache is not healthy enough for new international estimate conversions.'
    end
  );
end;
$$;

revoke all on function public.admin_fx_health_snapshot() from public;
grant execute on function public.admin_fx_health_snapshot() to authenticated, service_role;

create or replace function public.admin_pricing_operations(
  p_overdue_limit integer default 25,
  p_no_price_limit integer default 25
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_now timestamptz := now();
  v_day_ago timestamptz := now() - interval '24 hours';
  v_hour_ago timestamptz := now() - interval '1 hour';
  v_health jsonb;
  v_longest_overdue jsonb;
  v_no_price jsonb;
  v_recent_activity jsonb;
  v_rejection_reasons jsonb;
  v_queue jsonb;
  v_identity_count integer := 0;
  v_retry_delayed_count integer := 0;
  v_jobs_1h integer := 0;
  v_unique_1h integer := 0;
  v_ebay_verify_1h integer := 0;
  v_intl_jobs_24h integer := 0;
  v_intl_success_24h integer := 0;
  v_intl_failed_24h integer := 0;
  v_bulk_keys_1h numeric := 0;
  v_due_now integer := 0;
  v_overdue integer := 0;
  v_missing integer := 0;
  v_failed_backoff integer := 0;
  v_active_keys integer := 0;
  v_fresh_24h integer := 0;
  v_oldest_overdue_seconds bigint := 0;
  v_target_cph numeric := 2084;
  v_combined_cph numeric := 0;
  v_eta_minutes numeric := null;
  v_clearance_hours numeric := null;
  v_pct_of_target numeric := 0;
  v_sla_fresh_ratio numeric := 0;
  v_reference_display integer := 0;
  v_verified_display integer := 0;
  v_international_display integer := 0;
  v_unavailable_display integer := 0;
  v_pending_verification integer := 0;
  v_bulk_matched integer := 0;
  v_bulk_scanned integer := 0;
  v_bulk_updated integer := 0;
  v_bulk_unresolved integer := 0;
  v_bulk_quarantined integer := 0;
  v_bulk_errors integer := 0;
  v_last_bulk_sync timestamptz := null;
  v_bulk_duration_ms integer := 0;
  v_bulk_status text := 'unknown';
  v_bulk_sync jsonb := '{}'::jsonb;
  v_bulk_coverage jsonb := '{}'::jsonb;
  v_readiness jsonb := '{}'::jsonb;
  v_intl_fallback jsonb := '{}'::jsonb;
  v_fx_health jsonb := '{}'::jsonb;
  v_market_coverage jsonb := '[]'::jsonb;
  v_coverage_pct numeric := 0;
  v_usable_pct numeric := 0;
begin
  perform public.beta_require_admin();
  v_health := public.admin_pricing_health(90, 45);

  select coalesce(jsonb_agg(row_to_json(x)::jsonb), '[]'::jsonb) into v_longest_overdue from (
    select k.id as price_key_id, k.card_name, k.set_name, k.set_code, k.collector_number, k.language, k.variant,
      c.current_market_price, c.last_updated_at, coalesce(c.next_refresh_due_at, c.stale_after) as due_at,
      extract(epoch from (v_now - coalesce(c.next_refresh_due_at, c.stale_after)))::bigint as overdue_seconds,
      c.refresh_status, c.last_error_message
    from public.market_price_cache c join public.market_price_keys k on k.id = c.price_key_id
    where c.current_market_price is null or coalesce(c.next_refresh_due_at, c.stale_after) <= v_now
    order by coalesce(c.next_refresh_due_at, c.stale_after) asc nulls first
    limit greatest(5, least(coalesce(p_overdue_limit, 25), 50))
  ) x;

  select coalesce(jsonb_agg(row_to_json(x)::jsonb), '[]'::jsonb) into v_no_price from (
    select k.card_name, k.set_name, k.collector_number, k.language, c.last_updated_at as last_checked_at, c.refresh_status as status
    from public.market_price_cache c join public.market_price_keys k on k.id = c.price_key_id
    where c.current_market_price is null order by c.updated_at desc nulls last
    limit greatest(5, least(coalesce(p_no_price_limit, 25), 50))
  ) x;

  select coalesce(jsonb_agg(row_to_json(u)::jsonb), '[]'::jsonb) into v_recent_activity from (
    select k.card_name, k.set_code, k.collector_number, s.recommended_price as new_price, s.provider, s.confidence,
      s.included_count as evidence_count, s.created_at as updated_at, c.display_price_source, c.reference_provider
    from public.market_price_snapshots s join public.market_price_keys k on k.id = s.price_key_id
    left join public.market_price_cache c on c.price_key_id = s.price_key_id
    where s.created_at >= v_day_ago order by s.created_at desc limit 40
  ) u;

  select coalesce(jsonb_agg(jsonb_build_object('reason', reason, 'count', cnt) order by cnt desc), '[]'::jsonb) into v_rejection_reasons from (
    select key as reason, sum(value::integer) as cnt from public.market_price_snapshots s
    cross join lateral jsonb_each_text(coalesce(s.diagnostics_json->'rejectionReasonCounts', '{}'::jsonb)) counts(key, value)
    where s.created_at >= v_day_ago group by key order by cnt desc limit 20
  ) r;

  select jsonb_build_object(
    'queued', count(*) filter (where status = 'queued')::integer,
    'running', count(*) filter (where status = 'running')::integer,
    'failed24h', count(*) filter (where status = 'failed' and completed_at >= v_day_ago)::integer
  ) into v_queue from public.market_price_refresh_jobs;

  select count(*)::integer into v_identity_count from public.market_price_cache c
    where c.refresh_status = 'failed' and coalesce(c.last_error_message, '') ilike '%english_market_identity_unavailable%';
  select count(*)::integer into v_retry_delayed_count from public.market_price_cache c
    where c.refresh_status = 'failed' and c.next_refresh_due_at is not null and c.next_refresh_due_at > v_now;

  select count(*)::integer, count(distinct price_key_id)::integer into v_jobs_1h, v_unique_1h
    from public.market_price_refresh_jobs
    where coalesce(completed_at, started_at, requested_at) >= v_hour_ago and status in ('completed', 'failed');

  select count(*)::integer into v_ebay_verify_1h from public.market_price_refresh_jobs
    where coalesce(completed_at, started_at, requested_at) >= v_hour_ago and status = 'completed'
      and coalesce(reason, '') ilike 'bulk_verify:%';

  select count(*)::integer,
    count(*) filter (where status = 'completed')::integer,
    count(*) filter (where status = 'failed')::integer
  into v_intl_jobs_24h, v_intl_success_24h, v_intl_failed_24h
  from public.market_price_refresh_jobs
  where coalesce(completed_at, started_at, requested_at) >= v_day_ago
    and coalesce(reason, '') ilike 'international_fallback%';

  select coalesce(max(bulk_keys_per_hour), 0) into v_bulk_keys_1h from public.market_price_provider_sync_runs
    where provider = 'bulk_reference' and status = 'success' and started_at >= v_hour_ago;

  select count(*) filter (where coalesce(next_refresh_due_at, stale_after) <= v_now)::integer,
    count(*) filter (where coalesce(next_refresh_due_at, stale_after) <= v_now - interval '1 hour')::integer,
    count(*) filter (where current_market_price is null)::integer,
    count(*) filter (where refresh_status = 'failed' and next_refresh_due_at is not null and next_refresh_due_at > v_now)::integer,
    count(*)::integer,
    count(*) filter (where current_market_price is not null and last_updated_at >= v_day_ago)::integer,
    coalesce(max(extract(epoch from (v_now - coalesce(next_refresh_due_at, stale_after))))
      filter (where coalesce(next_refresh_due_at, stale_after) <= v_now), 0)::bigint,
    count(*) filter (where display_price_source = 'reference')::integer,
    count(*) filter (where display_price_source in ('verified_au', 'verified_local', 'local_verified'))::integer,
    count(*) filter (where display_price_source = 'international_estimate')::integer,
    count(*) filter (where current_market_price is null)::integer,
    count(*) filter (where verification_required = true)::integer
  into v_due_now, v_overdue, v_missing, v_failed_backoff, v_active_keys, v_fresh_24h, v_oldest_overdue_seconds,
       v_reference_display, v_verified_display, v_international_display, v_unavailable_display, v_pending_verification
  from public.market_price_cache;

  select keys_matched, keys_scanned, keys_updated, keys_unresolved, keys_quarantined, errors, finished_at, duration_ms, status,
    coalesce((diagnostics_json->'coverage'->>'usablePct')::numeric, 0)
  into v_bulk_matched, v_bulk_scanned, v_bulk_updated, v_bulk_unresolved, v_bulk_quarantined, v_bulk_errors,
       v_last_bulk_sync, v_bulk_duration_ms, v_bulk_status, v_usable_pct
  from public.market_price_provider_sync_runs
  where provider = 'bulk_reference' and status in ('success', 'failed')
  order by started_at desc limit 1;

  if v_bulk_scanned > 0 then
    v_coverage_pct := round((v_bulk_matched::numeric / v_bulk_scanned::numeric), 4);
  end if;

  v_combined_cph := greatest(v_unique_1h::numeric, v_bulk_keys_1h, v_ebay_verify_1h::numeric);
  v_pct_of_target := case when v_target_cph <= 0 then 0 else round((v_combined_cph / v_target_cph) * 100.0, 1) end;
  if v_combined_cph > 0 and v_due_now > 0 then v_eta_minutes := round((v_due_now::numeric / v_combined_cph) * 60.0, 1); end if;
  if v_combined_cph > 0 then v_clearance_hours := round((50000::numeric / v_combined_cph), 2); end if;
  if v_active_keys > 0 then v_sla_fresh_ratio := round((v_fresh_24h::numeric / v_active_keys::numeric), 4); end if;

  select coalesce(jsonb_agg(jsonb_build_object(
    'userMarket', k.market_country,
    'localVerified', count(*) filter (where c.display_price_source in ('verified_au', 'verified_local', 'local_verified')),
    'reference', count(*) filter (where c.display_price_source = 'reference'),
    'internationalEstimate', count(*) filter (where c.display_price_source = 'international_estimate'),
    'unavailable', count(*) filter (where c.current_market_price is null)
  )), '[]'::jsonb) into v_market_coverage
  from public.market_price_cache c
  join public.market_price_keys k on k.id = c.price_key_id
  group by k.market_country;

  v_intl_fallback := jsonb_build_object(
    'jobsLast24h', coalesce(v_intl_jobs_24h, 0),
    'successfulEstimates24h', coalesce(v_intl_success_24h, 0),
    'failures24h', coalesce(v_intl_failed_24h, 0),
    'currentInternationalEstimates', coalesce(v_international_display, 0),
    'queueQueued', coalesce((v_queue->>'queued')::integer, 0)
  );

  v_fx_health := public.admin_fx_health_snapshot();

  v_bulk_sync := jsonb_build_object(
    'keysPerHour', v_bulk_keys_1h,
    'keysMatched', coalesce(v_bulk_matched, 0),
    'keysScanned', coalesce(v_bulk_scanned, 0),
    'keysUpdated', coalesce(v_bulk_updated, 0),
    'keysUnresolved', coalesce(v_bulk_unresolved, 0),
    'keysQuarantined', coalesce(v_bulk_quarantined, 0),
    'errors', coalesce(v_bulk_errors, 0),
    'coveragePct', v_coverage_pct,
    'usablePct', v_usable_pct,
    'lastSyncAtUtc', v_last_bulk_sync,
    'durationMs', coalesce(v_bulk_duration_ms, 0),
    'status', coalesce(v_bulk_status, 'unknown'),
    'health', case
      when v_last_bulk_sync is null then 'stale'
      when v_last_bulk_sync < v_now - interval '3 hours' then 'stale'
      when coalesce(v_bulk_errors, 0) > 0 then 'warning'
      when v_bulk_status = 'failed' then 'failed'
      else 'healthy'
    end
  );

  v_bulk_coverage := jsonb_build_object(
    'productionKeys', coalesce(v_bulk_scanned, v_active_keys),
    'mapped', coalesce(v_bulk_matched, 0),
    'mappedPct', v_coverage_pct,
    'usable', coalesce(v_bulk_updated, 0),
    'usablePct', v_usable_pct,
    'quarantined', coalesce(v_bulk_quarantined, 0),
    'unresolved', coalesce(v_bulk_unresolved, 0),
    'unsupported', coalesce(v_bulk_unresolved, 0)
  );

  v_readiness := jsonb_build_object(
    'throughputReady', coalesce(v_bulk_keys_1h, 0) >= 2084,
    'coverageReady', coalesce(v_usable_pct, 0) >= 0.80,
    'automationReady', v_last_bulk_sync is not null and v_last_bulk_sync >= v_now - interval '3 hours',
    'verificationCapacityReady', coalesce((v_queue->>'queued')::integer, 0) < 50,
    'fiftyKReady', coalesce(v_bulk_keys_1h, 0) >= 2084 and coalesce(v_usable_pct, 0) >= 0.80
      and v_last_bulk_sync is not null and v_last_bulk_sync >= v_now - interval '3 hours'
  );

  return jsonb_build_object(
    'checkedAtUtc', v_now,
    'health', v_health,
    'longestOverdue', coalesce(v_longest_overdue, '[]'::jsonb),
    'noPriceCards', coalesce(v_no_price, '[]'::jsonb),
    'recentActivity', coalesce(v_recent_activity, '[]'::jsonb),
    'rejectionReasons24h', coalesce(v_rejection_reasons, '[]'::jsonb),
    'queue', coalesce(v_queue, '{}'::jsonb),
    'identityUnavailableCount', coalesce(v_identity_count, 0),
    'retryDelayedCount', coalesce(v_retry_delayed_count, 0),
    'jobsLastHour', coalesce(v_jobs_1h, 0),
    'uniqueKeysLastHour', coalesce(v_unique_1h, 0),
    'ebayVerificationJobsLastHour', coalesce(v_ebay_verify_1h, 0),
    'bulkKeysPerHour', coalesce(v_bulk_keys_1h, 0),
    'combinedKeysPerHour', coalesce(v_combined_cph, 0),
    'pctOf50kTarget', v_pct_of_target,
    'etaMinutesToClearDue', v_eta_minutes,
    'clearanceHoursAtCurrentRate', v_clearance_hours,
    'dueNow', coalesce(v_due_now, 0),
    'overdueOneHourPlus', coalesce(v_overdue, 0),
    'missingPrice', coalesce(v_missing, 0),
    'failedBackoff', coalesce(v_failed_backoff, 0),
    'activeKeys', coalesce(v_active_keys, 0),
    'fresh24h', coalesce(v_fresh_24h, 0),
    'slaFreshRatio24h', v_sla_fresh_ratio,
    'oldestOverdueSeconds', coalesce(v_oldest_overdue_seconds, 0),
    'pricingSources', jsonb_build_object(
      'localVerified', coalesce(v_verified_display, 0),
      'reference', coalesce(v_reference_display, 0),
      'internationalEstimate', coalesce(v_international_display, 0),
      'unavailable', coalesce(v_unavailable_display, 0),
      'pendingVerification', coalesce(v_pending_verification, 0)
    ),
    'internationalFallback', v_intl_fallback,
    'fxHealth', v_fx_health,
    'marketCoverage', coalesce(v_market_coverage, '[]'::jsonb),
    'bulkSync', v_bulk_sync,
    'bulkCoverage', v_bulk_coverage,
    'readiness50k', v_readiness
  );
end;
$$;

grant execute on function public.admin_pricing_operations(integer, integer) to authenticated, service_role;
