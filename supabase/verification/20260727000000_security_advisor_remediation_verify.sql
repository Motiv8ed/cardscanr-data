-- Post-migration verification for 20260727000000_security_advisor_remediation.
-- Run with a privileged role (postgres / service_role SQL editor), then
-- SET ROLE to anon / authenticated for client checks.
-- Do not print secrets, source hashes, or full URLs of non-public rows.

-- =============================================================================
-- A. Object / grant posture
-- =============================================================================

select
  c.relname,
  c.reloptions,
  pg_get_userbyid(c.relowner) as owner
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public'
  and c.relname in (
    'card_image_manifests_current',
    'card_image_manifests_with_legacy_records'
  )
order by 1;

-- Expect: reloptions contains security_invoker=true

select
  table_name,
  grantee,
  privilege_type
from information_schema.role_table_grants
where table_schema = 'public'
  and table_name in (
    'card_image_manifests',
    'card_image_manifests_current',
    'card_image_manifests_with_legacy_records'
  )
  and grantee in ('anon', 'authenticated', 'service_role', 'PUBLIC')
order by table_name, grantee, privilege_type;

-- Expect:
--   card_image_manifests: SELECT for anon/authenticated; DML for service_role
--   card_image_manifests_current: SELECT for anon/authenticated/service_role
--   card_image_manifests_with_legacy_records: SELECT for service_role only

select
  p.proname,
  pg_get_function_identity_arguments(p.oid) as args,
  p.prosecdef as security_definer,
  p.proconfig,
  has_function_privilege('anon', p.oid, 'EXECUTE') as anon_exec,
  has_function_privilege('authenticated', p.oid, 'EXECUTE') as auth_exec,
  has_function_privilege('service_role', p.oid, 'EXECUTE') as service_exec
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.proname in (
    'get_market_price_bundle',
    'get_or_create_market_price_key',
    'handle_new_user',
    'handle_new_user_default_collection',
    'request_market_price_refresh',
    'rls_auto_enable'
  )
order by 1;

-- Expect:
--   get_market_price_bundle: anon false, authenticated true, service true
--   request_market_price_refresh: anon false, authenticated true, service true
--   get_or_create_market_price_key: anon false, authenticated false, service true
--   handle_new_user*: all false for anon/authenticated/service_role
--   rls_auto_enable: all false for anon/authenticated/service_role

select polname
from pg_policy pol
join pg_class c on c.oid = pol.polrelid
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'storage'
  and c.relname = 'objects'
  and pol.polname = 'pokemon_card_images_public_read';

-- Expect: 0 rows (policy removed)

-- =============================================================================
-- B. Anonymous role behaviour
-- =============================================================================

set local role anon;

-- Intended public manifests readable via current view
select count(*) as anon_current_view_count
from public.card_image_manifests_current;

-- Base table still RLS-filtered to current+verified
select
  count(*) filter (
    where is_current and verification_status = 'verified'
  ) as anon_intended_rows,
  count(*) filter (
    where not (is_current and verification_status = 'verified')
  ) as anon_hidden_leaks
from public.card_image_manifests;

-- Expect anon_hidden_leaks = 0

-- Legacy admin view must be denied
do $$
begin
  begin
    perform 1 from public.card_image_manifests_with_legacy_records limit 1;
    raise exception 'FAIL: anon can still read card_image_manifests_with_legacy_records';
  exception
    when insufficient_privilege then
      raise notice 'PASS: anon cannot read legacy view';
  end;
end;
$$;

-- Internal functions must be denied
do $$
begin
  begin
    perform public.handle_new_user();
    raise exception 'FAIL: anon executed handle_new_user';
  exception
    when insufficient_privilege then
      raise notice 'PASS: anon cannot execute handle_new_user';
    when others then
      -- wrong call shape still proves EXECUTE may exist; privilege check above
      if sqlerrm ilike '%permission denied%' then
        raise notice 'PASS: anon cannot execute handle_new_user';
      else
        raise;
      end if;
  end;
end;
$$;

do $$
begin
  begin
    perform public.rls_auto_enable();
    raise exception 'FAIL: anon executed rls_auto_enable';
  exception
    when insufficient_privilege then
      raise notice 'PASS: anon cannot execute rls_auto_enable';
    when others then
      if sqlerrm ilike '%permission denied%' then
        raise notice 'PASS: anon cannot execute rls_auto_enable';
      else
        raise;
      end if;
  end;
end;
$$;

do $$
begin
  begin
    perform public.get_market_price_bundle('nonexistent-fingerprint', 1);
    raise exception 'FAIL: anon executed get_market_price_bundle';
  exception
    when insufficient_privilege then
      raise notice 'PASS: anon cannot execute get_market_price_bundle';
    when others then
      if sqlerrm ilike '%permission denied%' then
        raise notice 'PASS: anon cannot execute get_market_price_bundle';
      else
        raise;
      end if;
  end;
end;
$$;

reset role;

-- =============================================================================
-- C. Authenticated role behaviour (catalogue + pricing grants only)
-- =============================================================================

set local role authenticated;

select count(*) as auth_current_view_count
from public.card_image_manifests_current;

select
  count(*) filter (
    where not (is_current and verification_status = 'verified')
  ) as auth_hidden_leaks
from public.card_image_manifests;

-- Expect auth_hidden_leaks = 0

do $$
begin
  begin
    perform 1 from public.card_image_manifests_with_legacy_records limit 1;
    raise exception 'FAIL: authenticated can still read legacy view';
  exception
    when insufficient_privilege then
      raise notice 'PASS: authenticated cannot read legacy view';
  end;
end;
$$;

-- Pricing RPCs remain executable for authenticated (body may return null)
select public.get_market_price_bundle('nonexistent-fingerprint', 1)
  is null as auth_bundle_callable_null_ok;

reset role;

-- =============================================================================
-- D. Service role / publisher retains pipeline access
-- =============================================================================

set local role service_role;

select count(*) as service_legacy_view_count
from public.card_image_manifests_with_legacy_records;

select count(*) as service_all_manifests
from public.card_image_manifests;

reset role;

-- =============================================================================
-- E. Overexposure closed (privileged compare)
-- =============================================================================

select
  (select count(*) from public.card_image_manifests) as total_manifests,
  (select count(*) from public.card_image_manifests
     where is_current and verification_status = 'verified') as intended_public,
  (select count(*) from public.card_image_manifests_current) as current_view_rows;

-- After remediation, anon/authenticated must not see superseded/non-current
-- rows via either the base table or the legacy view.
