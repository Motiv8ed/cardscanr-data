-- Build 46: CardScanR cloud image manifests (R2 + CDN metadata).
-- Image binaries stay in Cloudflare R2; Postgres stores manifests only.
-- Prefer this table over extending pokemon_card_image_records for CDN linkage.
-- Existing pokemon_card_image_records remains for the legacy ingestion pipeline.

create table if not exists public.card_image_manifests (
  id uuid primary key default gen_random_uuid(),
  card_id text not null,
  set_id text not null,
  language text not null,
  variant text not null default 'standard',
  source_provider text,
  source_url text,
  source_license_or_terms text,
  rights_status text not null default 'unknown',
  source_card_identifier text,
  source_sha256 text,
  r2_bucket text,
  r2_original_key text,
  r2_display_key text,
  r2_thumbnail_key text,
  public_display_url text,
  public_thumbnail_url text,
  content_sha256 text not null,
  width integer,
  height integer,
  byte_size bigint,
  mime_type text,
  verification_status text not null default 'pending',
  verification_reason text,
  is_current boolean not null default false,
  verified_at timestamptz,
  uploaded_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint card_image_manifests_language_lowercase
    check (language = lower(language)),
  constraint card_image_manifests_variant_lowercase
    check (variant = lower(variant)),
  constraint card_image_manifests_rights_status_valid
    check (
      rights_status in (
        'approved_for_mirror',
        'approved_for_link_only',
        'permission_pending',
        'unknown',
        'blocked'
      )
    ),
  constraint card_image_manifests_verification_status_valid
    check (
      verification_status in (
        'pending',
        'verified',
        'failed',
        'rejected',
        'superseded'
      )
    ),
  constraint card_image_manifests_byte_size_non_negative
    check (byte_size is null or byte_size >= 0),
  constraint card_image_manifests_dimensions_positive
    check (
      (width is null or width > 0)
      and (height is null or height > 0)
    ),
  constraint card_image_manifests_identity_hash_unique
    unique (card_id, set_id, language, variant, content_sha256)
);

create index if not exists card_image_manifests_card_id_idx
  on public.card_image_manifests (card_id);

create index if not exists card_image_manifests_set_id_idx
  on public.card_image_manifests (set_id);

create index if not exists card_image_manifests_language_idx
  on public.card_image_manifests (language);

create index if not exists card_image_manifests_verification_status_idx
  on public.card_image_manifests (verification_status);

create index if not exists card_image_manifests_is_current_idx
  on public.card_image_manifests (is_current);

create index if not exists card_image_manifests_current_verified_idx
  on public.card_image_manifests (card_id, set_id, language, variant)
  where is_current = true and verification_status = 'verified';

create index if not exists card_image_manifests_missing_coverage_idx
  on public.card_image_manifests (language, set_id, verification_status, is_current);

create index if not exists card_image_manifests_rights_status_idx
  on public.card_image_manifests (rights_status);

drop trigger if exists card_image_manifests_set_updated_at
  on public.card_image_manifests;
create trigger card_image_manifests_set_updated_at
  before update on public.card_image_manifests
  for each row execute function public.set_updated_at();

-- Current verified images for app reads.
create or replace view public.card_image_manifests_current as
select
  m.*
from public.card_image_manifests m
where m.is_current = true
  and m.verification_status = 'verified';

comment on view public.card_image_manifests_current is
  'Build 46: current verified CardScanR CDN image manifests for app reads.';

-- Optional join helper for legacy ingestion records.
create or replace view public.card_image_manifests_with_legacy_records as
select
  m.*,
  r.id as legacy_record_id,
  r.status as legacy_pipeline_status,
  r.content_hash_sha256 as legacy_content_hash,
  r.thumb_storage_path as legacy_thumb_storage_path,
  r.display_storage_path as legacy_display_storage_path
from public.card_image_manifests m
left join public.pokemon_card_image_records r
  on r.canonical_base_id = m.source_card_identifier
  or (
    r.set_id = m.set_id
    and r.language = m.language
    and (
      r.provider_card_id = m.card_id
      or r.canonical_base_id = m.card_id
    )
  );

grant select, insert, update, delete on public.card_image_manifests to service_role;
grant select on public.card_image_manifests to anon, authenticated;
grant select on public.card_image_manifests_current to anon, authenticated, service_role;
grant select on public.card_image_manifests_with_legacy_records to anon, authenticated, service_role;

alter table public.card_image_manifests enable row level security;

drop policy if exists card_image_manifests_public_read_current_verified
  on public.card_image_manifests;
create policy card_image_manifests_public_read_current_verified
  on public.card_image_manifests
  for select
  to anon, authenticated
  using (is_current = true and verification_status = 'verified');

-- Explicitly deny writes for anon/authenticated (service_role bypasses RLS).
drop policy if exists card_image_manifests_no_insert_anon_auth
  on public.card_image_manifests;
create policy card_image_manifests_no_insert_anon_auth
  on public.card_image_manifests
  for insert
  to anon, authenticated
  with check (false);

drop policy if exists card_image_manifests_no_update_anon_auth
  on public.card_image_manifests;
create policy card_image_manifests_no_update_anon_auth
  on public.card_image_manifests
  for update
  to anon, authenticated
  using (false)
  with check (false);

drop policy if exists card_image_manifests_no_delete_anon_auth
  on public.card_image_manifests;
create policy card_image_manifests_no_delete_anon_auth
  on public.card_image_manifests
  for delete
  to anon, authenticated
  using (false);
