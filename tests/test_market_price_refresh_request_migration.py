from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
STATE_MIGRATION = ROOT / "supabase" / "migrations" / "20260606000000_market_price_refresh_state_cache_rows.sql"
MIGRATIONS = (
    ROOT / "supabase" / "migrations" / "20260528000000_market_price_refresh_request_cooldown.sql",
    STATE_MIGRATION,
    ROOT / "supabase" / "migrations" / "20260704000000_jp_market_price_identity_payload.sql",
)


class MarketPriceRefreshRequestMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = "\n".join(path.read_text(encoding="utf-8") for path in MIGRATIONS)
        cls.state_sql = STATE_MIGRATION.read_text(encoding="utf-8")

    def test_request_rpc_contract_exists(self) -> None:
        self.assertIn("create or replace function public.request_market_price_refresh", self.sql)
        for field in (
            "'action'",
            "'state'",
            "'price_key_id'",
            "'job_id'",
            "'job_status'",
            "'cache_last_updated_at'",
            "'cooldown_hours'",
            "'cooldown_until'",
            "'cooldown_reason'",
            "'cache_is_fresh'",
            "'cache_state'",
            "'refresh_state'",
            "'cache_has_current_price'",
            "'current_market_evidence_available'",
            "'stale_cache_available'",
            "'active_refresh_job'",
        ):
            self.assertIn(field, self.sql)

    def test_no_duplicate_job_during_cooldown_actions_exist(self) -> None:
        self.assertIn("'cache_fresh'", self.sql)
        self.assertIn("'active_job_exists'", self.sql)
        self.assertIn("'job_enqueued'", self.sql)
        self.assertRegex(self.sql, re.compile(r"status\s+in\s+\('queued',\s*'running'\)", re.IGNORECASE))

    def test_request_states_cover_app_read_cases(self) -> None:
        for state in (
            "'existing_fresh_cache'",
            "'stale_cache_refresh_queued'",
            "'refresh_queued'",
            "'refresh_running'",
            "'cooldown'",
            "'provider_failed'",
            "'unsupported_market'",
            "'no_evidence_found'",
            "'cache_missing_unexpected'",
        ):
            self.assertIn(state, self.sql)

    def test_enqueue_claim_and_fail_create_cache_state_rows(self) -> None:
        self.assertIn("create or replace function public.upsert_market_price_refresh_cache_state", self.sql)
        self.assertRegex(
            self.sql,
            re.compile(r"perform\s+public\.upsert_market_price_refresh_cache_state\(", re.IGNORECASE),
        )
        self.assertRegex(
            self.sql,
            re.compile(r"insert\s+into\s+public\.market_price_cache[\s\S]*?'running'", re.IGNORECASE),
        )
        self.assertRegex(
            self.sql,
            re.compile(r"insert\s+into\s+public\.market_price_cache[\s\S]*?'failed'", re.IGNORECASE),
        )

    def test_unsupported_market_does_not_enqueue_job(self) -> None:
        self.assertIn("create or replace function public.market_price_supported_route", self.sql)
        unsupported_index = self.state_sql.index("'unsupported_market'")
        enqueue_index = self.state_sql.index("v_job := public.enqueue_market_price_refresh")
        self.assertLess(unsupported_index, enqueue_index)

    def test_evidence_url_dedupe_is_scoped_to_snapshot(self) -> None:
        self.assertIn("drop index if exists public.idx_market_sold_listing_evidence_provider_market_url_unique", self.sql)
        self.assertIn(
            "idx_market_sold_listing_evidence_snapshot_provider_market_url_unique",
            self.sql,
        )
        self.assertIn("(snapshot_id, provider, marketplace, listing_url)", self.sql)

    def test_force_refresh_is_blocked_for_normal_callers(self) -> None:
        self.assertIn("force_refresh is reserved for service_role", self.sql)
        self.assertIn("auth.role()", self.sql)
        self.assertIn("'service_role'", self.sql)

    def test_user_refresh_priority_is_ten(self) -> None:
        self.assertRegex(
            self.sql,
            re.compile(r"enqueue_market_price_refresh\([\s\S]*?v_requested_reason,\s*10,", re.IGNORECASE),
        )

    def test_market_identity_inputs_are_present(self) -> None:
        for arg in (
            "p_market_country text",
            "p_currency text",
            "p_fingerprint text",
            "p_condition text",
            "p_canonical_name_en text",
            "p_original_name_ja text",
            "p_aliases jsonb",
        ):
            self.assertIn(arg, self.sql)

    def test_japanese_source_identity_columns_are_present(self) -> None:
        for field in (
            "canonical_name_en text",
            "original_name_ja text",
            "aliases jsonb",
        ):
            self.assertIn(field, self.sql)

    def test_japanese_identity_migration_removes_ambiguous_legacy_overloads(self) -> None:
        migration = MIGRATIONS[-1].read_text(encoding="utf-8")
        self.assertIn("drop function if exists public.request_market_price_refresh", migration)
        self.assertIn(
            "text, text, text, text, text, text, text, text, text, text, text, text, text, boolean",
            migration,
        )
        self.assertIn("drop function if exists public.get_or_create_market_price_key", migration)
        self.assertIn(
            "text, text, text, text, text, text, text, text, text, text, text, text, timestamptz",
            migration,
        )


if __name__ == "__main__":
    unittest.main()
