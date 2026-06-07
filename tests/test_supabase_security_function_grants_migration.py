from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_DIR = ROOT / "supabase" / "migrations"
HARDENING_MIGRATION = MIGRATION_DIR / "20260607000000_harden_security_definer_function_grants.sql"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.lower()).strip()


class SupabaseSecurityFunctionGrantsMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.all_sql = "\n".join(_read(path) for path in sorted(MIGRATION_DIR.glob("*.sql")))
        cls.hardening_sql = _read(HARDENING_MIGRATION)
        cls.hardening_norm = _norm(cls.hardening_sql)

    def test_all_declared_public_security_definer_functions_have_fixed_search_path(self) -> None:
        declarations = re.finditer(
            r"create\s+or\s+replace\s+function\s+public\.([a-z0-9_]+)\s*"
            r"\((?P<args>[\s\S]*?)\)\s*"
            r"returns\s+[\s\S]*?as\s+\$\$",
            self.all_sql,
            re.IGNORECASE,
        )

        missing: list[str] = []
        found_security_definer = False
        for declaration in declarations:
            body = declaration.group(0)
            if re.search(r"\bsecurity\s+definer\b", body, re.IGNORECASE):
                found_security_definer = True
                if not re.search(r"\bset\s+search_path\s*=\s*public\b", body, re.IGNORECASE):
                    missing.append(declaration.group(1))

        self.assertTrue(found_security_definer)
        self.assertEqual([], missing)

    def test_set_updated_at_gets_fixed_search_path_and_no_public_execute(self) -> None:
        self.assertIn("alter function public.set_updated_at() set search_path = public;", self.hardening_norm)
        self.assertIn(
            "revoke all on function public.set_updated_at() from public, anon, authenticated, service_role;",
            self.hardening_norm,
        )
        self.assertIn("grant execute on function public.set_updated_at() to service_role;", self.hardening_norm)

    def test_pricing_read_rpc_is_authenticated_only(self) -> None:
        self.assertIn(
            "revoke all on function public.get_market_price_bundle(text, integer) "
            "from public, anon, authenticated, service_role;",
            self.hardening_norm,
        )
        self.assertIn(
            "grant execute on function public.get_market_price_bundle(text, integer) "
            "to authenticated, service_role;",
            self.hardening_norm,
        )
        self.assertNotIn(
            "grant execute on function public.get_market_price_bundle(text, integer) to anon",
            self.hardening_norm,
        )

    def test_user_refresh_rpc_is_authenticated_only(self) -> None:
        request_signature = (
            "public.request_market_price_refresh( text, text, text, text, text, text, "
            "text, text, text, text, text, text, text, boolean )"
        )
        self.assertIn(f"revoke all on function {request_signature}", self.hardening_norm)
        self.assertIn(f"grant execute on function {request_signature} to authenticated, service_role;", self.hardening_norm)
        self.assertNotIn(f"grant execute on function {request_signature} to anon", self.hardening_norm)

    def test_worker_and_internal_functions_are_service_role_only(self) -> None:
        service_only_signatures = (
            "public.get_or_create_market_price_key(",
            "public.enqueue_market_price_refresh(uuid, text, smallint, uuid, text)",
            "public.claim_market_price_refresh_jobs(text, integer)",
            "public.complete_market_price_refresh_job(",
            "public.fail_market_price_refresh_job(",
            "public.upsert_market_price_refresh_cache_state(",
            "public.market_price_refresh_cooldown_hours(",
            "public.market_price_supported_route(text, text, text)",
        )

        for signature in service_only_signatures:
            with self.subTest(signature=signature):
                signature_index = self.hardening_norm.index(signature)
                grant_index = self.hardening_norm.index("to service_role", signature_index)
                next_statement = self.hardening_norm.find(";", grant_index)
                grant_statement = self.hardening_norm[grant_index:next_statement]
                self.assertNotIn("authenticated", grant_statement)
                self.assertNotIn("anon", grant_statement)

    def test_deployed_advisor_functions_are_hardened_when_present(self) -> None:
        for signature in (
            "public.enqueue_market_price_refresh(uuid,text,integer,uuid,text)",
            "public.handle_new_user()",
            "public.handle_new_user_default_collection()",
            "public.rls_auto_enable()",
        ):
            with self.subTest(signature=signature):
                self.assertIn(f"'{signature}'", self.hardening_sql)

        self.assertIn(
            "revoke all on function %s from public, anon, authenticated, service_role",
            self.hardening_sql,
        )
        self.assertIn("grant execute on function %s to service_role", self.hardening_sql)


if __name__ == "__main__":
    unittest.main()
