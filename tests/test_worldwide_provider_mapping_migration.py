from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20260801234924_worldwide_provider_entity_mappings.sql"


def test_provider_mapping_migration_has_security_boundary() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "create table public.provider_entity_mappings" in sql
    assert "enable row level security" in sql
    assert "revoke all on table public.provider_entity_mappings from public, anon, authenticated" in sql
    assert "grant select, insert, update, delete" in sql


def test_provider_mapping_records_method_status_and_evidence() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    for column in ("provider_record_id", "entity_type", "entity_id", "match_method", "mapping_status", "source_record_id", "evidence"):
        assert column in sql
    assert "unique (provider_id, provider_record_type, provider_record_id, entity_type, entity_id)" in sql
