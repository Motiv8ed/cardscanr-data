-- Pokemon card image ingestion pipeline: metadata records + storage bucket

create table if not exists public.pokemon_card_image_records (
  id uuid primary key default gen_random_uuid(),
  canonical_base_id text not null,
  game text not null default 'pokemon',
  language text not null,
  set_id text not null,
  set_code text,
  collector_number text not null,
  printed_card_number text,
  local_card_number text,
  set_total integer,
  printed_total integer,
  provider_set_id text,
  status text not null default 'pending',
  failure_reason text,
  retry_count integer not null default 0,
  primary_provider text,
  fallback_provider text,
  source_image_url text,
  source_image_url_display text,
  provider_card_id text,
  provider_image_set_id text,
  content_hash_sha256 text,
  thumb_storage_path text,
  display_storage_path text,
  thumb_width integer,
  thumb_height integer,
  display_width integer,
  display_height integer,
  thumb_bytes integer,
  display_bytes integer,
  cache_control text not null default 'public, max-age=31536000, immutable',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  processed_at timestamptz,
  verified_at timestamptz,
  last_attempt_at timestamptz,
  constraint pokemon_card_image_records_canonical_unique unique (canonical_base_id),
  constraint pokemon_card_image_records_game_lowercase check (game = lower(game)),
  constraint pokemon_card_image_records_language_lowercase check (language = lower(language)),
  constraint pokemon_card_image_records_status_valid check (
    status in ('pending', 'processing', 'completed', 'failed', 'skipped', 'verified')
  ),
  constraint pokemon_card_image_records_retry_non_negative check (retry_count >= 0),
  constraint pokemon_card_image_records_thumb_bytes_non_negative check (thumb_bytes is null or thumb_bytes > 0),
  constraint pokemon_card_image_records_display_bytes_non_negative check (display_bytes is null or display_bytes > 0)
);

create index if not exists pokemon_card_image_records_status_idx
  on public.pokemon_card_image_records (status);

create index if not exists pokemon_card_image_records_language_set_idx
  on public.pokemon_card_image_records (language, set_id);

create index if not exists pokemon_card_image_records_hash_idx
  on public.pokemon_card_image_records (content_hash_sha256)
  where content_hash_sha256 is not null;

drop trigger if exists pokemon_card_image_records_set_updated_at on public.pokemon_card_image_records;
create trigger pokemon_card_image_records_set_updated_at
  before update on public.pokemon_card_image_records
  for each row execute function public.set_updated_at();

-- Public read bucket for immutable card images. Writes use service role only.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'pokemon-card-images',
  'pokemon-card-images',
  true,
  10485760,
  array['image/webp']
)
on conflict (id) do update set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

-- Allow anonymous/public read of card images.
drop policy if exists pokemon_card_images_public_read on storage.objects;
create policy pokemon_card_images_public_read
  on storage.objects
  for select
  using (bucket_id = 'pokemon-card-images');
