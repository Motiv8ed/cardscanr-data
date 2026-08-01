-- Provider-specific identities remain independently addressable until a
-- deterministic reconciliation promotes them to a canonical entity.
create table public.provider_entity_mappings (
  id uuid primary key default gen_random_uuid(),
  provider_id text not null references public.source_providers(id),
  provider_record_type text not null,
  provider_record_id text not null,
  entity_type text not null check (entity_type in (
    'series','set','set_release','card_design','card_printing','card_variant',
    'sealed_product','sealed_product_variant','accessory'
  )),
  entity_id text not null,
  match_method text not null,
  mapping_status text not null check (mapping_status in ('verified','candidate','rejected','stale')),
  source_record_id uuid not null references public.source_records(id),
  evidence jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (provider_id, provider_record_type, provider_record_id, entity_type, entity_id)
);

create index provider_entity_mappings_entity_idx
  on public.provider_entity_mappings(entity_type, entity_id);
create index provider_entity_mappings_source_record_idx
  on public.provider_entity_mappings(source_record_id);
create index provider_entity_mappings_status_idx
  on public.provider_entity_mappings(provider_id, mapping_status);

alter table public.provider_entity_mappings enable row level security;
revoke all on table public.provider_entity_mappings from public, anon, authenticated;
grant select, insert, update, delete on table public.provider_entity_mappings to service_role;
grant usage, select on all sequences in schema public to service_role;
