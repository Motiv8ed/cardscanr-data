-- CardScanR worldwide Pokémon TCG catalogue and sealed-product foundation.
-- The app consumes immutable published artifacts; these service-role-only tables
-- are the normalized source of truth and are not a public client API.

create table public.franchises (
  id text primary key check (id <> ''),
  name text not null,
  owner_name text,
  created_at timestamptz not null default now()
);

create table public.languages (
  code text primary key check (code <> ''),
  english_name text not null,
  native_name text,
  script_code text,
  officially_printed boolean not null default false,
  first_official_release_date date,
  last_official_release_date date,
  aliases text[] not null default '{}',
  notes text,
  created_at timestamptz not null default now()
);

create table public.regions (
  code text primary key check (code <> ''),
  name text not null,
  territory_codes text[] not null default '{}',
  default_currency text check (default_currency is null or default_currency ~ '^[A-Z]{3}$'),
  notes text,
  created_at timestamptz not null default now()
);

create table public.source_providers (
  id text primary key check (id <> ''),
  name text not null,
  provider_type text not null check (provider_type in ('official','open_dataset','community','retailer','marketplace','archive','internal')),
  base_url text,
  rights_status text not null default 'unknown' check (rights_status in ('approved_for_mirror','metadata_only','link_only','permission_pending','restricted','public_domain','unknown')),
  attribution_text text,
  terms_url text,
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.import_runs (
  id uuid primary key default gen_random_uuid(),
  provider_id text references public.source_providers(id),
  collector_name text not null,
  collector_version text,
  status text not null check (status in ('running','completed','failed','cancelled','partial')),
  checkpoint jsonb not null default '{}'::jsonb,
  counters jsonb not null default '{}'::jsonb,
  started_at timestamptz not null default now(),
  completed_at timestamptz,
  error_summary text,
  check (completed_at is null or completed_at >= started_at)
);

create table public.source_snapshots (
  id uuid primary key default gen_random_uuid(),
  provider_id text not null references public.source_providers(id),
  import_run_id uuid references public.import_runs(id),
  source_url text not null,
  fetched_at timestamptz not null,
  http_status integer,
  content_type text,
  byte_size bigint check (byte_size is null or byte_size >= 0),
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  storage_uri text not null,
  response_headers jsonb not null default '{}'::jsonb,
  source_version text,
  unique (provider_id, sha256)
);

create table public.source_records (
  id uuid primary key default gen_random_uuid(),
  provider_id text not null references public.source_providers(id),
  snapshot_id uuid not null references public.source_snapshots(id),
  import_run_id uuid references public.import_runs(id),
  provider_record_type text not null,
  provider_record_id text not null,
  provider_parent_id text,
  raw_payload jsonb,
  raw_payload_uri text,
  raw_sha256 text not null check (raw_sha256 ~ '^[0-9a-f]{64}$'),
  observed_at timestamptz not null,
  created_at timestamptz not null default now(),
  check (raw_payload is not null or raw_payload_uri is not null),
  unique (provider_id, provider_record_type, provider_record_id, raw_sha256)
);

create table public.eras (
  id text primary key check (id <> ''),
  franchise_id text not null references public.franchises(id),
  name text not null,
  starts_on date,
  ends_on date,
  sequence integer,
  unique (franchise_id, name)
);

create table public.series (
  id text primary key check (id <> ''),
  franchise_id text not null references public.franchises(id),
  source_record_id uuid references public.source_records(id),
  era_id text references public.eras(id),
  parent_series_id text references public.series(id),
  name text not null,
  local_names jsonb not null default '{}'::jsonb,
  starts_on date,
  ends_on date,
  series_kind text not null default 'expansion' check (series_kind in ('expansion','promotion','deck','tournament','historical','other'))
);

create table public.sets (
  id text primary key check (id <> ''),
  franchise_id text not null references public.franchises(id),
  source_record_id uuid references public.source_records(id),
  series_id text references public.series(id),
  canonical_name text not null,
  set_kind text not null check (set_kind in ('main','special','promo','deck','tournament','prize','retailer','vending','historical','other')),
  symbol_uri text,
  logo_uri text,
  verification_status text not null default 'provisional' check (verification_status in ('verified','corroborated','provisional','disputed')),
  created_at timestamptz not null default now()
);

create table public.set_releases (
  id text primary key check (id <> ''),
  set_id text not null references public.sets(id),
  source_record_id uuid references public.source_records(id),
  language_code text not null references public.languages(code),
  region_code text not null references public.regions(code),
  local_name text not null,
  translated_name text,
  release_code text,
  release_date date,
  printed_total integer check (printed_total is null or printed_total >= 0),
  official_total integer check (official_total is null or official_total >= 0),
  regulation_marks text[] not null default '{}',
  expected_printing_count integer check (expected_printing_count is null or expected_printing_count >= 0),
  verification_status text not null default 'provisional' check (verification_status in ('verified','corroborated','provisional','disputed')),
  unique (set_id, language_code, region_code, release_code)
);

create table public.card_designs (
  id text primary key check (id <> ''),
  franchise_id text not null references public.franchises(id),
  design_kind text not null default 'card' check (design_kind in ('pokemon','trainer','energy','marker','other')),
  national_pokedex_numbers integer[] not null default '{}',
  canonical_name text,
  artwork_key text,
  rules_identity_key text,
  created_at timestamptz not null default now()
);

create table public.card_printings (
  id text primary key check (id <> ''),
  card_design_id text not null references public.card_designs(id),
  set_release_id text not null references public.set_releases(id),
  source_record_id uuid references public.source_records(id),
  collector_number text not null,
  printed_collector_number text,
  printed_total integer check (printed_total is null or printed_total >= 0),
  local_printing_key text not null,
  card_back_key text,
  edition text,
  copyright_line text,
  regulation_mark text,
  release_date date,
  rarity text,
  illustrator text,
  supertype text,
  subtypes text[] not null default '{}',
  stage text,
  evolves_from_printing_id text references public.card_printings(id),
  hp integer check (hp is null or hp >= 0),
  types text[] not null default '{}',
  rules text[] not null default '{}',
  weaknesses jsonb not null default '[]'::jsonb,
  resistances jsonb not null default '[]'::jsonb,
  retreat_cost text[] not null default '{}',
  legality jsonb not null default '{}'::jsonb,
  promotional_source text,
  product_origin text,
  verification_status text not null default 'provisional' check (verification_status in ('verified','corroborated','provisional','disputed','quarantined')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (set_release_id, local_printing_key),
  unique (set_release_id, collector_number, card_design_id, edition, copyright_line)
);

create table public.card_variants (
  id text primary key check (id <> ''),
  card_printing_id text not null references public.card_printings(id),
  variant_key text not null,
  finish text,
  foil_pattern text,
  edition text,
  stamp text,
  deck_exclusive boolean not null default false,
  error_kind text,
  correction_kind text,
  oversized boolean not null default false,
  attributes jsonb not null default '{}'::jsonb,
  recognition_status text not null default 'recognized' check (recognition_status in ('recognized','reported','disputed','unknown')),
  unique (card_printing_id, variant_key)
);

create table public.card_text_localisations (
  id uuid primary key default gen_random_uuid(),
  card_printing_id text not null references public.card_printings(id),
  language_code text not null references public.languages(code),
  name text not null,
  translated_name text,
  flavor_text text,
  rules text[] not null default '{}',
  translation_status text not null default 'source' check (translation_status in ('official','source','community','machine','unknown')),
  unique (card_printing_id, language_code)
);

create table public.abilities (
  id uuid primary key default gen_random_uuid(),
  card_printing_id text not null references public.card_printings(id),
  ordinal integer not null check (ordinal >= 0),
  ability_type text,
  name text,
  text text not null,
  unique (card_printing_id, ordinal)
);

create table public.attacks (
  id uuid primary key default gen_random_uuid(),
  card_printing_id text not null references public.card_printings(id),
  ordinal integer not null check (ordinal >= 0),
  name text not null,
  cost text[] not null default '{}',
  converted_energy_cost integer check (converted_energy_cost is null or converted_energy_cost >= 0),
  damage text,
  text text,
  unique (card_printing_id, ordinal)
);

create table public.card_images (
  id uuid primary key default gen_random_uuid(),
  card_variant_id text not null references public.card_variants(id),
  source_record_id uuid references public.source_records(id),
  source_provider_id text not null references public.source_providers(id),
  image_role text not null check (image_role in ('front','back','detail','display','thumbnail')),
  source_url text not null,
  source_rights_status text not null,
  fetched_at timestamptz,
  original_sha256 text check (original_sha256 is null or original_sha256 ~ '^[0-9a-f]{64}$'),
  content_sha256 text check (content_sha256 is null or content_sha256 ~ '^[0-9a-f]{64}$'),
  perceptual_hash text,
  width integer check (width is null or width > 0),
  height integer check (height is null or height > 0),
  byte_size bigint check (byte_size is null or byte_size >= 0),
  mime_type text,
  original_object_key text,
  display_object_key text,
  thumbnail_object_key text,
  validation_status text not null default 'candidate' check (validation_status in (
    'candidate','verified','acquired','published','acquired_transient','invalid','blocked','missing'
  )),
  language_verified boolean,
  region_verified boolean,
  identity_verified boolean,
  unique (card_variant_id, image_role, source_provider_id, source_url)
);

create table public.sealed_products (
  id text primary key check (id <> ''),
  franchise_id text not null references public.franchises(id),
  source_record_id uuid references public.source_records(id),
  canonical_name text not null,
  translated_name text,
  product_type text not null,
  description text,
  verification_status text not null default 'provisional' check (verification_status in ('verified','corroborated','provisional','disputed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.accessories (
  id text primary key check (id <> ''),
  franchise_id text not null references public.franchises(id),
  source_record_id uuid references public.source_records(id),
  canonical_name text not null,
  accessory_type text not null check (accessory_type in ('binder','album','sleeves','deck_box','playmat','coin','dice','storage','marker','other')),
  description text,
  verification_status text not null default 'provisional'
);

create table public.sealed_product_variants (
  id text primary key check (id <> ''),
  sealed_product_id text not null references public.sealed_products(id),
  language_code text references public.languages(code),
  region_code text not null references public.regions(code),
  local_name text not null,
  variant_key text not null,
  release_date date,
  msrp numeric(14,2) check (msrp is null or msrp >= 0),
  currency text check (currency is null or currency ~ '^[A-Z]{3}$'),
  upc text,
  ean text,
  jan text,
  gtin text,
  sku text,
  packs_included integer check (packs_included is null or packs_included >= 0),
  cards_per_pack integer check (cards_per_pack is null or cards_per_pack >= 0),
  dimensions jsonb,
  weight_grams numeric(14,3) check (weight_grams is null or weight_grams >= 0),
  box_art_key text,
  pack_art_key text,
  contents_description text,
  attributes jsonb not null default '{}'::jsonb,
  unique (sealed_product_id, region_code, language_code, variant_key)
);

create table public.product_contents (
  id uuid primary key default gen_random_uuid(),
  sealed_product_variant_id text not null references public.sealed_product_variants(id),
  ordinal integer not null check (ordinal >= 0),
  content_kind text not null check (content_kind in (
    'set_release','card_printing','card_variant','sealed_product_variant','accessory','pack',
    'booster_pack','promotional_card','constructed_deck','digital_code','card','other'
  )),
  set_release_id text references public.set_releases(id),
  card_printing_id text references public.card_printings(id),
  card_variant_id text references public.card_variants(id),
  nested_product_variant_id text references public.sealed_product_variants(id),
  accessory_id text references public.accessories(id),
  description text,
  quantity integer not null default 1 check (quantity > 0),
  attributes jsonb not null default '{}'::jsonb,
  unique (sealed_product_variant_id, ordinal)
);

create table public.product_images (
  id uuid primary key default gen_random_uuid(),
  sealed_product_variant_id text not null references public.sealed_product_variants(id),
  source_record_id uuid references public.source_records(id),
  source_provider_id text not null references public.source_providers(id),
  image_role text not null check (image_role in (
    'front','back','side','contents','pack_art','box_art','listing','display','thumbnail'
  )),
  source_url text not null,
  source_rights_status text not null,
  fetched_at timestamptz,
  content_sha256 text check (content_sha256 is null or content_sha256 ~ '^[0-9a-f]{64}$'),
  perceptual_hash text,
  width integer check (width is null or width > 0),
  height integer check (height is null or height > 0),
  byte_size bigint check (byte_size is null or byte_size >= 0),
  mime_type text,
  original_object_key text,
  display_object_key text,
  thumbnail_object_key text,
  validation_status text not null default 'candidate' check (validation_status in (
    'candidate','verified','acquired','published','acquired_transient','invalid','blocked','missing'
  )),
  unique (sealed_product_variant_id, image_role, source_provider_id, source_url)
);

create table public.marketplace_mappings (
  id uuid primary key default gen_random_uuid(),
  entity_type text not null check (entity_type in ('card_printing','card_variant','sealed_product','sealed_product_variant','accessory','set_release')),
  entity_id text not null,
  marketplace text not null,
  marketplace_id text not null,
  mapping_status text not null default 'candidate' check (mapping_status in ('verified','candidate','rejected','stale')),
  source_record_id uuid references public.source_records(id),
  unique (entity_type, entity_id, marketplace, marketplace_id)
);

create table public.image_validation_results (
  id uuid primary key default gen_random_uuid(),
  card_image_id uuid references public.card_images(id),
  product_image_id uuid references public.product_images(id),
  validator text not null,
  validator_version text,
  status text not null check (status in ('pass','fail','warning','not_applicable')),
  checks jsonb not null,
  checked_at timestamptz not null default now(),
  check ((card_image_id is not null)::integer + (product_image_id is not null)::integer = 1)
);

create table public.image_acquisition_attempts (
  id uuid primary key default gen_random_uuid(),
  entity_type text not null check (entity_type in ('card_variant','sealed_product_variant')),
  entity_id text not null,
  provider_id text not null references public.source_providers(id),
  source_url text,
  attempted_at timestamptz not null default now(),
  http_status integer,
  outcome text not null check (outcome in ('acquired','not_found','blocked','mismatch','invalid','retryable_error','rights_blocked')),
  evidence jsonb not null default '{}'::jsonb
);

create table public.record_provenance (
  id uuid primary key default gen_random_uuid(),
  entity_type text not null,
  entity_id text not null,
  field_name text not null,
  source_record_id uuid not null references public.source_records(id),
  source_value jsonb,
  confidence numeric(5,4) check (confidence is null or confidence between 0 and 1),
  verification_status text not null default 'observed' check (verification_status in ('observed','corroborated','verified','disputed','superseded')),
  created_at timestamptz not null default now(),
  unique (entity_type, entity_id, field_name, source_record_id)
);

create table public.publication_runs (
  id uuid primary key default gen_random_uuid(),
  version text not null unique,
  status text not null check (status in ('building','canary','verified','active','rolled_back','failed')),
  catalogue_sha256 text check (catalogue_sha256 is null or catalogue_sha256 ~ '^[0-9a-f]{64}$'),
  manifest_sha256 text check (manifest_sha256 is null or manifest_sha256 ~ '^[0-9a-f]{64}$'),
  object_prefix text not null,
  previous_publication_id uuid references public.publication_runs(id),
  counters jsonb not null default '{}'::jsonb,
  gates jsonb not null default '{}'::jsonb,
  started_at timestamptz not null default now(),
  activated_at timestamptz,
  completed_at timestamptz,
  rollback_retained boolean not null default true
);

create table public.publication_artifacts (
  id uuid primary key default gen_random_uuid(),
  publication_run_id uuid not null references public.publication_runs(id),
  artifact_type text not null,
  object_key text not null,
  public_url text,
  byte_size bigint not null check (byte_size >= 0),
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  verified_at timestamptz,
  unique (publication_run_id, object_key)
);

create table public.unresolved_items (
  id uuid primary key default gen_random_uuid(),
  entity_type text not null,
  entity_id text,
  language_code text references public.languages(code),
  region_code text references public.regions(code),
  issue_class text not null,
  summary text not null,
  evidence jsonb not null default '{}'::jsonb,
  attempted_providers text[] not null default '{}',
  status text not null default 'open' check (status in (
    'open','blocked_external','needs_review','resolved','wont_fix','classified_nonblocking','documented_exhausted'
  )),
  externally_unavoidable boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  resolved_at timestamptz
);

create index source_records_provider_lookup_idx on public.source_records(provider_id, provider_record_type, provider_record_id);
create index import_runs_provider_idx on public.import_runs(provider_id);
create index source_snapshots_import_run_idx on public.source_snapshots(import_run_id);
create index source_records_snapshot_idx on public.source_records(snapshot_id);
create index source_records_import_run_idx on public.source_records(import_run_id);
create index eras_franchise_idx on public.eras(franchise_id);
create index series_franchise_idx on public.series(franchise_id);
create index series_source_record_idx on public.series(source_record_id);
create index series_era_idx on public.series(era_id);
create index series_parent_idx on public.series(parent_series_id);
create index sets_franchise_idx on public.sets(franchise_id);
create index sets_source_record_idx on public.sets(source_record_id);
create index sets_series_idx on public.sets(series_id);
create index set_releases_language_idx on public.set_releases(language_code);
create index set_releases_region_idx on public.set_releases(region_code);
create index set_releases_source_record_idx on public.set_releases(source_record_id);
create index card_designs_franchise_idx on public.card_designs(franchise_id);
create index card_printings_release_collector_idx on public.card_printings(set_release_id, collector_number);
create index card_printings_design_idx on public.card_printings(card_design_id);
create index card_printings_source_record_idx on public.card_printings(source_record_id);
create index card_printings_evolves_from_idx on public.card_printings(evolves_from_printing_id);
create index card_variants_printing_idx on public.card_variants(card_printing_id);
create index card_images_source_url_idx on public.card_images(source_url);
create index card_text_localisations_language_idx on public.card_text_localisations(language_code);
create index card_images_variant_status_idx on public.card_images(card_variant_id, validation_status);
create index card_images_source_record_idx on public.card_images(source_record_id);
create index card_images_source_provider_idx on public.card_images(source_provider_id);
create index sealed_products_franchise_idx on public.sealed_products(franchise_id);
create index sealed_products_source_record_idx on public.sealed_products(source_record_id);
create index accessories_franchise_idx on public.accessories(franchise_id);
create index accessories_source_record_idx on public.accessories(source_record_id);
create index sealed_product_variants_language_idx on public.sealed_product_variants(language_code);
create index sealed_product_variants_region_language_idx on public.sealed_product_variants(region_code, language_code);
create index product_contents_variant_idx on public.product_contents(sealed_product_variant_id);
create index product_contents_set_release_idx on public.product_contents(set_release_id);
create index product_contents_card_printing_idx on public.product_contents(card_printing_id);
create index product_contents_card_variant_idx on public.product_contents(card_variant_id);
create index product_contents_nested_variant_idx on public.product_contents(nested_product_variant_id);
create index product_contents_accessory_idx on public.product_contents(accessory_id);
create index product_images_source_record_idx on public.product_images(source_record_id);
create index product_images_source_provider_idx on public.product_images(source_provider_id);
create index marketplace_mappings_source_record_idx on public.marketplace_mappings(source_record_id);
create index image_validation_card_image_idx on public.image_validation_results(card_image_id);
create index image_validation_product_image_idx on public.image_validation_results(product_image_id);
create index acquisition_attempt_provider_idx on public.image_acquisition_attempts(provider_id);
create index provenance_entity_idx on public.record_provenance(entity_type, entity_id, field_name);
create index provenance_source_record_idx on public.record_provenance(source_record_id);
create index publication_runs_previous_idx on public.publication_runs(previous_publication_id);
create index unresolved_status_idx on public.unresolved_items(status, issue_class, language_code, region_code);
create index unresolved_language_idx on public.unresolved_items(language_code);
create index unresolved_region_idx on public.unresolved_items(region_code);
create index acquisition_attempt_entity_idx on public.image_acquisition_attempts(entity_type, entity_id, attempted_at desc);

do $security$
declare
  table_name text;
begin
  foreach table_name in array array[
    'franchises','languages','regions','source_providers','import_runs','source_snapshots','source_records',
    'eras','series','sets','set_releases','card_designs','card_printings','card_variants',
    'card_text_localisations','abilities','attacks','card_images','sealed_products','accessories',
    'sealed_product_variants','product_contents','product_images','marketplace_mappings',
    'image_validation_results','image_acquisition_attempts','record_provenance','publication_runs',
    'publication_artifacts','unresolved_items'
  ] loop
    execute format('alter table public.%I enable row level security', table_name);
    execute format('revoke all on table public.%I from public, anon, authenticated', table_name);
    execute format('grant select, insert, update, delete on table public.%I to service_role', table_name);
  end loop;
end
$security$;

revoke all on all sequences in schema public from public, anon, authenticated;
grant usage, select on all sequences in schema public to service_role;
