-- Resolve PostgREST PGRST203 for enqueue_market_price_refresh.
-- Production currently has both smallint and integer overloads for p_priority,
-- so JSON numeric args are ambiguous. Keep the canonical smallint signature.

drop function if exists public.enqueue_market_price_refresh(uuid, text, integer, uuid, text);

-- Ensure the intended smallint overload remains executable by service_role /
-- authenticated callers used by request_market_price_refresh.
do $$
begin
  if to_regprocedure('public.enqueue_market_price_refresh(uuid, text, smallint, uuid, text)') is null then
    raise exception 'canonical enqueue_market_price_refresh(smallint) missing after integer overload drop';
  end if;
end;
$$;

revoke all on function public.enqueue_market_price_refresh(uuid, text, smallint, uuid, text)
  from public, anon;
grant execute on function public.enqueue_market_price_refresh(uuid, text, smallint, uuid, text)
  to authenticated, service_role;

comment on function public.enqueue_market_price_refresh(uuid, text, smallint, uuid, text) is
  'Canonical market price refresh enqueue path. Priority is smallint; integer overload removed to avoid PostgREST PGRST203.';
