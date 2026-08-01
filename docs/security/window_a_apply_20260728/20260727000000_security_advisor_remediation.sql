-- CardScanR Supabase Security Advisor remediation (least privilege).
-- Project target: qstcdlczasmvexpgbpjk
--
-- Intent:
--   1) Make public catalogue image views security_invoker and least-privilege.
--   2) Remove client access to the legacy admin join view.
--   3) Re-harden SECURITY DEFINER function EXECUTE grants after signature
--      recreates (JP identity payload) restored PUBLIC defaults.
--   4) Stop bucket-wide listing on pokemon-card-images while keeping public
--      object URL retrieval for the public bucket.
--
-- Non-goals:
--   - No row data deletes/updates.
--   - No Auth provider / password changes (leaked-password protection is an
--     owner dashboard step; see docs/security/LEAKED_PASSWORD_PROTECTION.md).
--   - Do not convert privileged pricing/trigger helpers to SECURITY INVOKER.
--
-- Rollback notes (manual):
--   - Recreate views without security_invoker / with prior column sets from
--     20260724000000_card_image_manifests_build46.sql (+ Build 47 quality col).
--   - Re-grant SELECT on legacy view to anon/authenticated if deliberately
--     required (not recommended).
--   - Re-create storage policy pokemon_card_images_public_read if listing is
--     required again.
--   - Function grants can be restored selectively per role.

-- ---------------------------------------------------------------------------
-- Build 47 column may already exist live; keep migration idempotent.
-- ---------------------------------------------------------------------------
alter table public.card_image_manifests
  add column if not exists quality_classification text;

comment on column public.card_image_manifests.quality_classification is
  'Build 47 quality class: highResolution|standardResolution|lowResolutionAccepted|linkOnly|rightsPending|missingProvider|authRequired|permanentFailure|unsupported';

-- ---------------------------------------------------------------------------
-- Views: security_invoker + least privilege
-- ---------------------------------------------------------------------------

-- Drop/recreate so column projection can shrink safely.
drop view if exists public.card_image_manifests_current;

-- Public/authenticated catalogue reads for the Flutter app
-- (SupabaseCardImageManifestLookup -> card_image_manifests_current).
-- Expose only client-required columns; keep current+verified filter.
-- security_invoker ensures base-table RLS applies as the querying role.
create view public.card_image_manifests_current
with (security_invoker = true)
as
select
  m.id,
  m.card_id,
  m.set_id,
  m.language,
  m.variant,
  m.rights_status,
  m.r2_display_key,
  m.r2_thumbnail_key,
  m.public_display_url,
  m.public_thumbnail_url,
  m.content_sha256,
  m.width,
  m.height,
  m.byte_size,
  m.mime_type,
  m.verification_status,
  m.is_current,
  m.quality_classification
from public.card_image_manifests m
where m.is_current = true
  and m.verification_status = 'verified';

comment on view public.card_image_manifests_current is
  'Current verified CDN image manifests for app reads. security_invoker; no source hashes, original keys, or verification reasons.';

-- Legacy join helper is pipeline/admin only. Keep definition for service_role
-- tooling, but strip client grants and force invoker semantics.
drop view if exists public.card_image_manifests_with_legacy_records;

create view public.card_image_manifests_with_legacy_records
with (security_invoker = true)
as
select
  m.id,
  m.card_id,
  m.set_id,
  m.language,
  m.variant,
  m.source_provider,
  m.source_url,
  m.source_license_or_terms,
  m.rights_status,
  m.source_card_identifier,
  m.source_sha256,
  m.r2_bucket,
  m.r2_original_key,
  m.r2_display_key,
  m.r2_thumbnail_key,
  m.public_display_url,
  m.public_thumbnail_url,
  m.content_sha256,
  m.width,
  m.height,
  m.byte_size,
  m.mime_type,
  m.verification_status,
  m.verification_reason,
  m.is_current,
  m.verified_at,
  m.uploaded_at,
  m.created_at,
  m.updated_at,
  m.quality_classification,
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

comment on view public.card_image_manifests_with_legacy_records is
  'Admin/pipeline join of manifests to legacy pokemon_card_image_records. Not client-facing; service_role SELECT only.';

-- Base table + view grants: SELECT only where needed.
revoke all on table public.card_image_manifests from public, anon, authenticated;
grant select on table public.card_image_manifests to anon, authenticated;
grant select, insert, update, delete on table public.card_image_manifests to service_role;

revoke all on table public.card_image_manifests_current from public, anon, authenticated, service_role;
grant select on table public.card_image_manifests_current to anon, authenticated, service_role;

revoke all on table public.card_image_manifests_with_legacy_records from public, anon, authenticated, service_role;
grant select on table public.card_image_manifests_with_legacy_records to service_role;

-- ---------------------------------------------------------------------------
-- SECURITY DEFINER function grants (retain DEFINER; fix search_path + EXECUTE)
-- ---------------------------------------------------------------------------

-- Pricing read RPC used by signed-in Flutter MarketPriceService.
-- AuthGate requires a session, so anon EXECUTE is not required.
alter function public.get_market_price_bundle(text, integer)
  set search_path = public;
revoke all on function public.get_market_price_bundle(text, integer)
  from public, anon, authenticated, service_role;
grant execute on function public.get_market_price_bundle(text, integer)
  to authenticated, service_role;

-- User refresh RPC (cooldown / active-job gate inside function body).
alter function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text, boolean, text, text, jsonb
) set search_path = public;
revoke all on function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text, boolean, text, text, jsonb
) from public, anon, authenticated, service_role;
grant execute on function public.request_market_price_refresh(
  text, text, text, text, text, text, text, text, text, text, text, text, text, boolean, text, text, jsonb
) to authenticated, service_role;

-- Worker/internal key upsert. Also called by request_market_price_refresh as
-- SECURITY DEFINER owner, so clients do not need direct EXECUTE.
alter function public.get_or_create_market_price_key(
  text, text, text, text, text, text, text, text, text, text, text, text, timestamptz, text, text, jsonb
) set search_path = public;
revoke all on function public.get_or_create_market_price_key(
  text, text, text, text, text, text, text, text, text, text, text, text, timestamptz, text, text, jsonb
) from public, anon, authenticated, service_role;
grant execute on function public.get_or_create_market_price_key(
  text, text, text, text, text, text, text, text, text, text, text, text, timestamptz, text, text, jsonb
) to service_role;

-- Auth signup triggers only (on_auth_user_created*). Not RPC-callable.
alter function public.handle_new_user() set search_path = public;
revoke all on function public.handle_new_user()
  from public, anon, authenticated, service_role;

alter function public.handle_new_user_default_collection() set search_path = public;
revoke all on function public.handle_new_user_default_collection()
  from public, anon, authenticated, service_role;

-- Event-trigger helper that enables RLS on new public tables. Internal only.
alter function public.rls_auto_enable() set search_path = pg_catalog;
revoke all on function public.rls_auto_enable()
  from public, anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- Storage: public object URLs do not require a broad SELECT/list policy.
-- Flutter/CDN image retrieval uses public_display_url / public_thumbnail_url
-- (R2/CDN) or direct /object/public/... paths, not bucket listing.
-- ---------------------------------------------------------------------------
drop policy if exists pokemon_card_images_public_read on storage.objects;
