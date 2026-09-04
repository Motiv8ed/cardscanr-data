-- CS-012r: classify refresh job outcomes; stop treating already-fresh no-ops as failures.
-- Also add cancel_market_price_refresh_job for worker skipped_already_fresh path.

create or replace function public.cancel_market_price_refresh_job(
  p_job_id uuid,
  p_reason text default 'cancelled'
)
returns public.market_price_refresh_jobs
language plpgsql
security definer
set search_path = public, pg_temp
as 
declare
  v_job public.market_price_refresh_jobs;
begin
  if coalesce(auth.role(), '') <> 'service_role' then
    raise exception 'service_role required' using errcode = '42501';
  end if;

  update public.market_price_refresh_jobs
  set
    status = 'cancelled',
    completed_at = now(),
    error_message = left(coalesce(nullif(trim(p_reason), ''), 'cancelled'), 1000),
    worker_id = null,
    locked_at = null,
    updated_at = now()
  where id = p_job_id
    and status = 'running'
  returning * into v_job;

  if v_job.id is null then
    raise exception 'running refresh job not found for id %', p_job_id;
  end if;

  -- Do not mark cache failed for intentional no-ops / cancellations.
  return v_job;
end;
;

revoke all on function public.cancel_market_price_refresh_job(uuid, text) from public;
grant execute on function public.cancel_market_price_refresh_job(uuid, text) to service_role;

create or replace function public.admin_pricing_pipeline(
  p_heartbeat_stale_minutes integer default 45,
  p_default_market text default 'au',
  p_default_currency text default 'aud'
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
  v_hb_cutoff timestamptz := now() - make_interval(mins => greatest(5, coalesce(p_heartbeat_stale_minutes, 45)));
  v_target_cph numeric := 2084;
  v_scheduler_hb jsonb;
  v_worker_hb jsonb;
  v_scheduler_age_seconds bigint := null;
  v_worker_age_seconds bigint := null;
  v_jobs_completed_1h integer := 0;
  v_jobs_failed_1h integer := 0;
  v_jobs_completed_24h integer := 0;
  v_jobs_failed_24h integer := 0;
  v_jobs_failed_raw_24h integer := 0;
  v_jobs_skipped_fresh_24h integer := 0;
  v_jobs_timeout_24h integer := 0;
  v_jobs_cancelled_24h integer := 0;
  v_jobs_real_failed_24h integer := 0;
  v_jobs_failed_raw_1h integer := 0;
  v_jobs_skipped_fresh_1h integer := 0;
  v_jobs_timeout_1h integer := 0;
  v_jobs_real_failed_1h integer := 0;
  v_real_failure_rate_1h numeric := 0;
  v_real_failure_rate_24h numeric := 0;
  v_jobs_total_24h integer := 0;
  v_bulk_keys_1h integer := 0;
  v_owned_cards integer := 0;
  v_owned_rows integer := 0;
  v_owned_printings integer := 0;
  v_owned_ppid integer := 0;
  v_tracked_keys integer := 0;
  v_inventory_sum integer := 0;
  v_inventory_nonzero integer := 0;
  v_fresh integer := 0;
  v_stale integer := 0;
  v_missing integer := 0;
  v_unresolved integer := 0;
  v_quarantined integer := 0;
  v_verification_required integer := 0;
  v_failed_cache integer := 0;
  v_due_now integer := 0;
  v_queue_depth integer := 0;
  v_user_owned_priced integer := 0;
  v_user_owned_missing integer := 0;
  v_user_owned_unresolved integer := 0;
  v_user_owned_stale integer := 0;
  v_user_owned_total integer := 0;
  v_cache_priced integer := 0;
  v_hourly_runs jsonb := '[]'::jsonb;
  v_recent_jobs jsonb := '[]'::jsonb;
  v_errors_by_reason jsonb := '[]'::jsonb;
  v_last_price_change jsonb := null;
  v_oldest_user_owned jsonb := null;
  v_overall text := 'UNKNOWN';
  v_alerts jsonb := '[]'::jsonb;
  v_failure_rate_1h numeric := 0;
  v_failure_rate_24h numeric := 0;
  v_scheduler_keys_eligible integer := 0;
  v_scheduler_jobs_enqueued integer := 0;
  v_scheduler_candidates integer := 0;
  v_pipeline_stalled boolean := false;
  v_user_coverage_pct numeric := 0;
  v_cache_coverage_pct numeric := 0;
  v_heartbeat_only_healthy boolean := false;
  v_bulk_unresolved integer := 0;
  v_bulk_quarantined integer := 0;
begin
  perform public.beta_require_admin();

  select to_jsonb(h) into v_scheduler_hb
  from public.market_price_pipeline_heartbeats h
  where component = 'scheduler';

  select to_jsonb(h) into v_worker_hb
  from public.market_price_pipeline_heartbeats h
  where component = 'worker';

  if v_scheduler_hb is not null then
    v_scheduler_age_seconds := extract(epoch from (
      v_now - (v_scheduler_hb->>'last_heartbeat_at')::timestamptz
    ))::bigint;
    v_scheduler_keys_eligible := coalesce((v_scheduler_hb->'meta'->>'keysEligible')::integer, 0);
    v_scheduler_jobs_enqueued := coalesce((v_scheduler_hb->'meta'->>'jobsEnqueued')::integer, 0);
    v_scheduler_candidates := coalesce((v_scheduler_hb->'meta'->>'candidatesScanned')::integer, 0);
  end if;

  if v_worker_hb is not null then
    v_worker_age_seconds := extract(epoch from (
      v_now - (v_worker_hb->>'last_heartbeat_at')::timestamptz
    ))::bigint;
  end if;

  select
    count(*) filter (where status = 'completed')::integer,
    count(*) filter (where status = 'failed')::integer,
    count(*) filter (
      where status = 'failed'
        and coalesce(error_message, '') = 'skipped_already_fresh'
    )::integer,
    count(*) filter (
      where status = 'failed'
        and coalesce(error_message, '') ilike '%Timed out waiting for eBay result container%'
    )::integer,
    count(*) filter (
      where status = 'failed'
        and coalesce(error_message, '') <> 'skipped_already_fresh'
    )::integer
  into
    v_jobs_completed_1h,
    v_jobs_failed_raw_1h,
    v_jobs_skipped_fresh_1h,
    v_jobs_timeout_1h,
    v_jobs_real_failed_1h
  from public.market_price_refresh_jobs
  where coalesce(completed_at, started_at, requested_at) >= v_hour_ago;

  -- Health alerts use real failures only (exclude already-fresh no-ops).
  v_jobs_failed_1h := v_jobs_real_failed_1h;

  select
    count(*)::integer,
    count(*) filter (where status = 'completed')::integer,
    count(*) filter (where status = 'failed')::integer,
    count(*) filter (
      where status = 'failed'
        and coalesce(error_message, '') = 'skipped_already_fresh'
    )::integer,
    count(*) filter (
      where status = 'failed'
        and coalesce(error_message, '') ilike '%Timed out waiting for eBay result container%'
    )::integer,
    count(*) filter (
      where status = 'failed'
        and coalesce(error_message, '') <> 'skipped_already_fresh'
    )::integer,
    count(*) filter (where status = 'cancelled')::integer
  into
    v_jobs_total_24h,
    v_jobs_completed_24h,
    v_jobs_failed_raw_24h,
    v_jobs_skipped_fresh_24h,
    v_jobs_timeout_24h,
    v_jobs_real_failed_24h,
    v_jobs_cancelled_24h
  from public.market_price_refresh_jobs
  where requested_at >= v_day_ago;

  v_jobs_failed_24h := v_jobs_real_failed_24h;

  if (v_jobs_completed_1h + v_jobs_failed_raw_1h) > 0 then
    v_failure_rate_1h := round(v_jobs_failed_raw_1h::numeric / (v_jobs_completed_1h + v_jobs_failed_raw_1h)::numeric, 4);
  end if;
  if (v_jobs_completed_1h + v_jobs_real_failed_1h) > 0 then
    v_real_failure_rate_1h := round(
      v_jobs_real_failed_1h::numeric / (v_jobs_completed_1h + v_jobs_real_failed_1h)::numeric,
      4
    );
  end if;
  if v_jobs_total_24h > 0 then
    v_failure_rate_24h := round(v_jobs_failed_raw_24h::numeric / v_jobs_total_24h::numeric, 4);
    v_real_failure_rate_24h := round(v_jobs_real_failed_24h::numeric / v_jobs_total_24h::numeric, 4);
  end if;

  select coalesce(max(bulk_keys_per_hour), 0) into v_bulk_keys_1h
  from public.market_price_provider_sync_runs
  where provider = 'bulk_reference'
    and status = 'success'
    and started_at >= v_hour_ago;

  select
    coalesce(sum(greatest(coalesce(i.quantity, 1), 1)), 0)::integer,
    count(*)::integer,
    count(distinct coalesce(nullif(trim(i.physical_printing_id), ''), i.id::text))::integer,
    count(*) filter (where nullif(trim(i.physical_printing_id), '') is not null)::integer
  into v_owned_cards, v_owned_rows, v_owned_printings, v_owned_ppid
  from public.customer_collection_items i
  where i.deleted_at is null;

  select count(*)::integer,
    coalesce(sum(inventory_count), 0)::integer,
    count(*) filter (where inventory_count > 0)::integer
  into v_tracked_keys, v_inventory_sum, v_inventory_nonzero
  from public.market_price_keys;

  select
    count(*) filter (
      where c.current_market_price is not null
        and coalesce(c.next_refresh_due_at, c.stale_after) > v_now
    )::integer,
    count(*) filter (
      where c.current_market_price is not null
        and coalesce(c.next_refresh_due_at, c.stale_after) <= v_now
    )::integer,
    count(*) filter (where c.current_market_price is null)::integer,
    count(*) filter (where c.refresh_status = 'failed' and c.current_market_price is null)::integer,
    count(*) filter (where coalesce(c.verification_required, false))::integer,
    count(*) filter (
      where c.refresh_status = 'failed'
        or nullif(trim(c.last_error_message), '') is not null
    )::integer,
    count(*) filter (where coalesce(c.next_refresh_due_at, c.stale_after) <= v_now)::integer,
    count(*) filter (where c.current_market_price is not null)::integer
  into
    v_fresh,
    v_stale,
    v_missing,
    v_unresolved,
    v_verification_required,
    v_failed_cache,
    v_due_now,
    v_cache_priced
  from public.market_price_cache c;

  select coalesce(keys_unresolved, 0), coalesce(keys_quarantined, 0)
  into v_bulk_unresolved, v_bulk_quarantined
  from public.market_price_provider_sync_runs
  where provider = 'bulk_reference'
  order by started_at desc
  limit 1;

  v_unresolved := v_unresolved + v_bulk_unresolved;
  v_quarantined := v_quarantined + v_bulk_quarantined;

  select count(*) filter (where status in ('queued', 'running'))::integer
  into v_queue_depth
  from public.market_price_refresh_jobs;

  with owned as (
    select
      public.cardscanr_market_price_fingerprint(
        coalesce(i.card_name, ''),
        coalesce(i.set_name, ''),
        coalesce(i.set_id, ''),
        coalesce(i.collector_number, ''),
        coalesce(nullif(trim(i.language), ''), 'en'),
        coalesce(nullif(trim(i.variant), ''), 'raw'),
        coalesce(nullif(trim(i.condition), ''), 'raw'),
        lower(trim(p_default_market)),
        lower(trim(p_default_currency))
      ) as fingerprint,
      greatest(coalesce(i.quantity, 1), 1)::integer as qty,
      nullif(trim(i.physical_printing_id), '') as ppid
    from public.customer_collection_items i
    where i.deleted_at is null
      and coalesce(trim(i.card_name), '') <> ''
      and coalesce(trim(i.collector_number), '') <> ''
      and (
        coalesce(trim(i.set_id), '') <> ''
        or coalesce(trim(i.set_name), '') <> ''
      )
  ),
  owned_status as (
    select
      o.qty,
      o.ppid,
      k.id as key_id,
      c.current_market_price,
      c.last_updated_at,
      coalesce(c.next_refresh_due_at, c.stale_after) as due_at,
      c.refresh_status,
      c.verification_required
    from owned o
    left join public.market_price_keys k on k.fingerprint = o.fingerprint
    left join public.market_price_cache c on c.price_key_id = k.id
  )
  select
    coalesce(sum(qty), 0)::integer,
    coalesce(sum(qty) filter (
      where key_id is not null
        and current_market_price is not null
        and coalesce(due_at, v_now + interval '1 day') > v_now
    ), 0)::integer,
    coalesce(sum(qty) filter (
      where key_id is null
        or current_market_price is null
    ), 0)::integer,
    coalesce(sum(qty) filter (where key_id is null), 0)::integer,
    coalesce(sum(qty) filter (
      where key_id is not null
        and current_market_price is not null
        and due_at <= v_now
    ), 0)::integer
  into
    v_user_owned_total,
    v_user_owned_priced,
    v_user_owned_missing,
    v_user_owned_unresolved,
    v_user_owned_stale
  from owned_status;

  if v_user_owned_total > 0 then
    v_user_coverage_pct := round(v_user_owned_priced::numeric / v_user_owned_total::numeric, 4);
  end if;
  if v_tracked_keys > 0 then
    v_cache_coverage_pct := round(v_cache_priced::numeric / v_tracked_keys::numeric, 4);
  end if;

  select coalesce(jsonb_agg(row_to_json(h)::jsonb order by h.hour_start desc), '[]'::jsonb)
  into v_hourly_runs
  from (
    select
      date_trunc('hour', coalesce(j.completed_at, j.started_at, j.requested_at)) as hour_start,
      count(*)::integer as jobs,
      count(*) filter (where j.status = 'completed')::integer as completed,
      count(*) filter (where j.status = 'failed')::integer as failed,
      count(distinct j.price_key_id)::integer as unique_keys
    from public.market_price_refresh_jobs j
    where coalesce(j.completed_at, j.started_at, j.requested_at) >= v_day_ago
    group by 1
    order by 1 desc
    limit 24
  ) h;

  select coalesce(jsonb_agg(row_to_json(j)::jsonb), '[]'::jsonb)
  into v_recent_jobs
  from (
    select
      j.id,
      j.price_key_id,
      j.status,
      j.reason,
      left(j.error_message, 240) as error_message,
      j.requested_at,
      j.started_at,
      j.completed_at,
      j.attempt_count,
      k.card_name,
      k.set_code,
      k.collector_number,
      k.fingerprint
    from public.market_price_refresh_jobs j
    left join public.market_price_keys k on k.id = j.price_key_id
    order by j.requested_at desc
    limit 50
  ) j;

  select coalesce(jsonb_agg(jsonb_build_object('reason', reason, 'count', cnt, 'class', class) order by cnt desc), '[]'::jsonb)
  into v_errors_by_reason
  from (
    select
      coalesce(nullif(trim(left(j.error_message, 240)), ''), '(empty)') as reason,
      count(*)::integer as cnt,
      case
        when coalesce(j.error_message, '') = 'skipped_already_fresh' then 'already_fresh_noop'
        when coalesce(j.error_message, '') ilike '%Timed out waiting for eBay result container%' then 'provider_timeout'
        else 'real_failure'
      end as class
    from public.market_price_refresh_jobs j
    where j.status in ('failed', 'cancelled')
      and j.completed_at >= v_day_ago
    group by 1, 3
    order by cnt desc
    limit 25
  ) e;

  select to_jsonb(x) into v_last_price_change
  from (
    select
      k.card_name,
      k.set_code,
      k.collector_number,
      prev.recommended_price as previous_price,
      s.recommended_price as new_price,
      s.created_at as changed_at
    from public.market_price_snapshots s
    join public.market_price_keys k on k.id = s.price_key_id
    join lateral (
      select s2.recommended_price
      from public.market_price_snapshots s2
      where s2.price_key_id = s.price_key_id
        and s2.created_at < s.created_at
        and s2.recommended_price is not null
      order by s2.created_at desc
      limit 1
    ) prev on true
    where s.recommended_price is not null
      and prev.recommended_price is not null
      and s.recommended_price <> prev.recommended_price
    order by s.created_at desc
    limit 1
  ) x;

  select to_jsonb(x) into v_oldest_user_owned
  from (
    with owned as (
      select
        public.cardscanr_market_price_fingerprint(
          coalesce(i.card_name, ''),
          coalesce(i.set_name, ''),
          coalesce(i.set_id, ''),
          coalesce(i.collector_number, ''),
          coalesce(nullif(trim(i.language), ''), 'en'),
          coalesce(nullif(trim(i.variant), ''), 'raw'),
          coalesce(nullif(trim(i.condition), ''), 'raw'),
          lower(trim(p_default_market)),
          lower(trim(p_default_currency))
        ) as fingerprint
      from public.customer_collection_items i
      where i.deleted_at is null
        and coalesce(trim(i.card_name), '') <> ''
        and coalesce(trim(i.collector_number), '') <> ''
        and (
          coalesce(trim(i.set_id), '') <> ''
          or coalesce(trim(i.set_name), '') <> ''
        )
    )
    select
      k.card_name,
      k.set_code,
      k.collector_number,
      c.current_market_price,
      c.last_updated_at,
      extract(epoch from (v_now - c.last_updated_at))::bigint as age_seconds
    from owned o
    join public.market_price_keys k on k.fingerprint = o.fingerprint
    join public.market_price_cache c on c.price_key_id = k.id
    where c.current_market_price is not null
    order by c.last_updated_at asc nulls last
    limit 1
  ) x;

  v_pipeline_stalled := (
    v_queue_depth > 0
    or v_due_now > 0
    or v_user_owned_missing > 0
  ) and v_jobs_completed_1h = 0 and v_bulk_keys_1h = 0;

  v_heartbeat_only_healthy := (
    v_scheduler_hb is not null
    and v_worker_hb is not null
    and (v_scheduler_hb->>'last_heartbeat_at')::timestamptz >= v_hb_cutoff
    and (v_worker_hb->>'last_heartbeat_at')::timestamptz >= v_hb_cutoff
  );

  if v_pipeline_stalled then
    v_alerts := v_alerts || jsonb_build_array(jsonb_build_object(
      'code', 'PIPELINE_STALLED',
      'severity', 'critical',
      'message', format(
        'Pricing pipeline stalled: queue=%s dueNow=%s userMissing=%s but 0 completions in last hour',
        v_queue_depth, v_due_now, v_user_owned_missing
      )
    ));
  end if;

  if v_real_failure_rate_24h >= 0.5 and v_jobs_total_24h >= 10 then
    v_alerts := v_alerts || jsonb_build_array(jsonb_build_object(
      'code', 'HIGH_FAILURE_RATE_24H',
      'severity', 'critical',
      'message', format(
        'Real job failure rate %s%% over 24h (%s/%s; rawFailed=%s skippedAlreadyFresh=%s ebayTimeouts=%s)',
        round(v_real_failure_rate_24h * 100, 1),
        v_jobs_real_failed_24h,
        v_jobs_total_24h,
        v_jobs_failed_raw_24h,
        v_jobs_skipped_fresh_24h,
        v_jobs_timeout_24h
      )
    ));
  elsif v_jobs_timeout_24h >= 50 and v_jobs_total_24h >= 10 then
    v_alerts := v_alerts || jsonb_build_array(jsonb_build_object(
      'code', 'HIGH_EBAY_TIMEOUT_24H',
      'severity', 'warning',
      'message', format(
        'eBay result-container timeouts %s over 24h (%s%% of jobs)',
        v_jobs_timeout_24h,
        round((v_jobs_timeout_24h::numeric / v_jobs_total_24h::numeric) * 100, 1)
      )
    ));
  end if;

  if v_scheduler_hb is null or (v_scheduler_hb->>'last_heartbeat_at')::timestamptz < v_hb_cutoff then
    v_alerts := v_alerts || jsonb_build_array(jsonb_build_object(
      'code', 'SCHEDULER_HEARTBEAT_STALE',
      'severity', 'warning',
      'message', 'Scheduler heartbeat missing or stale'
    ));
  end if;

  if v_worker_hb is null or (v_worker_hb->>'last_heartbeat_at')::timestamptz < v_hb_cutoff then
    v_alerts := v_alerts || jsonb_build_array(jsonb_build_object(
      'code', 'WORKER_HEARTBEAT_STALE',
      'severity', 'critical',
      'message', 'Worker heartbeat missing or stale'
    ));
  end if;

  if v_stale > 0 and v_scheduler_keys_eligible = 0 and v_scheduler_candidates = 0 then
    v_alerts := v_alerts || jsonb_build_array(jsonb_build_object(
      'code', 'STALE_NOT_SCHEDULED',
      'severity', 'warning',
      'message', format('%s stale cache rows but scheduler scanned 0 candidates (keysEligible=%s)', v_stale, v_scheduler_keys_eligible)
    ));
  end if;

  if v_inventory_nonzero = 0 and v_owned_cards > 0 then
    v_alerts := v_alerts || jsonb_build_array(jsonb_build_object(
      'code', 'INVENTORY_COUNT_UNMATERIALIZED',
      'severity', 'warning',
      'message', format('market_price_keys.inventory_count is 0 for all keys while %s owned cards exist', v_owned_cards)
    ));
  end if;

  if v_user_owned_unresolved > 0 then
    v_alerts := v_alerts || jsonb_build_array(jsonb_build_object(
      'code', 'USER_OWNED_UNRESOLVED_KEYS',
      'severity', 'warning',
      'message', format('%s user-owned card quantities have no market_price_key fingerprint match', v_user_owned_unresolved)
    ));
  end if;

  if exists (select 1 from jsonb_array_elements(v_alerts) a where a->>'severity' = 'critical') then
    v_overall := 'CRITICAL';
  elsif v_pipeline_stalled or v_real_failure_rate_1h >= 0.5 then
    v_overall := 'CRITICAL';
  elsif exists (select 1 from jsonb_array_elements(v_alerts) a where a->>'severity' = 'warning') then
    v_overall := 'DEGRADED';
  elsif v_jobs_completed_1h = 0 and v_user_owned_missing > 0 then
    v_overall := 'DEGRADED';
  elsif v_heartbeat_only_healthy and v_user_coverage_pct >= 0.8 and v_real_failure_rate_24h < 0.25 then
    v_overall := 'HEALTHY';
  elsif v_heartbeat_only_healthy then
    v_overall := 'DEGRADED';
  else
    v_overall := 'UNKNOWN';
  end if;

  return jsonb_build_object(
    'checkedAtUtc', v_now,
    'overall', v_overall,
    'alerts', v_alerts,
    'heartbeatOnlyWouldLookHealthy', v_heartbeat_only_healthy and v_overall <> 'HEALTHY',
    'scheduler', jsonb_build_object(
      'heartbeat', coalesce(v_scheduler_hb, 'null'::jsonb),
      'ageSeconds', v_scheduler_age_seconds,
      'keysEligible', v_scheduler_keys_eligible,
      'jobsEnqueued', v_scheduler_jobs_enqueued,
      'candidatesScanned', v_scheduler_candidates
    ),
    'worker', jsonb_build_object(
      'heartbeat', coalesce(v_worker_hb, 'null'::jsonb),
      'ageSeconds', v_worker_age_seconds
    ),
    'throughput', jsonb_build_object(
      'targetCardsPerHour', v_target_cph,
      'completedJobsLastHour', v_jobs_completed_1h,
      'failedJobsLastHour', v_jobs_failed_1h,
      'failureRateLastHour', v_real_failure_rate_1h,
      'rawFailedJobsLastHour', v_jobs_failed_raw_1h,
      'rawFailureRateLastHour', v_failure_rate_1h,
      'skippedAlreadyFreshLastHour', v_jobs_skipped_fresh_1h,
      'ebayTimeoutJobsLastHour', v_jobs_timeout_1h,
      'completedJobsLast24h', v_jobs_completed_24h,
      'failedJobsLast24h', v_jobs_failed_24h,
      'failureRateLast24h', v_real_failure_rate_24h,
      'rawFailedJobsLast24h', v_jobs_failed_raw_24h,
      'rawFailureRateLast24h', v_failure_rate_24h,
      'skippedAlreadyFreshLast24h', v_jobs_skipped_fresh_24h,
      'ebayTimeoutJobsLast24h', v_jobs_timeout_24h,
      'cancelledJobsLast24h', v_jobs_cancelled_24h,
      'bulkReferenceKeysPerHour', v_bulk_keys_1h
    ),
    'inventory', jsonb_build_object(
      'ownedCardsQuantity', v_owned_cards,
      'ownedCollectionRows', v_owned_rows,
      'distinctOwnedPrintings', v_owned_printings,
      'ownedWithPhysicalPrintingId', v_owned_ppid,
      'trackedMarketKeys', v_tracked_keys,
      'inventoryCountSum', v_inventory_sum,
      'keysWithInventoryCount', v_inventory_nonzero
    ),
    'cacheStates', jsonb_build_object(
      'fresh', v_fresh,
      'stale', v_stale,
      'missing', v_missing,
      'failed', v_failed_cache,
      'unresolved', v_unresolved,
      'quarantined', v_quarantined,
      'verificationRequired', v_verification_required,
      'dueNow', v_due_now,
      'queueDepth', v_queue_depth
    ),
    'coverage', jsonb_build_object(
      'userOwnedTotalQuantity', v_user_owned_total,
      'userOwnedPricedQuantity', v_user_owned_priced,
      'userOwnedMissingQuantity', v_user_owned_missing,
      'userOwnedUnresolvedQuantity', v_user_owned_unresolved,
      'userOwnedStaleQuantity', v_user_owned_stale,
      'userOwnedCoveragePct', v_user_coverage_pct,
      'cacheWideCoveragePct', v_cache_coverage_pct,
      'coverageUsesQuantityNotRows', true
    ),
    'hourlyRunsLast24h', v_hourly_runs,
    'recentJobs', v_recent_jobs,
    'errorsByReason24h', v_errors_by_reason,
    'lastSuccessfulPriceChange', v_last_price_change,
    'oldestUserOwnedPrice', v_oldest_user_owned
  );
end;
$$;

revoke all on function public.admin_pricing_pipeline(integer, text, text) from public, anon;
grant execute on function public.admin_pricing_pipeline(integer, text, text) to authenticated, service_role;

comment on function public.admin_pricing_pipeline(integer, text, text) is
  'Owner-facing pricing pipeline dashboard. Separates heartbeat liveness from end-to-end pricing health.';
