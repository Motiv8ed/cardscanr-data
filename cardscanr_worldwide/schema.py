"""SQLite staging schema for worldwide catalogue acquisition and reconciliation."""

from __future__ import annotations

import sqlite3

SCHEMA_SQL = r"""
pragma foreign_keys = on;

create table if not exists catalogue_meta (
  key text primary key,
  value text not null
);

create table if not exists source_provider (
  id text primary key,
  name text not null,
  provider_type text not null,
  base_url text,
  rights_status text not null,
  attribution_text text,
  terms_url text,
  source_version text
);

create table if not exists import_run (
  id text primary key,
  provider_id text not null references source_provider(id),
  status text not null,
  input_uri text not null,
  input_sha256 text,
  checkpoint_json text not null default '{}',
  counters_json text not null default '{}',
  started_at text not null,
  completed_at text,
  error_summary text
);

create table if not exists source_snapshot (
  id text primary key,
  provider_id text not null references source_provider(id),
  import_run_id text not null references import_run(id),
  source_uri text not null,
  source_sha256 text not null,
  source_version text,
  byte_size integer not null,
  fetched_at text not null,
  storage_uri text not null,
  unique(provider_id, source_sha256)
);

create table if not exists source_record (
  id text primary key,
  provider_id text not null references source_provider(id),
  import_run_id text not null references import_run(id),
  snapshot_id text not null references source_snapshot(id),
  record_type text not null,
  provider_record_id text not null,
  provider_parent_id text,
  source_path text not null,
  source_sha256 text not null,
  raw_payload_json text,
  error text,
  unique(provider_id, provider_record_id, source_sha256)
);

create table if not exists series (
  id text primary key,
  provider_id text not null references source_provider(id),
  provider_record_id text not null,
  source_record_id text not null references source_record(id),
  canonical_name text not null,
  local_names_json text not null,
  unique(provider_id, provider_record_id)
);

create table if not exists card_set (
  id text primary key,
  series_id text references series(id),
  provider_id text not null references source_provider(id),
  provider_record_id text not null,
  source_record_id text not null references source_record(id),
  canonical_name text not null,
  local_names_json text not null,
  set_kind text not null default 'other',
  official_count integer,
  release_dates_json text not null,
  third_party_json text not null,
  unique(provider_id, provider_record_id)
);

create table if not exists set_release (
  id text primary key,
  card_set_id text not null references card_set(id),
  language_code text not null,
  region_code text not null,
  local_name text not null,
  release_code text,
  release_date text,
  official_count integer,
  verification_status text not null,
  source_record_id text not null references source_record(id),
  unique(card_set_id, language_code, region_code)
);

create table if not exists card_design (
  id text primary key,
  design_kind text not null,
  canonical_name text,
  national_pokedex_numbers_json text not null,
  source_identity_key text not null unique
);

create table if not exists card_printing (
  id text primary key,
  card_design_id text not null references card_design(id),
  set_release_id text not null references set_release(id),
  source_record_id text not null references source_record(id),
  collector_number text not null,
  local_printing_key text not null,
  rarity text,
  illustrator text,
  supertype text,
  stage text,
  hp integer,
  types_json text not null,
  regulation_mark text,
  retreat_json text not null,
  weaknesses_json text not null,
  resistances_json text not null,
  verification_status text not null,
  raw_card_json text not null,
  unique(set_release_id, local_printing_key)
);

create table if not exists card_variant (
  id text primary key,
  card_printing_id text not null references card_printing(id),
  variant_key text not null,
  finish text,
  foil_pattern text,
  subtype text,
  stamp text,
  oversized integer not null default 0,
  attributes_json text not null,
  recognition_status text not null,
  unique(card_printing_id, variant_key)
);

create table if not exists card_localisation (
  card_printing_id text not null references card_printing(id),
  language_code text not null,
  name text not null,
  flavor_text text,
  rules_json text not null,
  translation_status text not null,
  primary key(card_printing_id, language_code)
);

create table if not exists attack (
  card_printing_id text not null references card_printing(id),
  ordinal integer not null,
  language_code text not null,
  name text not null,
  cost_json text not null,
  damage text,
  effect text,
  primary key(card_printing_id, ordinal, language_code)
);

create table if not exists ability (
  card_printing_id text not null references card_printing(id),
  ordinal integer not null,
  language_code text not null,
  ability_type text,
  name text,
  effect text not null,
  primary key(card_printing_id, ordinal, language_code)
);

create table if not exists marketplace_mapping (
  entity_type text not null,
  entity_id text not null,
  marketplace text not null,
  marketplace_id text not null,
  source_record_id text not null references source_record(id),
  mapping_status text not null,
  primary key(entity_type, entity_id, marketplace, marketplace_id)
);

create table if not exists provider_entity_mapping (
  provider_id text not null references source_provider(id),
  provider_record_type text not null,
  provider_record_id text not null,
  entity_type text not null,
  entity_id text not null,
  match_method text not null,
  mapping_status text not null,
  source_record_id text not null references source_record(id),
  evidence_json text not null,
  primary key(provider_id, provider_record_type, provider_record_id, entity_type, entity_id)
);

create table if not exists card_image_candidate (
  id text primary key,
  card_variant_id text not null references card_variant(id),
  source_record_id text not null references source_record(id),
  provider_id text not null references source_provider(id),
  image_role text not null,
  source_url text not null,
  rights_status text not null,
  validation_status text not null,
  unique(card_variant_id, image_role, provider_id, source_url)
);

create table if not exists sealed_product (
  id text primary key,
  provider_id text not null references source_provider(id),
  provider_record_id text not null,
  source_record_id text not null references source_record(id),
  canonical_name text not null,
  product_type text not null,
  verification_status text not null,
  raw_product_json text not null,
  unique(provider_id, provider_record_id)
);

create table if not exists sealed_product_variant (
  id text primary key,
  sealed_product_id text not null references sealed_product(id),
  language_code text,
  region_code text not null,
  local_name text not null,
  variant_key text not null,
  release_date text,
  attributes_json text not null,
  unique(sealed_product_id, region_code, language_code, variant_key)
);

create table if not exists product_content (
  sealed_product_variant_id text not null references sealed_product_variant(id),
  ordinal integer not null,
  content_kind text not null,
  entity_id text,
  description text,
  quantity integer not null,
  attributes_json text not null,
  primary key(sealed_product_variant_id, ordinal)
);

create table if not exists product_image_candidate (
  id text primary key,
  sealed_product_variant_id text not null references sealed_product_variant(id),
  source_record_id text not null references source_record(id),
  provider_id text not null references source_provider(id),
  image_role text not null,
  source_url text not null,
  rights_status text not null,
  validation_status text not null,
  attributes_json text not null,
  unique(sealed_product_variant_id, image_role, provider_id, source_url)
);

create table if not exists accessory (
  id text primary key,
  provider_id text not null references source_provider(id),
  provider_record_id text not null,
  source_record_id text not null references source_record(id),
  canonical_name text not null,
  accessory_type text not null,
  description text,
  verification_status text not null,
  unique(provider_id, provider_record_id)
);

create table if not exists unresolved_item (
  id text primary key,
  entity_type text not null,
  entity_id text,
  language_code text,
  region_code text,
  issue_class text not null,
  summary text not null,
  evidence_json text not null,
  status text not null,
  externally_unavoidable integer not null default 0,
  unique(entity_type, entity_id, language_code, issue_class)
);

create index if not exists source_record_lookup_idx
  on source_record(provider_id, record_type, provider_record_id);
create index if not exists source_snapshot_run_idx on source_snapshot(import_run_id);
create index if not exists set_release_language_idx on set_release(language_code);
create index if not exists set_release_region_idx on set_release(region_code);
create index if not exists printing_release_number_idx
  on card_printing(set_release_id, collector_number);
create index if not exists variant_printing_idx on card_variant(card_printing_id);
create index if not exists provider_mapping_entity_idx on provider_entity_mapping(entity_type, entity_id);
create index if not exists image_candidate_variant_idx on card_image_candidate(card_variant_id, validation_status);
create index if not exists image_candidate_source_url_idx on card_image_candidate(source_url);
create index if not exists product_content_entity_idx on product_content(content_kind, entity_id);
create index if not exists product_image_candidate_variant_idx
  on product_image_candidate(sealed_product_variant_id, validation_status);
create index if not exists unresolved_status_idx on unresolved_item(status, issue_class);
"""


def connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("pragma journal_mode = wal")
    connection.execute("pragma synchronous = normal")
    connection.execute("pragma foreign_keys = on")
    connection.executescript(SCHEMA_SQL)
    return connection
