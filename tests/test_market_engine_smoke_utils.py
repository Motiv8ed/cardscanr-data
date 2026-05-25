from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.smoke_utils import REDACTED, missing_smoke_env_vars, sanitize_for_report


class SmokeUtilsTests(unittest.TestCase):
    def test_missing_smoke_env_vars_reports_required_values(self) -> None:
        missing = missing_smoke_env_vars({})
        self.assertIn("SUPABASE_URL", missing)
        self.assertIn("SUPABASE_SERVICE_ROLE_KEY", missing)

    def test_missing_smoke_env_vars_requires_mock_provider(self) -> None:
        missing = missing_smoke_env_vars(
            {
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "secret",
                "MARKET_LOOKUP_PROVIDER": "real_provider",
            }
        )
        self.assertIn("MARKET_LOOKUP_PROVIDER=mock", missing)

    def test_missing_smoke_env_vars_empty_when_env_is_ready(self) -> None:
        missing = missing_smoke_env_vars(
            {
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "secret",
                "MARKET_LOOKUP_PROVIDER": "mock",
            }
        )
        self.assertEqual(missing, [])

    def test_sanitize_for_report_redacts_secret_like_keys(self) -> None:
        payload = {
            "apikey": "abc",
            "nested": {"SUPABASE_SERVICE_ROLE_KEY": "xyz", "ok": "value"},
            "array": [{"authorization": "bearer 123"}, {"value": 1}],
        }
        sanitized = sanitize_for_report(payload)
        self.assertEqual(sanitized["apikey"], REDACTED)
        self.assertEqual(sanitized["nested"]["SUPABASE_SERVICE_ROLE_KEY"], REDACTED)
        self.assertEqual(sanitized["nested"]["ok"], "value")
        self.assertEqual(sanitized["array"][0]["authorization"], REDACTED)


if __name__ == "__main__":
    unittest.main()
