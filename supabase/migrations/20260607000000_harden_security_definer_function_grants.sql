-- Harden SECURITY DEFINER RPCs and trigger helpers reported by Supabase Security Advisor.
--
-- PostgreSQL grants EXECUTE on new functions to PUBLIC by default.  For SECURITY
-- DEFINER functions that can read or mutate protected tables, keep direct RPC
-- access limited to the roles that actually need it.

alter function public.set_updated_at() set search_path = public;
revoke all on function public.set_updated_at() from public, anon, authenticated, service_role;
grant execute on function public.set_updated_at() to service_role;

alter function public.get_or_create_market_price_key(
  text, text, text, text, text, text, text, text, text, text, text, text, timestamptz
) set search_path = public;
revoke all on function public.get_or_create_market_price_key(
  text, text, text, text, text, text, text, text, text, text, text, text, timestamptz
) from public, anon, authenticated, service_role;
grant execute on function public.get_or_create_market_price_key(
  text, text, text, text, text, text, text, text, text, text, text, text, timestamptz
) to service_role;

alter function public.enqueue_market_price_refresh(uuid, text, smallint, uuid, text) set search_path = public;
revoke all on function public.enqueue_market_price_refresh(uuid, text, smallint, uuid, text)
  from public, anon, authenticated, service_role;
grant execute on function public.enqueue_market_price_refresh(uuid, text, smallint, uuid, text)
  to service_role;

alter function public.claim_market_price_refresh_jobs(text, integer) set search_path = public;
revoke all on function public.claim_market_price_refresh_jobs(text, integer)
  from public, anon, authenticated, service_role;
grant execute on function public.claim_market_price_refresh_jobs(text, integer) to service_role;

alter function public.complete_market_price_refresh_job(
  uuid, uuid, timestamptz, timestamptz, timestamptz
) set search_path = public;
revoke all on function public.complete_market_price_refresh_job(
  uuid, uuid, timestamptz, timestamptz, timestamptz
) from public, anon, authenticated, service_role;
grant execute on function public.complete_market_price_refresh_job(
  uuid, uuid, timestamptz, timestamptz, timestamptz
) to service_role;

alter function public.fail_market_price_refresh_job(
  uuid, text, boolean, integer, integer
) set search_path = public;
revoke all on function public.fail_market_price_refresh_job(
  uuid, text, boolean, integer, integer
) from public, anon, authenticated, service_role;
grant execute on function public.fail_market_price_refresh_job(
  uuid, text, boolean, integer, integer
) to service_role;

alter function public.upsert_market_price_refresh_cache_state(
  uuid, text, text, text, text
) set search_path = public;
revoke all on function public.upsert_market_price_refresh_cache_state(
  uuid, text, text, text, text
) from public, anon, authenticated, service_role;
grant execute on function public.upsert_market_price_refresh_cache_state(
  uuid, text, text, text, text
) to service_role;

alter function public.market_price_refresh_cooldown_hours(
  public.market_price_cache, public.market_price_keys
) set search_path = public;
revoke all on function public.market_price_refresh_cooldown_hours(
  public.market_price_cache, public.market_price_keys
) from public, anon, authenticated, service_role;
grant execute on function public.market_price_refresh_cooldown_hours(
  public.market_price_cache, public.market_price_keys
) to service_role;

alter function public.market_price_supported_route(text, text, text) set search_path = public;
revoke all on function public.market_price_supported_route(text, text, text)
  from public, anon, authenticated, service_role;
grant execute on function public.market_price_supported_route(text, text, text) to service_role;

alter function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text, boolean
) set search_path = public;
revoke all on function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text, boolean
) from public, anon, authenticated, service_role;
grant execute on function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text, boolean
) to authenticated, service_role;

alter function public.get_market_price_bundle(text, integer) set search_path = public;
revoke all on function public.get_market_price_bundle(text, integer)
  from public, anon, authenticated, service_role;
grant execute on function public.get_market_price_bundle(text, integer)
  to authenticated, service_role;

do $$
declare
  v_proc regprocedure;
  v_signature text;
begin
  foreach v_signature in array array[
    'public.enqueue_market_price_refresh(uuid,text,integer,uuid,text)',
    'public.handle_new_user()',
    'public.handle_new_user_default_collection()',
    'public.rls_auto_enable()'
  ]
  loop
    v_proc := to_regprocedure(v_signature);
    if v_proc is not null then
      execute format('alter function %s set search_path = public', v_proc);
      execute format('revoke all on function %s from public, anon, authenticated, service_role', v_proc);
      execute format('grant execute on function %s to service_role', v_proc);
    end if;
  end loop;
end;
$$;
