from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "supabase" / "migrations" / "20260528000000_market_price_refresh_request_cooldown.sql"


class MarketPriceRefreshRequestMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8")

    def test_request_rpc_contract_exists(self) -> None:
        self.assertIn("create or replace function public.request_market_price_refresh", self.sql)
        for field in (
            "'action'",
            "'price_key_id'",
            "'job_id'",
            "'job_status'",
            "'cache_last_updated_at'",
            "'cooldown_hours'",
            "'cooldown_until'",
            "'cooldown_reason'",
            "'cache_is_fresh'",
            "'active_refresh_job'",
        ):
            self.assertIn(field, self.sql)

    def test_no_duplicate_job_during_cooldown_actions_exist(self) -> None:
        self.assertIn("'cache_fresh'", self.sql)
        self.assertIn("'active_job_exists'", self.sql)
        self.assertIn("'job_enqueued'", self.sql)
        self.assertRegex(self.sql, re.compile(r"status\s+in\s+\('queued',\s*'running'\)", re.IGNORECASE))

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
        ):
            self.assertIn(arg, self.sql)


if __name__ == "__main__":
    unittest.main()
