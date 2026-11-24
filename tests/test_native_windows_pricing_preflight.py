"""Pure offline tests; never import Win32 browser control."""
import unittest
from cardscanr_market_engine.native_windows_pricing_preflight import (
    inspect_native_adapter_source, evaluate_migration_readiness,
)


class NativePricingPreflightTests(unittest.TestCase):
    def test_legacy_signature_fails_before_browser_use(self):
        out = inspect_native_adapter_source(
            "def navigate_query_to_sold(query, *, reset_homepage=True): pass")
        self.assertFalse(out["source_compatible"])
        self.assertEqual(out["missing_navigation_kwargs"],
                         ["attempt_id", "pre_submit_only", "price_key_id"])

    def test_kwargs_catchall_does_not_prove_contract(self):
        out = inspect_native_adapter_source(
            "def navigate_query_to_sold(query, **kwargs): pass")
        self.assertFalse(out["source_compatible"])

    def test_complete_signature_is_only_source_compatible(self):
        out = inspect_native_adapter_source(
            "def navigate_query_to_sold(query, *, reset_homepage, attempt_id, price_key_id, pre_submit_only): pass")
        self.assertTrue(out["source_compatible"])
        self.assertFalse(out["live_navigation_authorized"])

    def test_wildcard_debug_origin_fails(self):
        out = inspect_native_adapter_source(
            "def navigate_query_to_sold(query, *, reset_homepage, attempt_id, price_key_id, pre_submit_only):\n"
            "    flags = f'--remote-allow-origins=* {query}'")
        self.assertFalse(out["source_compatible"])
        self.assertIn("WILDCARD_DEBUG_ORIGIN", out["reason_codes"])

    def test_invalid_source_fails(self):
        self.assertFalse(inspect_native_adapter_source("def ???")["source_compatible"])

    def test_duplicate_entrypoint_fails(self):
        s = "def navigate_query_to_sold(query): pass\n"
        self.assertFalse(inspect_native_adapter_source(s+s)["source_compatible"])

    def inputs(self):
        return dict(source_report={"source_compatible": True},
                    gate_allowed=True, state_integrity_ok=True,
                    scheduler_disabled=True, worker_count=0,
                    windows_session_verified=True, dedicated_listener_verified=True,
                    offline_integration_passed=True,
                    permitted_marketplace_access_verified=True)

    def test_even_complete_review_never_authorizes_navigation(self):
        result = evaluate_migration_readiness(**self.inputs())
        self.assertTrue(result["ready_for_review"])
        self.assertFalse(result["live_navigation_authorized"])

    def test_existing_hold_cannot_be_overridden_by_windows_readiness(self):
        args = self.inputs()
        args["gate_allowed"] = False
        result = evaluate_migration_readiness(**args)
        self.assertFalse(result["ready_for_review"])
        self.assertIn("EXISTING_MARKETPLACE_GATE_BLOCKED", result["reason_codes"])
        self.assertFalse(result["challenge_cleared"])

    def test_each_missing_prerequisite_blocks(self):
        for key in self.inputs():
            args = self.inputs()
            args[key] = ({"source_compatible": False} if key == "source_report"
                         else 1 if key == "worker_count" else False)
            with self.subTest(key=key):
                self.assertFalse(evaluate_migration_readiness(**args)["ready_for_review"])

    def test_unknown_worker_count_fails(self):
        args = self.inputs()
        args["worker_count"] = False
        self.assertFalse(evaluate_migration_readiness(**args)["ready_for_review"])
