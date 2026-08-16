from __future__ import annotations

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
SQL_PATH = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260817090000_drop_enqueue_market_price_refresh_integer_overload.sql"
)


class DropEnqueueIntegerOverloadMigrationTests(unittest.TestCase):
    def test_migration_drops_integer_overload_and_keeps_smallint(self) -> None:
        sql = SQL_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "drop function if exists public.enqueue_market_price_refresh(uuid, text, integer, uuid, text)",
            sql.lower().replace("\n", " "),
        )
        # Accept either formatting of the signature
        self.assertTrue(
            "enqueue_market_price_refresh(uuid, text, integer, uuid, text)" in sql
            or "enqueue_market_price_refresh(uuid,text,integer,uuid,text)" in sql.replace(" ", "")
        )
        self.assertIn("smallint", sql.lower())
        self.assertNotIn("drop function if exists public.enqueue_market_price_refresh(uuid, text, smallint", sql.lower())


if __name__ == "__main__":
    unittest.main()
