from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_DIR = ROOT / "supabase" / "migrations"
REMEDIATION = MIGRATION_DIR / "20260727000000_security_advisor_remediation.sql"
VERIFY = (
    ROOT
    / "supabase"
    / "verification"
    / "20260727000000_security_advisor_remediation_verify.sql"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.lower()).strip()


class SecurityAdvisorRemediationMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = _read(REMEDIATION)
        cls.norm = _norm(cls.sql)
        cls.verify = _read(VERIFY)

    def test_current_view_is_security_invoker_and_filtered(self) -> None:
        self.assertIn("create view public.card_image_manifests_current", self.norm)
        self.assertIn("with (security_invoker = true)", self.norm)
        self.assertIn("m.is_current = true", self.norm)
        self.assertIn("m.verification_status = 'verified'", self.norm)
        self.assertIn("grant select on table public.card_image_manifests_current to anon, authenticated, service_role", self.norm)

    def test_current_view_omits_sensitive_pipeline_columns(self) -> None:
        # Column list for the public current view should not re-export internals.
        current_block = self.sql.split("create view public.card_image_manifests_current", 1)[1]
        current_block = current_block.split("create view public.card_image_manifests_with_legacy_records", 1)[0]
        current_norm = _norm(current_block)
        for forbidden in (
            "source_sha256",
            "source_url",
            "r2_original_key",
            "r2_bucket",
            "verification_reason",
            "source_license_or_terms",
            "source_card_identifier",
        ):
            self.assertNotIn(forbidden, current_norm)

    def test_legacy_view_is_service_role_only(self) -> None:
        self.assertIn(
            "revoke all on table public.card_image_manifests_with_legacy_records from public, anon, authenticated, service_role",
            self.norm,
        )
        self.assertIn(
            "grant select on table public.card_image_manifests_with_legacy_records to service_role",
            self.norm,
        )
        self.assertNotIn(
            "grant select on table public.card_image_manifests_with_legacy_records to anon",
            self.norm,
        )

    def test_pricing_rpcs_authenticated_not_anon(self) -> None:
        self.assertIn(
            "grant execute on function public.get_market_price_bundle(text, integer) to authenticated, service_role",
            self.norm,
        )
        self.assertIn(
            "revoke all on function public.get_market_price_bundle(text, integer) from public, anon, authenticated, service_role",
            self.norm,
        )
        refresh = (
            "public.request_market_price_refresh( text, text, text, text, text, text, "
            "text, text, text, text, text, text, text, boolean, text, text, jsonb )"
        )
        self.assertIn(f"revoke all on function {refresh} from public, anon, authenticated, service_role", self.norm)
        self.assertIn(f"grant execute on function {refresh} to authenticated, service_role", self.norm)

    def test_internal_functions_lose_client_execute(self) -> None:
        for signature in (
            "public.handle_new_user()",
            "public.handle_new_user_default_collection()",
            "public.rls_auto_enable()",
            "public.get_or_create_market_price_key( text, text, text, text, text, text, text, text, text, text, text, text, timestamptz, text, text, jsonb )",
        ):
            with self.subTest(signature=signature):
                self.assertIn(
                    f"revoke all on function {signature} from public, anon, authenticated, service_role",
                    self.norm,
                )

        self.assertIn(
            "grant execute on function public.get_or_create_market_price_key( text, text, text, text, text, text, text, text, text, text, text, text, timestamptz, text, text, jsonb ) to service_role",
            self.norm,
        )
        self.assertNotIn("grant execute on function public.handle_new_user()", self.norm)
        self.assertNotIn("grant execute on function public.rls_auto_enable()", self.norm)

    def test_storage_list_policy_removed(self) -> None:
        self.assertIn(
            "drop policy if exists pokemon_card_images_public_read on storage.objects",
            self.norm,
        )

    def test_no_destructive_data_changes(self) -> None:
        destructive = re.findall(
            r"\b(delete from|truncate|drop table|drop schema)\b",
            self.norm,
        )
        self.assertEqual([], destructive)

    def test_verification_sql_covers_anon_auth_service_checks(self) -> None:
        verify_norm = _norm(self.verify)
        for needle in (
            "set local role anon",
            "set local role authenticated",
            "set local role service_role",
            "card_image_manifests_with_legacy_records",
            "get_market_price_bundle",
            "pokemon_card_images_public_read",
        ):
            self.assertIn(needle, verify_norm)


if __name__ == "__main__":
    unittest.main()
