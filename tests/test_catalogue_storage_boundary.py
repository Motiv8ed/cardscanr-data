from __future__ import annotations

from pathlib import Path

from tools.validate_catalogue_storage_boundary import validate

ROOT = Path(__file__).resolve().parents[1]


def test_storage_boundary_doc_exists() -> None:
    path = ROOT / "reports" / "cloudflare_migration" / "CATALOGUE_STORAGE_BOUNDARY.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "Supabase dynamic data" in text
    assert "Cloudflare public catalogue data" in text
    assert "card_printings" in text


def test_supabase_catalogue_loader_is_opt_in_and_blocks_production() -> None:
    issues = validate()
    assert issues == [], issues


def test_cleanup_migration_does_not_drop_user_tables() -> None:
    migration = ROOT / "supabase" / "migrations" / "20260803080000_remove_worldwide_catalogue_from_supabase.sql"
    text = migration.read_text(encoding="utf-8")
    for retained in (
        "user_profiles",
        "user_collections",
        "user_cards",
        "customer_collection_items",
        "card_image_manifests",
    ):
        assert f"drop table if exists public.{retained}" not in text.lower()
    assert "drop table if exists public.card_printings" in text.lower()
    assert "drop table if exists public.franchises" in text.lower()
