-- CardScanR website feedback submissions (legal/support site).
-- Additive. Does not modify beta_feedback_reports (in-app authenticated path).
-- Public clients have no direct table access; writes only via service_role Edge Function.

create table if not exists public.website_feedback_submissions (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  source text not null default 'legal_site',
  feedback_type text not null,
  subject text not null,
  description text not null,
  card_name text,
  set_name text,
  collector_number text,
  app_version text,
  android_version text,
  device_model text,
  contact_email text,
  reproduction_steps text,
  status text not null default 'new',
  public_reference text not null,
  content_hash text not null,
  submitted_by_user_id uuid references auth.users(id) on delete set null,
  rate_limit_key text,
  constraint website_feedback_source_valid check (
    source in ('legal_site', 'website')
  ),
  constraint website_feedback_type_valid check (
    feedback_type in (
      'bug_crash',
      'feature_suggestion',
      'incorrect_card_match',
      'missing_card_or_set',
      'incorrect_market_price',
      'scanner_ocr',
      'collection_binder',
      'login_account',
      'other'
    )
  ),
  constraint website_feedback_status_valid check (
    status in (
      'new', 'triaged', 'needs_information', 'planned',
      'in_progress', 'resolved', 'closed', 'duplicate', 'test'
    )
  ),
  constraint website_feedback_subject_length check (
    char_length(btrim(subject)) between 4 and 120
  ),
  constraint website_feedback_description_length check (
    char_length(btrim(description)) between 10 and 4000
  ),
  constraint website_feedback_optional_lengths check (
    coalesce(char_length(card_name), 0) <= 160
    and coalesce(char_length(set_name), 0) <= 160
    and coalesce(char_length(collector_number), 0) <= 40
    and coalesce(char_length(app_version), 0) <= 40
    and coalesce(char_length(android_version), 0) <= 40
    and coalesce(char_length(device_model), 0) <= 80
    and coalesce(char_length(contact_email), 0) <= 320
    and coalesce(char_length(reproduction_steps), 0) <= 2000
    and coalesce(char_length(rate_limit_key), 0) <= 128
  ),
  constraint website_feedback_reference_shape check (
    public_reference ~ '^WEB-[A-Z0-9]{6,12}$'
  ),
  constraint website_feedback_content_hash_shape check (
    content_hash ~ '^[a-f0-9]{64}$'
  )
);

comment on table public.website_feedback_submissions is
  'Public website feedback from cardscanr.com. Inserts only via service_role Edge Function after Turnstile validation. No anon/authenticated PostgREST access.';

create unique index if not exists uq_website_feedback_public_reference
  on public.website_feedback_submissions (public_reference);

create unique index if not exists uq_website_feedback_content_hash_day
  on public.website_feedback_submissions (content_hash, ((created_at at time zone 'utc')::date));

create index if not exists idx_website_feedback_created
  on public.website_feedback_submissions (created_at desc);

create index if not exists idx_website_feedback_status
  on public.website_feedback_submissions (status, created_at desc);

create index if not exists idx_website_feedback_type
  on public.website_feedback_submissions (feedback_type, created_at desc);

create table if not exists public.website_feedback_rate_limits (
  bucket_key text primary key,
  hit_count integer not null default 0,
  window_started_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint website_feedback_rate_limits_key_len check (
    char_length(bucket_key) between 8 and 128
  ),
  constraint website_feedback_rate_limits_hits check (
    hit_count >= 0 and hit_count <= 1000000
  )
);

comment on table public.website_feedback_rate_limits is
  'Short-lived HMAC/IP-derived rate-limit buckets for website feedback. Raw IPs are never stored.';

alter table public.website_feedback_submissions enable row level security;
alter table public.website_feedback_rate_limits enable row level security;

revoke all on table public.website_feedback_submissions from anon, authenticated, public;
revoke all on table public.website_feedback_rate_limits from anon, authenticated, public;
grant all on table public.website_feedback_submissions to service_role;
grant all on table public.website_feedback_rate_limits to service_role;

-- No policies for anon/authenticated: PostgREST cannot select/insert/update/delete.

create or replace function public.website_feedback_generate_reference()
returns text
language plpgsql
volatile
security definer
set search_path = public
as $$
declare
  v_alphabet text := 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
  v_candidate text;
  v_attempt integer := 0;
begin
  loop
    v_attempt := v_attempt + 1;
    v_candidate := 'WEB-';
    for i in 1..6 loop
      v_candidate := v_candidate || substr(v_alphabet, 1 + floor(random() * char_length(v_alphabet))::int, 1);
    end loop;
    exit when not exists (
      select 1 from public.website_feedback_submissions where public_reference = v_candidate
    );
    if v_attempt > 20 then
      v_candidate := 'WEB-' || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 8));
      exit;
    end if;
  end loop;
  return v_candidate;
end;
$$;

revoke all on function public.website_feedback_generate_reference() from public, anon, authenticated;
grant execute on function public.website_feedback_generate_reference() to service_role;

create or replace function public.website_feedback_rate_limit_hit(
  p_bucket_key text,
  p_limit integer default 5,
  p_window_seconds integer default 3600
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_row public.website_feedback_rate_limits;
  v_key text := left(trim(coalesce(p_bucket_key, '')), 128);
  v_limit integer := greatest(coalesce(p_limit, 5), 1);
  v_window integer := greatest(coalesce(p_window_seconds, 3600), 60);
begin
  if coalesce(auth.role(), '') <> 'service_role' then
    raise exception 'service_role required' using errcode = '42501';
  end if;
  if v_key is null or char_length(v_key) < 8 then
    return jsonb_build_object('allowed', false, 'code', 'invalid_bucket');
  end if;

  insert into public.website_feedback_rate_limits (bucket_key, hit_count, window_started_at)
  values (v_key, 1, now())
  on conflict (bucket_key) do update
    set
      hit_count = case
        when public.website_feedback_rate_limits.window_started_at
          < now() - make_interval(secs => v_window)
        then 1
        else public.website_feedback_rate_limits.hit_count + 1
      end,
      window_started_at = case
        when public.website_feedback_rate_limits.window_started_at
          < now() - make_interval(secs => v_window)
        then now()
        else public.website_feedback_rate_limits.window_started_at
      end,
      updated_at = now()
  returning * into v_row;

  if v_row.hit_count > v_limit then
    return jsonb_build_object(
      'allowed', false,
      'code', 'rate_limited',
      'hit_count', v_row.hit_count
    );
  end if;

  return jsonb_build_object('allowed', true, 'hit_count', v_row.hit_count);
end;
$$;

revoke all on function public.website_feedback_rate_limit_hit(text, integer, integer) from public, anon, authenticated;
grant execute on function public.website_feedback_rate_limit_hit(text, integer, integer) to service_role;

create or replace function public.website_feedback_submit_internal(p_payload jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  v_type text := lower(trim(coalesce(p_payload ->> 'feedback_type', '')));
  v_subject text := left(trim(coalesce(p_payload ->> 'subject', '')), 120);
  v_description text := left(trim(coalesce(p_payload ->> 'description', '')), 4000);
  v_hash text := lower(trim(coalesce(p_payload ->> 'content_hash', '')));
  v_source text := lower(trim(coalesce(p_payload ->> 'source', 'legal_site')));
  v_reference text;
  v_id uuid;
  v_existing text;
begin
  if coalesce(auth.role(), '') <> 'service_role' then
    raise exception 'service_role required' using errcode = '42501';
  end if;

  if v_source not in ('legal_site', 'website') then
    return jsonb_build_object('ok', false, 'code', 'invalid_source');
  end if;
  if v_type not in (
    'bug_crash', 'feature_suggestion', 'incorrect_card_match', 'missing_card_or_set',
    'incorrect_market_price', 'scanner_ocr', 'collection_binder', 'login_account', 'other'
  ) then
    return jsonb_build_object('ok', false, 'code', 'invalid_type');
  end if;
  if char_length(v_subject) < 4 or char_length(v_description) < 10 then
    return jsonb_build_object('ok', false, 'code', 'validation_failed');
  end if;
  if v_hash !~ '^[a-f0-9]{64}$' then
    return jsonb_build_object('ok', false, 'code', 'invalid_hash');
  end if;

  select public_reference into v_existing
  from public.website_feedback_submissions
  where content_hash = v_hash
    and created_at >= now() - interval '24 hours'
  order by created_at desc
  limit 1;

  if v_existing is not null then
    return jsonb_build_object(
      'ok', true,
      'duplicate', true,
      'public_reference', v_existing
    );
  end if;

  v_reference := public.website_feedback_generate_reference();

  insert into public.website_feedback_submissions (
    source,
    feedback_type,
    subject,
    description,
    card_name,
    set_name,
    collector_number,
    app_version,
    android_version,
    device_model,
    contact_email,
    reproduction_steps,
    status,
    public_reference,
    content_hash,
    submitted_by_user_id,
    rate_limit_key
  ) values (
    v_source,
    v_type,
    v_subject,
    v_description,
    nullif(left(trim(coalesce(p_payload ->> 'card_name', '')), 160), ''),
    nullif(left(trim(coalesce(p_payload ->> 'set_name', '')), 160), ''),
    nullif(left(trim(coalesce(p_payload ->> 'collector_number', '')), 40), ''),
    nullif(left(trim(coalesce(p_payload ->> 'app_version', '')), 40), ''),
    nullif(left(trim(coalesce(p_payload ->> 'android_version', '')), 40), ''),
    nullif(left(trim(coalesce(p_payload ->> 'device_model', '')), 80), ''),
    nullif(left(trim(coalesce(p_payload ->> 'contact_email', '')), 320), ''),
    nullif(left(trim(coalesce(p_payload ->> 'reproduction_steps', '')), 2000), ''),
    coalesce(nullif(trim(p_payload ->> 'status'), ''), 'new'),
    v_reference,
    v_hash,
    nullif(p_payload ->> 'submitted_by_user_id', '')::uuid,
    nullif(left(trim(coalesce(p_payload ->> 'rate_limit_key', '')), 128), '')
  )
  returning id into v_id;

  return jsonb_build_object(
    'ok', true,
    'duplicate', false,
    'id', v_id,
    'public_reference', v_reference
  );
end;
$$;

revoke all on function public.website_feedback_submit_internal(jsonb) from public, anon, authenticated;
grant execute on function public.website_feedback_submit_internal(jsonb) to service_role;
