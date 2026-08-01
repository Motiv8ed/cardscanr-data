from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20260801232032_worldwide_catalogue_foundation.sql"

REQUIRED_TABLES = {
    "franchises",
    "languages",
    "regions",
    "eras",
    "series",
    "sets",
    "set_releases",
    "card_designs",
    "card_printings",
    "card_variants",
    "card_text_localisations",
    "attacks",
    "abilities",
    "card_images",
    "sealed_products",
    "sealed_product_variants",
    "product_contents",
    "product_images",
    "accessories",
    "source_providers",
    "source_records",
    "source_snapshots",
    "marketplace_mappings",
    "image_validation_results",
    "import_runs",
    "publication_runs",
    "unresolved_items",
}


def migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_all_required_worldwide_tables_are_present() -> None:
    sql = migration_sql()
    created = set(re.findall(r"create table public\.([a-z_]+)", sql))
    assert REQUIRED_TABLES <= created


def test_tables_are_service_role_only_and_rls_enabled() -> None:
    sql = migration_sql()
    assert "enable row level security" in sql
    assert "revoke all on table public.%i from public, anon, authenticated" in sql
    assert "grant select, insert, update, delete on table public.%i to service_role" in sql
    assert "create policy" not in sql


def test_physical_copy_is_not_part_of_catalogue_schema() -> None:
    sql = migration_sql()
    assert "user_owned_copy" not in sql
    assert "collection_item" not in sql


def test_card_image_identity_is_variant_scoped() -> None:
    sql = migration_sql()
    card_image_body = sql.split("create table public.card_images", 1)[1].split(");", 1)[0]
    assert "card_variant_id text not null references public.card_variants(id)" in card_image_body
    assert "language_verified boolean" in card_image_body
    assert "region_verified boolean" in card_image_body
    assert "identity_verified boolean" in card_image_body
