"""Ported PR #30 poisoned-tab invalidation helper with offline tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cardscanr_market_engine.browser_lifecycle_policy import RUNTIME_COLD_START, PriorCardContext
from cardscanr_market_engine.navigation_runtime_context import (
    NAV_CONTEXT_PATH_ENV,
    RUNTIME_MODE_ENV,
    invalidate_inter_card_handoff,
    load_navigation_runtime_context,
)


class InterCardInvalidationTests(unittest.TestCase):
    def test_invalidate_resets_cold_start_without_prior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nav_runtime_context.json"
            prior = PriorCardContext(
                target_id="abc",
                job_id="job",
                price_key_id="pk",
                fingerprint="fp",
                query="q",
                market="AU",
                currency="AUD",
            )
            from cardscanr_market_engine.navigation_runtime_context import NavigationRuntimeContext

            ctx = NavigationRuntimeContext(runtime_mode="INTER_CARD", expected_prior=prior)
            path.write_text(json.dumps(ctx.to_dict(), indent=2), encoding="utf-8")
            import os

            os.environ[NAV_CONTEXT_PATH_ENV] = str(path)
            os.environ[RUNTIME_MODE_ENV] = "INTER_CARD"
            reset = invalidate_inter_card_handoff(reason="capture_failure", persist=True)
            self.assertEqual(reset.runtime_mode, RUNTIME_COLD_START)
            self.assertIsNone(reset.expected_prior)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded.get("runtimeMode"), RUNTIME_COLD_START)
            self.assertEqual(loaded.get("invalidatedReason"), "capture_failure")
            runtime = load_navigation_runtime_context()
            self.assertEqual(runtime.runtime_mode, RUNTIME_COLD_START)


if __name__ == "__main__":
    unittest.main()
