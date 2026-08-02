-- Remove worldwide Pokémon catalogue infrastructure from Supabase.
-- App-facing catalogue and images are served from CardScanR Cloudflare R2.
-- Canonical build source remains local staging SQLite.
--
-- Safety:
-- * Does not touch auth schema
-- * Does not touch user/collection/customer/market/image-manifest tables
-- * Catalogue tables have no FK references from retained user tables

begin;

-- Temporary MCP migration helper (empty).
drop table if exists public._cardscanr_mcp_sql_chunks cascade;

-- Drop worldwide catalogue foundation tables (dependency-safe via CASCADE).
drop table if exists public.unresolved_items cascade;
drop table if exists public.publication_artifacts cascade;
drop table if exists public.publication_runs cascade;
drop table if exists public.record_provenance cascade;
drop table if exists public.image_acquisition_attempts cascade;
drop table if exists public.image_validation_results cascade;
drop table if exists public.marketplace_mappings cascade;
drop table if exists public.product_images cascade;
drop table if exists public.product_contents cascade;
drop table if exists public.sealed_product_variants cascade;
drop table if exists public.accessories cascade;
drop table if exists public.sealed_products cascade;
drop table if exists public.card_images cascade;
drop table if exists public.attacks cascade;
drop table if exists public.abilities cascade;
drop table if exists public.card_text_localisations cascade;
drop table if exists public.card_variants cascade;
drop table if exists public.card_printings cascade;
drop table if exists public.card_designs cascade;
drop table if exists public.set_releases cascade;
drop table if exists public.sets cascade;
drop table if exists public.series cascade;
drop table if exists public.eras cascade;
drop table if exists public.source_records cascade;
drop table if exists public.source_snapshots cascade;
drop table if exists public.import_runs cascade;
drop table if exists public.source_providers cascade;
drop table if exists public.regions cascade;
drop table if exists public.languages cascade;
drop table if exists public.franchises cascade;

-- Guard: refuse if any retained user table disappeared.
do $$
declare
  missing text;
begin
  select string_agg(required, ', ')
  into missing
  from (
    values
      ('user_profiles'),
      ('user_collections'),
      ('user_cards'),
      ('scan_sessions'),
      ('customer_sync_preferences'),
      ('customer_collection_items'),
      ('customer_binders'),
      ('customer_binder_memberships'),
      ('customer_sync_operations'),
      ('customer_sync_checkpoints'),
      ('pokemon_card_image_records'),
      ('card_image_manifests')
  ) as required(required)
  where to_regclass('public.' || required) is null;

  if missing is not null then
    raise exception 'retained application tables missing after catalogue drop: %', missing;
  end if;
end
$$;

commit;
