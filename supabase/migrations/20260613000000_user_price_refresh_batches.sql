create table if not exists public.user_price_refresh_batches (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  market text not null,
  currency text not null,
  source text not null default 'login',
  status text not null default 'queued',
  total_count integer not null default 0,
  cache_hit_count integer not null default 0,
  queued_count integer not null default 0,
  completed_count integer not null default 0,
  cooldown_count integer not null default 0,
  skipped_count integer not null default 0,
  failed_count integer not null default 0,
  notify_on_complete boolean not null default true,
  notification_created_at timestamptz,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  constraint user_price_refresh_batches_status_valid check (
    status in ('queued', 'running', 'completed', 'partial_failed', 'failed')
  )
);

create table if not exists public.user_price_refresh_batch_items (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references public.user_price_refresh_batches(id) on delete cascade,
  user_card_id uuid not null,
  card_id text,
  provider_card_id text,
  market_price_key_id uuid references public.market_price_keys(id) on delete set null,
  cache_key text,
  refresh_job_id uuid references public.market_price_refresh_jobs(id) on delete set null,
  status text not null default 'queued',
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint user_price_refresh_batch_items_status_valid check (
    status in ('cache_hit', 'queued', 'running', 'completed', 'cooldown', 'skipped', 'failed')
  )
);

create table if not exists public.user_push_tokens (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  platform text not null,
  token text not null,
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, platform, token)
);

create table if not exists public.user_notification_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  batch_id uuid references public.user_price_refresh_batches(id) on delete cascade,
  type text not null,
  title text not null,
  body text not null,
  status text not null default 'queued',
  created_at timestamptz not null default now(),
  sent_at timestamptz
);

create index if not exists idx_user_price_refresh_batches_user_created
  on public.user_price_refresh_batches(user_id, created_at desc);
create index if not exists idx_user_price_refresh_batches_active
  on public.user_price_refresh_batches(user_id, market, currency, status)
  where status in ('queued', 'running');
create index if not exists idx_user_price_refresh_batch_items_batch_status
  on public.user_price_refresh_batch_items(batch_id, status);
create index if not exists idx_user_price_refresh_batch_items_job
  on public.user_price_refresh_batch_items(refresh_job_id)
  where refresh_job_id is not null;

alter table public.user_price_refresh_batches enable row level security;
alter table public.user_price_refresh_batch_items enable row level security;
alter table public.user_push_tokens enable row level security;
alter table public.user_notification_events enable row level security;

create policy if not exists user_price_refresh_batches_read_own
on public.user_price_refresh_batches for select
to authenticated
using (user_id = auth.uid());

create policy if not exists user_price_refresh_batch_items_read_own
on public.user_price_refresh_batch_items for select
to authenticated
using (
  exists (
    select 1 from public.user_price_refresh_batches b
    where b.id = batch_id and b.user_id = auth.uid()
  )
);

create policy if not exists user_push_tokens_own
on public.user_push_tokens for all
to authenticated
using (user_id = auth.uid())
with check (user_id = auth.uid());

create policy if not exists user_notification_events_read_own
on public.user_notification_events for select
to authenticated
using (user_id = auth.uid());

create policy if not exists user_price_refresh_batches_service_role_all
on public.user_price_refresh_batches for all
to service_role
using (true)
with check (true);

create policy if not exists user_price_refresh_batch_items_service_role_all
on public.user_price_refresh_batch_items for all
to service_role
using (true)
with check (true);

create policy if not exists user_push_tokens_service_role_all
on public.user_push_tokens for all
to service_role
using (true)
with check (true);

create policy if not exists user_notification_events_service_role_all
on public.user_notification_events for all
to service_role
using (true)
with check (true);

create or replace function public.cardscanr_normalize_price_text(p_value text)
returns text
language sql
immutable
as $$
  select coalesce(
    nullif(regexp_replace(lower(trim(coalesce(p_value, ''))), '[^a-z0-9]+', '_', 'g'), ''),
    'unknown'
  );
$$;

create or replace function public.cardscanr_market_price_fingerprint(
  p_card_name text,
  p_set_name text,
  p_set_code text,
  p_collector_number text,
  p_language text,
  p_variant text,
  p_condition text,
  p_market text,
  p_currency text
)
returns text
language sql
immutable
as $$
  select concat_ws(
    '|',
    'pokemon',
    coalesce(nullif(lower(trim(p_language)), ''), 'en'),
    coalesce(nullif(lower(trim(p_set_code)), ''), public.cardscanr_normalize_price_text(p_set_name)),
    coalesce(nullif(upper(regexp_replace(trim(coalesce(p_collector_number, '')), '[^A-Za-z0-9/]+', '', 'g')), ''), '-'),
    public.cardscanr_normalize_price_text(p_card_name),
    coalesce(nullif(lower(trim(p_variant)), ''), 'raw'),
    coalesce(nullif(lower(trim(p_condition)), ''), 'raw'),
    lower(trim(p_market)),
    lower(trim(p_currency))
  );
$$;

create or replace function public.enqueue_missing_prices_for_user_collection(
  p_user_id uuid,
  p_market text,
  p_currency text,
  p_source text,
  p_notify_on_complete boolean default true
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_batch_id uuid;
  v_row record;
  v_result jsonb;
  v_action text;
  v_price_key_id uuid;
  v_job_id uuid;
  v_status text;
  v_total integer := 0;
  v_cache_hit integer := 0;
  v_queued integer := 0;
  v_cooldown integer := 0;
  v_skipped integer := 0;
  v_failed integer := 0;
  v_market text := upper(trim(coalesce(p_market, 'AU')));
  v_currency text := upper(trim(coalesce(p_currency, 'AUD')));
begin
  if auth.uid() is distinct from p_user_id and coalesce(auth.role(), '') <> 'service_role' then
    raise exception 'Cannot enqueue price refresh for another user'
      using errcode = '42501';
  end if;

  select id into v_batch_id
  from public.user_price_refresh_batches
  where user_id = p_user_id
    and market = v_market
    and currency = v_currency
    and source = coalesce(nullif(trim(p_source), ''), 'login')
    and status in ('queued', 'running')
    and created_at > now() - interval '6 hours'
  order by created_at desc
  limit 1;

  if v_batch_id is not null then
    return public.get_user_price_refresh_batch_status(v_batch_id);
  end if;

  insert into public.user_price_refresh_batches (
    user_id, market, currency, source, status, notify_on_complete
  )
  values (
    p_user_id,
    v_market,
    v_currency,
    coalesce(nullif(trim(p_source), ''), 'login'),
    'running',
    coalesce(p_notify_on_complete, true)
  )
  returning id into v_batch_id;

  for v_row in
    select
      uc.id,
      uc.card_id,
      uc.card_id as provider_card_id,
      uc.card_name,
      uc.set_name,
      uc.set_id,
      uc.collector_number,
      coalesce(nullif(uc.language, ''), 'en') as language,
      coalesce(nullif(uc.variant, ''), 'raw') as variant,
      coalesce(nullif(uc.condition, ''), 'raw') as condition
    from public.user_cards uc
    where uc.user_id = p_user_id
  loop
    v_total := v_total + 1;
    if coalesce(trim(v_row.card_name), '') = ''
      or coalesce(trim(v_row.collector_number), '') = ''
      or (coalesce(trim(v_row.set_id), '') = '' and coalesce(trim(v_row.set_name), '') = '') then
      v_skipped := v_skipped + 1;
      insert into public.user_price_refresh_batch_items (
        batch_id, user_card_id, card_id, provider_card_id, status, error
      )
      values (
        v_batch_id, v_row.id, v_row.card_id, v_row.provider_card_id, 'skipped',
        'missing_identity'
      );
      continue;
    end if;

    v_result := public.request_market_price_refresh(
      'pokemon',
      v_row.card_name,
      public.cardscanr_normalize_price_text(v_row.card_name),
      coalesce(v_row.set_name, ''),
      coalesce(v_row.set_id, ''),
      v_row.collector_number,
      lower(v_row.language),
      lower(v_row.variant),
      lower(v_row.condition),
      v_market,
      v_currency,
      public.cardscanr_market_price_fingerprint(
        v_row.card_name, v_row.set_name, v_row.set_id, v_row.collector_number,
        v_row.language, v_row.variant, v_row.condition, v_market, v_currency
      ),
      'user_collection_' || coalesce(nullif(trim(p_source), ''), 'login'),
      false
    );
    v_action := v_result->>'action';
    v_price_key_id := nullif(v_result->>'price_key_id', '')::uuid;
    v_job_id := nullif(v_result->>'job_id', '')::uuid;
    v_status := case
      when v_action = 'cache_fresh' and coalesce((v_result->>'cache_has_current_price')::boolean, false) then 'cache_hit'
      when v_action = 'cache_fresh' then 'cooldown'
      when v_action in ('job_enqueued', 'active_job_exists') then 'queued'
      else 'failed'
    end;

    if v_status = 'cache_hit' then v_cache_hit := v_cache_hit + 1;
    elsif v_status = 'cooldown' then v_cooldown := v_cooldown + 1;
    elsif v_status = 'queued' then v_queued := v_queued + 1;
    else v_failed := v_failed + 1;
    end if;

    insert into public.user_price_refresh_batch_items (
      batch_id, user_card_id, card_id, provider_card_id, market_price_key_id,
      cache_key, refresh_job_id, status, error
    )
    values (
      v_batch_id, v_row.id, v_row.card_id, v_row.provider_card_id,
      v_price_key_id, v_result->>'p_fingerprint', v_job_id, v_status,
      case when v_status = 'failed' then coalesce(v_result->>'state', 'request_failed') else null end
    );
  end loop;

  update public.user_price_refresh_batches
  set
    status = case
      when v_total = 0 then 'completed'
      when v_failed > 0 and (v_cache_hit + v_queued + v_cooldown) > 0 then 'partial_failed'
      when v_failed > 0 then 'failed'
      when v_queued > 0 then 'running'
      else 'completed'
    end,
    total_count = v_total,
    cache_hit_count = v_cache_hit,
    queued_count = v_queued,
    cooldown_count = v_cooldown,
    skipped_count = v_skipped,
    failed_count = v_failed,
    completed_at = case when v_queued = 0 then now() else null end
  where id = v_batch_id;

  return public.get_user_price_refresh_batch_status(v_batch_id);
end;
$$;

create or replace function public.get_user_price_refresh_batch_status(p_batch_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_batch public.user_price_refresh_batches;
begin
  select * into v_batch
  from public.user_price_refresh_batches
  where id = p_batch_id
  limit 1;

  if v_batch.id is null then
    return null;
  end if;
  if auth.uid() is distinct from v_batch.user_id and coalesce(auth.role(), '') <> 'service_role' then
    raise exception 'Cannot read another user price refresh batch'
      using errcode = '42501';
  end if;

  return jsonb_build_object(
    'batch_id', v_batch.id,
    'user_id', v_batch.user_id,
    'market', v_batch.market,
    'currency', v_batch.currency,
    'source', v_batch.source,
    'status', v_batch.status,
    'total_count', v_batch.total_count,
    'cache_hit_count', v_batch.cache_hit_count,
    'queued_count', v_batch.queued_count,
    'completed_count', v_batch.completed_count,
    'cooldown_count', v_batch.cooldown_count,
    'skipped_count', v_batch.skipped_count,
    'failed_count', v_batch.failed_count,
    'created_at', v_batch.created_at,
    'completed_at', v_batch.completed_at
  );
end;
$$;

create or replace function public.update_user_price_refresh_batch_item_from_job()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.status in ('completed', 'failed', 'cancelled') then
    update public.user_price_refresh_batch_items
    set
      status = case
        when new.status = 'completed' then 'completed'
        else 'failed'
      end,
      error = case when new.status = 'completed' then null else new.error_message end,
      updated_at = now()
    where refresh_job_id = new.id;

    update public.user_price_refresh_batches b
    set
      completed_count = s.completed_count,
      failed_count = s.failed_count,
      status = case
        when s.running_count > 0 then 'running'
        when s.failed_count > 0 and s.completed_count > 0 then 'partial_failed'
        when s.failed_count > 0 then 'failed'
        else 'completed'
      end,
      completed_at = case when s.running_count = 0 then now() else b.completed_at end
    from (
      select
        batch_id,
        count(*) filter (where status = 'completed') as completed_count,
        count(*) filter (where status = 'failed') as failed_count,
        count(*) filter (where status in ('queued', 'running')) as running_count
      from public.user_price_refresh_batch_items
      where batch_id in (
        select batch_id from public.user_price_refresh_batch_items
        where refresh_job_id = new.id
      )
      group by batch_id
    ) s
    where b.id = s.batch_id;

    insert into public.user_notification_events (user_id, batch_id, type, title, body)
    select
      b.user_id,
      b.id,
      'price_refresh_batch_completed',
      'CardScanR prices updated',
      case
        when b.failed_count > 0 then
          'CardScanR updated ' || b.completed_count::text || ' prices. ' || b.failed_count::text || ' need retry.'
        else
          'CardScanR prices updated for ' || b.completed_count::text || ' cards.'
      end
    from public.user_price_refresh_batches b
    where b.notify_on_complete
      and b.notification_created_at is null
      and b.status in ('completed', 'partial_failed', 'failed')
      and b.id in (
        select batch_id from public.user_price_refresh_batch_items
        where refresh_job_id = new.id
      );

    update public.user_price_refresh_batches
    set notification_created_at = now()
    where notify_on_complete
      and notification_created_at is null
      and status in ('completed', 'partial_failed', 'failed')
      and id in (
        select batch_id from public.user_price_refresh_batch_items
        where refresh_job_id = new.id
      );
  end if;
  return new;
end;
$$;

drop trigger if exists trg_user_price_refresh_batch_job_update
on public.market_price_refresh_jobs;
create trigger trg_user_price_refresh_batch_job_update
after update of status on public.market_price_refresh_jobs
for each row
execute function public.update_user_price_refresh_batch_item_from_job();

grant select on public.user_price_refresh_batches to authenticated;
grant select on public.user_price_refresh_batch_items to authenticated;
grant select, insert, update, delete on public.user_push_tokens to authenticated;
grant select on public.user_notification_events to authenticated;
grant all privileges on public.user_price_refresh_batches to service_role;
grant all privileges on public.user_price_refresh_batch_items to service_role;
grant all privileges on public.user_push_tokens to service_role;
grant all privileges on public.user_notification_events to service_role;
grant execute on function public.enqueue_missing_prices_for_user_collection(uuid, text, text, text, boolean)
  to authenticated, service_role;
grant execute on function public.get_user_price_refresh_batch_status(uuid)
  to authenticated, service_role;
grant execute on function public.update_user_price_refresh_batch_item_from_job()
  to service_role;
