-- Post-migration verification for 20260804140000_play_v56_security_hardening.
-- Run with a privileged role (postgres / SQL editor).
-- Do not print secrets, JWTs, emails, or PII.

-- =============================================================================
-- A. search_path + grants for hardened functions
-- =============================================================================

select
  p.proname,
  pg_get_function_identity_arguments(p.oid) as args,
  p.prosecdef as security_definer,
  p.proconfig as config,
  has_function_privilege('anon', p.oid, 'EXECUTE') as anon_exec,
  has_function_privilege('authenticated', p.oid, 'EXECUTE') as auth_exec,
  has_function_privilege('service_role', p.oid, 'EXECUTE') as service_exec
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.proname in (
    'customer_force_owner_user_id',
    'customer_set_updated_at',
    'customer_begin_operation',
    'customer_ack_operation',
    'customer_upsert_collection_item',
    'customer_soft_delete_collection_item',
    'customer_upsert_binder',
    'customer_soft_delete_binder',
    'customer_upsert_binder_membership',
    'customer_soft_delete_binder_membership',
    'customer_purge_cloud_collection_data',
    'customer_request_collection_data_deletion',
    'get_market_price_bundle',
    'request_market_price_refresh'
  )
order by 1, 2;

-- Expect (when objects exist):
--   customer_force_owner_user_id / customer_set_updated_at:
--     proconfig contains search_path=public, pg_temp
--   customer_begin_operation / customer_ack_operation:
--     anon_exec=false, auth_exec=false, service_exec=true
--     security_definer=true, search_path pinned
--   customer mutation RPCs + purge + request_deletion:
--     anon_exec=false, auth_exec=true, service_exec=true
--   get_market_price_bundle / request_market_price_refresh:
--     anon_exec=false, auth_exec=true, service_exec=true
--     search_path contains public and pg_temp

-- Explicit search_path presence check
select
  p.proname,
  pg_get_function_identity_arguments(p.oid) as args,
  exists (
    select 1
    from unnest(coalesce(p.proconfig, array[]::text[])) cfg
    where cfg ilike 'search_path=%public%'
      and cfg ilike '%pg_temp%'
  ) as search_path_public_pg_temp
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.proname in (
    'customer_force_owner_user_id',
    'customer_set_updated_at',
    'customer_begin_operation',
    'customer_ack_operation',
    'customer_purge_cloud_collection_data',
    'get_market_price_bundle',
    'request_market_price_refresh'
  )
order by 1;

-- Expect: search_path_public_pg_temp = true for each existing function

-- =============================================================================
-- B. Internal helpers denied to anon / authenticated
-- =============================================================================

do $$
declare
  v_begin regprocedure := to_regprocedure(
    'public.customer_begin_operation(uuid, text, text, uuid)'
  );
begin
  if v_begin is null then
    raise notice 'SKIP: customer_begin_operation not present';
    return;
  end if;
  if has_function_privilege('anon', v_begin, 'EXECUTE') then
    raise exception 'FAIL: anon can execute customer_begin_operation';
  end if;
  if has_function_privilege('authenticated', v_begin, 'EXECUTE') then
    raise exception 'FAIL: authenticated can execute customer_begin_operation';
  end if;
  if not has_function_privilege('service_role', v_begin, 'EXECUTE') then
    raise exception 'FAIL: service_role missing EXECUTE on customer_begin_operation';
  end if;
  raise notice 'PASS: customer_begin_operation grants';
end;
$$;

do $$
declare
  v_ack regprocedure := to_regprocedure(
    'public.customer_ack_operation(uuid, text, uuid, text, text, bigint, uuid)'
  );
begin
  if v_ack is null then
    raise notice 'SKIP: customer_ack_operation not present';
    return;
  end if;
  if has_function_privilege('anon', v_ack, 'EXECUTE') then
    raise exception 'FAIL: anon can execute customer_ack_operation';
  end if;
  if has_function_privilege('authenticated', v_ack, 'EXECUTE') then
    raise exception 'FAIL: authenticated can execute customer_ack_operation';
  end if;
  if not has_function_privilege('service_role', v_ack, 'EXECUTE') then
    raise exception 'FAIL: service_role missing EXECUTE on customer_ack_operation';
  end if;
  raise notice 'PASS: customer_ack_operation grants';
end;
$$;

-- =============================================================================
-- C. Market RPCs: anon denied
-- =============================================================================

do $$
begin
  begin
    set local role anon;
    perform public.get_market_price_bundle('nonexistent-fingerprint', 1);
    reset role;
    raise exception 'FAIL: anon executed get_market_price_bundle';
  exception
    when insufficient_privilege then
      reset role;
      raise notice 'PASS: anon cannot execute get_market_price_bundle';
    when others then
      reset role;
      if sqlerrm ilike '%permission denied%' then
        raise notice 'PASS: anon cannot execute get_market_price_bundle';
      else
        raise;
      end if;
  end;
end;
$$;

do $$
declare
  v_proc regprocedure := to_regprocedure(
    'public.request_market_price_refresh(text, text, text, text, text, text, text, text, text, text, text, text, text, boolean, text, text, jsonb)'
  );
begin
  if v_proc is null then
    raise exception 'FAIL: request_market_price_refresh signature missing';
  end if;
  if has_function_privilege('anon', v_proc, 'EXECUTE') then
    raise exception 'FAIL: anon can execute request_market_price_refresh';
  end if;
  if not has_function_privilege('authenticated', v_proc, 'EXECUTE') then
    raise exception 'FAIL: authenticated missing EXECUTE on request_market_price_refresh';
  end if;
  raise notice 'PASS: request_market_price_refresh grants';
end;
$$;

-- =============================================================================
-- D. RLS policies use (select auth.uid())
-- =============================================================================

select
  c.relname as table_name,
  pol.polname as policy_name,
  pg_get_expr(pol.polqual, pol.polrelid) as using_expr,
  pg_get_expr(pol.polwithcheck, pol.polrelid) as with_check_expr
from pg_policy pol
join pg_class c on c.oid = pol.polrelid
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public'
  and c.relname like 'customer\_%' escape '\'
order by 1, 2;

-- Expect using/with_check expressions to contain `(SELECT auth.uid())`
-- rather than bare `auth.uid()` where policies exist.

-- =============================================================================
-- E. FK indexes on customer_binder_memberships
-- =============================================================================

select
  i.relname as index_name,
  pg_get_indexdef(i.oid) as index_def
from pg_class t
join pg_namespace n on n.oid = t.relnamespace
join pg_index x on x.indrelid = t.oid
join pg_class i on i.oid = x.indexrelid
where n.nspname = 'public'
  and t.relname = 'customer_binder_memberships'
  and i.relname in (
    'idx_customer_binder_memberships_binder_id',
    'idx_customer_binder_memberships_collection_item_id'
  )
order by 1;

-- Expect 2 rows when customer_binder_memberships exists.
-- SKIP (0 rows) is acceptable only when the customer table is not deployed.
