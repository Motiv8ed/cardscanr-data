-- CardScanR Google Play v56 security hardening (Phase 3).
--
-- Idempotent. Hardens customer_* helpers/RPCs, market price RPCs, RLS policy
-- evaluation, and missing FK indexes. Does NOT delete user data.
--
-- Prerequisites (when present on target):
--   * customer_* objects from customer_collection_sync_core
--   * request_market_price_refresh (JP signature) + get_market_price_bundle
--
-- DO NOT APPLY TO PRODUCTION without explicit owner approval.
--
-- Rollback notes (manual):
--   1. Restore function bodies / search_path / grants from:
--        - 20260728090000_customer_collection_sync_core.sql (customer_*)
--        - 20260704000000_jp_market_price_identity_payload.sql
--          (request_market_price_refresh)
--        - 20260606000000_market_price_refresh_state_cache_rows.sql
--          (get_market_price_bundle)
--        - 20260727000000_security_advisor_remediation.sql (prior grants)
--   2. Re-create customer_* RLS policies with `auth.uid()` (not
--      `(select auth.uid())`) from the customer core migration.
--   3. Drop indexes added here if undesired:
--        idx_customer_binder_memberships_binder_id
--        idx_customer_binder_memberships_collection_item_id
--   4. Re-grant EXECUTE on customer_begin_operation /
--      customer_ack_operation to authenticated if client direct calls
--      were intentionally restored (not recommended).

-- =============================================================================
-- A. Mutable search_path on customer trigger helpers
-- =============================================================================

do $$
begin
  if to_regprocedure('public.customer_force_owner_user_id()') is not null then
    execute $ddl$
      create or replace function public.customer_force_owner_user_id()
      returns trigger
      language plpgsql
      set search_path = public, pg_temp
      as $fn$
      begin
        if auth.uid() is null then
          raise exception 'Authentication required' using errcode = '42501';
        end if;
        -- Never trust client-supplied ownership claims.
        new.user_id := auth.uid();
        if tg_op = 'UPDATE' and old.user_id is distinct from auth.uid() then
          raise exception 'Cannot modify another user''s row' using errcode = '42501';
        end if;
        return new;
      end;
      $fn$
    $ddl$;
    comment on function public.customer_force_owner_user_id() is
      'Forces user_id to auth.uid() so clients cannot reassign ownership via payload. search_path pinned.';
  end if;

  if to_regprocedure('public.customer_set_updated_at()') is not null then
    execute $ddl$
      create or replace function public.customer_set_updated_at()
      returns trigger
      language plpgsql
      set search_path = public, pg_temp
      as $fn$
      begin
        new.updated_at := now();
        new.server_updated_at := now();
        if new.revision is null or new.revision <= coalesce(old.revision, 0) then
          new.revision := coalesce(old.revision, 0) + 1;
        end if;
        return new;
      end;
      $fn$
    $ddl$;
    comment on function public.customer_set_updated_at() is
      'Maintains updated_at, monotonic revision, and server_updated_at. search_path pinned.';
  end if;
end;
$$;

-- =============================================================================
-- B. Internal helpers: keep DEFINER, revoke client EXECUTE
-- =============================================================================

do $$
declare
  v_begin regprocedure := to_regprocedure(
    'public.customer_begin_operation(uuid, text, text, uuid)'
  );
  v_ack regprocedure := to_regprocedure(
    'public.customer_ack_operation(uuid, text, uuid, text, text, bigint, uuid)'
  );
begin
  if v_begin is not null then
    execute format(
      'alter function %s set search_path = public, pg_temp',
      v_begin
    );
    execute format(
      'revoke all on function %s from public, anon, authenticated',
      v_begin
    );
    execute format('grant execute on function %s to service_role', v_begin);
    -- Owner/postgres retains EXECUTE; SECURITY DEFINER callers owned by the
    -- same role continue to work without granting EXECUTE to authenticated.
  end if;

  if v_ack is not null then
    execute format(
      'alter function %s set search_path = public, pg_temp',
      v_ack
    );
    execute format(
      'revoke all on function %s from public, anon, authenticated',
      v_ack
    );
    execute format('grant execute on function %s to service_role', v_ack);
  end if;
end;
$$;

-- =============================================================================
-- C. Externally callable customer mutation RPCs
-- =============================================================================

do $$
declare
  v_sig text;
  v_proc regprocedure;
  v_public_rpcs text[] := array[
    'public.customer_upsert_collection_item(uuid, uuid, text, integer, text, text, text, text, text, text, text, text, text, timestamptz, uuid, timestamptz)',
    'public.customer_soft_delete_collection_item(uuid, uuid, uuid)',
    'public.customer_upsert_binder(uuid, uuid, text, text, text, text, timestamptz, uuid, timestamptz)',
    'public.customer_soft_delete_binder(uuid, uuid, uuid)',
    'public.customer_upsert_binder_membership(uuid, uuid, uuid, uuid, uuid, timestamptz)',
    'public.customer_soft_delete_binder_membership(uuid, uuid, uuid)',
    'public.customer_request_collection_data_deletion(text)'
  ];
begin
  foreach v_sig in array v_public_rpcs
  loop
    v_proc := to_regprocedure(v_sig);
    if v_proc is not null then
      execute format(
        'alter function %s set search_path = public, pg_temp',
        v_proc
      );
      execute format(
        'revoke all on function %s from public, anon',
        v_proc
      );
      execute format(
        'grant execute on function %s to authenticated, service_role',
        v_proc
      );
    end if;
  end loop;

  -- Internal helpers used only by DEFINER RPCs — not public client endpoints.
  foreach v_sig in array array[
    'public.customer_require_auth_uid()',
    'public.customer_operation_payload_hash(text[])'
  ]
  loop
    v_proc := to_regprocedure(v_sig);
    if v_proc is not null then
      execute format(
        'alter function %s set search_path = public, pg_temp',
        v_proc
      );
      execute format(
        'revoke all on function %s from public, anon, authenticated',
        v_proc
      );
      execute format('grant execute on function %s to service_role', v_proc);
    end if;
  end loop;
end;
$$;

-- Harden purge: auth required; authenticated may only purge self;
-- service_role may pass any p_user_id (Edge Function delete-account).
do $$
begin
  if to_regprocedure('public.customer_purge_cloud_collection_data(uuid)') is null then
    return;
  end if;

  execute $ddl$
    create or replace function public.customer_purge_cloud_collection_data(
      p_user_id uuid default null
    )
    returns jsonb
    language plpgsql
    security definer
    set search_path = public, pg_temp
    as $fn$
    declare
      v_role text := coalesce(auth.role(), '');
      v_caller uuid := auth.uid();
      v_uid uuid;
      v_items integer;
      v_binders integer;
      v_memberships integer;
      v_ops integer;
      v_checkpoints integer;
      v_prefs integer;
    begin
      if v_role = 'service_role' then
        v_uid := coalesce(p_user_id, v_caller);
        if v_uid is null then
          raise exception 'p_user_id is required for service_role purge'
            using errcode = '22023';
        end if;
      else
        if v_caller is null then
          raise exception 'Authentication required' using errcode = '42501';
        end if;
        -- Authenticated: never trust a foreign owner id.
        if p_user_id is not null and p_user_id is distinct from v_caller then
          raise exception 'Cannot purge another user''s data' using errcode = '42501';
        end if;
        v_uid := v_caller;
      end if;

      delete from public.customer_binder_memberships where user_id = v_uid;
      get diagnostics v_memberships = row_count;
      delete from public.customer_collection_items where user_id = v_uid;
      get diagnostics v_items = row_count;
      delete from public.customer_binders where user_id = v_uid;
      get diagnostics v_binders = row_count;
      delete from public.customer_sync_operations where user_id = v_uid;
      get diagnostics v_ops = row_count;
      delete from public.customer_sync_checkpoints where user_id = v_uid;
      get diagnostics v_checkpoints = row_count;
      delete from public.customer_sync_preferences where user_id = v_uid;
      get diagnostics v_prefs = row_count;

      return jsonb_build_object(
        'user_id', v_uid,
        'collection_items_deleted', v_items,
        'binders_deleted', v_binders,
        'memberships_deleted', v_memberships,
        'operations_deleted', v_ops,
        'checkpoints_deleted', v_checkpoints,
        'preferences_deleted', v_prefs,
        'purged_at', now()
      );
    end;
    $fn$
  $ddl$;

  comment on function public.customer_purge_cloud_collection_data(uuid) is
    'Removes all customer portal cloud collection data for a user. '
    'Authenticated callers always purge auth.uid(); service_role may pass p_user_id. '
    'Local SQLite is never touched.';

  revoke all on function public.customer_purge_cloud_collection_data(uuid)
    from public, anon;
  grant execute on function public.customer_purge_cloud_collection_data(uuid)
    to authenticated, service_role;
end;
$$;

-- Harden request_deletion: auth required, reason truncated, search_path pinned.
do $$
begin
  if to_regprocedure('public.customer_request_collection_data_deletion(text)') is null then
    return;
  end if;

  execute $ddl$
    create or replace function public.customer_request_collection_data_deletion(
      p_reason text default null
    )
    returns jsonb
    language plpgsql
    security definer
    set search_path = public, pg_temp
    as $fn$
    declare
      v_uid uuid := auth.uid();
      v_result jsonb;
    begin
      if v_uid is null then
        raise exception 'Authentication required' using errcode = '42501';
      end if;

      v_result := public.customer_purge_cloud_collection_data(v_uid);
      v_result := v_result || jsonb_build_object(
        'reason', left(coalesce(p_reason, ''), 500),
        'legacy_user_cards_untouched', true
      );
      return v_result;
    end;
    $fn$
  $ddl$;

  revoke all on function public.customer_request_collection_data_deletion(text)
    from public, anon;
  grant execute on function public.customer_request_collection_data_deletion(text)
    to authenticated, service_role;
end;
$$;

-- =============================================================================
-- D. get_market_price_bundle
-- =============================================================================
-- Prefer INVOKER when RLS allows safe authenticated reads. market_price_*
-- catalogue/cache/evidence tables are readable, but market_price_refresh_jobs
-- is read-own only — INVOKER would hide active jobs enqueued by other roles
-- for the same fingerprint. Keep SECURITY DEFINER, pin search_path, require
-- auth (or service_role), and omit requested_by_user_id from the response.

create or replace function public.get_market_price_bundle(
  p_fingerprint text,
  p_evidence_limit integer default 50
)
returns jsonb
language plpgsql
security definer
stable
set search_path = public, pg_temp
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
  v_fp text;
begin
  if auth.uid() is null and coalesce(auth.role(), '') <> 'service_role' then
    raise exception 'Authentication required' using errcode = '42501';
  end if;

  v_fp := lower(trim(coalesce(p_fingerprint, '')));
  if v_fp = '' or char_length(v_fp) > 200 then
    raise exception 'fingerprint must be 1..200 characters' using errcode = '22023';
  end if;

  select * into v_key
  from public.market_price_keys
  where fingerprint = v_fp
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
    elsif v_cache.last_updated_at is not null
      and v_cache.stale_after is not null
      and now() >= v_cache.stale_after then
      v_cache_state := 'stale';
    elsif v_cache.last_updated_at is not null then
      v_cache_state := 'fresh';
    else
      v_cache_state := 'missing';
    end if;
  end if;

  if v_active_job.id is not null then
    v_state := case
      when v_active_job.status = 'running' then 'refresh_running'
      else 'refresh_queued'
    end;
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
    -- Safe subset only — never expose requested_by_user_id.
    'active_refresh_job', case
      when v_active_job.id is null then null
      else jsonb_build_object(
        'id', v_active_job.id,
        'status', v_active_job.status,
        'priority', v_active_job.priority,
        'reason', v_active_job.reason,
        'requested_at', v_active_job.requested_at
      )
    end,
    'sold_listing_evidence', v_evidence,
    'state', v_state,
    'cache_state', v_cache_state,
    'refresh_state', coalesce(v_active_job.status, v_cache.refresh_status),
    'cache_has_current_price', coalesce(v_cache.current_market_price is not null, false),
    'current_market_evidence_available',
      coalesce(
        v_cache.current_market_price is not null and v_cache.sample_size > 0,
        false
      ),
    'stale_cache_available', v_cache_state = 'stale',
    'no_reliable_price_reason', v_no_reliable_price_reason
  );
end;
$$;

comment on function public.get_market_price_bundle(text, integer) is
  'Authenticated market price bundle read. SECURITY DEFINER retained so active '
  'refresh jobs remain visible despite read-own RLS; response omits requester ids.';

revoke all on function public.get_market_price_bundle(text, integer)
  from public, anon;
grant execute on function public.get_market_price_bundle(text, integer)
  to authenticated, service_role;

-- =============================================================================
-- E. request_market_price_refresh — auth, size limits, cooldown, no anon
-- =============================================================================

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
    v_aliases
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
    10,
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

comment on function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text,
  boolean, text, text, jsonb
) is
  'Authenticated market refresh request. Enforces auth, payload size limits, '
  'per-key cooldown, force_refresh=service_role only, and soft per-user rate limit.';

revoke all on function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text,
  boolean, text, text, jsonb
) from public, anon;
grant execute on function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text,
  boolean, text, text, jsonb
) to authenticated, service_role;

-- =============================================================================
-- F. RLS policy optimization: (select auth.uid()) on customer_* policies
-- =============================================================================

do $$
begin
  if to_regclass('public.customer_sync_preferences') is null then
    null;
  else
    execute 'drop policy if exists customer_sync_preferences_select_own on public.customer_sync_preferences';
    execute $p$
      create policy customer_sync_preferences_select_own
        on public.customer_sync_preferences
        for select to authenticated
        using (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_sync_preferences_insert_own on public.customer_sync_preferences';
    execute $p$
      create policy customer_sync_preferences_insert_own
        on public.customer_sync_preferences
        for insert to authenticated
        with check (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_sync_preferences_update_own on public.customer_sync_preferences';
    execute $p$
      create policy customer_sync_preferences_update_own
        on public.customer_sync_preferences
        for update to authenticated
        using (user_id = (select auth.uid()))
        with check (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_sync_preferences_delete_own on public.customer_sync_preferences';
    execute $p$
      create policy customer_sync_preferences_delete_own
        on public.customer_sync_preferences
        for delete to authenticated
        using (user_id = (select auth.uid()))
    $p$;
  end if;

  if to_regclass('public.customer_collection_items') is not null then
    execute 'drop policy if exists customer_collection_items_select_own on public.customer_collection_items';
    execute $p$
      create policy customer_collection_items_select_own
        on public.customer_collection_items
        for select to authenticated
        using (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_collection_items_insert_own on public.customer_collection_items';
    execute $p$
      create policy customer_collection_items_insert_own
        on public.customer_collection_items
        for insert to authenticated
        with check (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_collection_items_update_own on public.customer_collection_items';
    execute $p$
      create policy customer_collection_items_update_own
        on public.customer_collection_items
        for update to authenticated
        using (user_id = (select auth.uid()))
        with check (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_collection_items_delete_own on public.customer_collection_items';
    execute $p$
      create policy customer_collection_items_delete_own
        on public.customer_collection_items
        for delete to authenticated
        using (user_id = (select auth.uid()))
    $p$;
  end if;

  if to_regclass('public.customer_binders') is not null then
    execute 'drop policy if exists customer_binders_select_own on public.customer_binders';
    execute $p$
      create policy customer_binders_select_own
        on public.customer_binders
        for select to authenticated
        using (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_binders_insert_own on public.customer_binders';
    execute $p$
      create policy customer_binders_insert_own
        on public.customer_binders
        for insert to authenticated
        with check (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_binders_update_own on public.customer_binders';
    execute $p$
      create policy customer_binders_update_own
        on public.customer_binders
        for update to authenticated
        using (user_id = (select auth.uid()))
        with check (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_binders_delete_own on public.customer_binders';
    execute $p$
      create policy customer_binders_delete_own
        on public.customer_binders
        for delete to authenticated
        using (user_id = (select auth.uid()))
    $p$;
  end if;

  if to_regclass('public.customer_binder_memberships') is not null then
    execute 'drop policy if exists customer_binder_memberships_select_own on public.customer_binder_memberships';
    execute $p$
      create policy customer_binder_memberships_select_own
        on public.customer_binder_memberships
        for select to authenticated
        using (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_binder_memberships_insert_own on public.customer_binder_memberships';
    execute $p$
      create policy customer_binder_memberships_insert_own
        on public.customer_binder_memberships
        for insert to authenticated
        with check (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_binder_memberships_update_own on public.customer_binder_memberships';
    execute $p$
      create policy customer_binder_memberships_update_own
        on public.customer_binder_memberships
        for update to authenticated
        using (user_id = (select auth.uid()))
        with check (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_binder_memberships_delete_own on public.customer_binder_memberships';
    execute $p$
      create policy customer_binder_memberships_delete_own
        on public.customer_binder_memberships
        for delete to authenticated
        using (user_id = (select auth.uid()))
    $p$;
  end if;

  if to_regclass('public.customer_sync_operations') is not null then
    execute 'drop policy if exists customer_sync_operations_select_own on public.customer_sync_operations';
    execute $p$
      create policy customer_sync_operations_select_own
        on public.customer_sync_operations
        for select to authenticated
        using (user_id = (select auth.uid()))
    $p$;
  end if;

  if to_regclass('public.customer_sync_checkpoints') is not null then
    execute 'drop policy if exists customer_sync_checkpoints_select_own on public.customer_sync_checkpoints';
    execute $p$
      create policy customer_sync_checkpoints_select_own
        on public.customer_sync_checkpoints
        for select to authenticated
        using (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_sync_checkpoints_insert_own on public.customer_sync_checkpoints';
    execute $p$
      create policy customer_sync_checkpoints_insert_own
        on public.customer_sync_checkpoints
        for insert to authenticated
        with check (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_sync_checkpoints_update_own on public.customer_sync_checkpoints';
    execute $p$
      create policy customer_sync_checkpoints_update_own
        on public.customer_sync_checkpoints
        for update to authenticated
        using (user_id = (select auth.uid()))
        with check (user_id = (select auth.uid()))
    $p$;
    execute 'drop policy if exists customer_sync_checkpoints_delete_own on public.customer_sync_checkpoints';
    execute $p$
      create policy customer_sync_checkpoints_delete_own
        on public.customer_sync_checkpoints
        for delete to authenticated
        using (user_id = (select auth.uid()))
    $p$;
  end if;
end;
$$;

-- =============================================================================
-- G. Missing FK indexes for customer_binder_memberships
-- =============================================================================

do $$
begin
  if to_regclass('public.customer_binder_memberships') is null then
    return;
  end if;

  execute $ddl$
    create index if not exists idx_customer_binder_memberships_binder_id
      on public.customer_binder_memberships (binder_id)
  $ddl$;

  execute $ddl$
    create index if not exists idx_customer_binder_memberships_collection_item_id
      on public.customer_binder_memberships (collection_item_id)
  $ddl$;
end;
$$;
