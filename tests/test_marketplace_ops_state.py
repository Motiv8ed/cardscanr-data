from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.marketplace_ops_state import (
    classify_provider_failure,
    get_active_cooldown,
    maybe_record_failure_cooldown,
    record_marketplace_cooldown,
)


class MarketplaceOpsStateTests(unittest.TestCase):
    def _isolate(self, tmp: str) -> Path:
        root = Path(tmp)
        ops = root / "ops.json"
        incidents = root / "control_plane_incidents.json"
        os.environ["CONTROL_PLANE_INCIDENTS_PATH"] = str(incidents)
        os.environ["MARKET_OPS_STATE_PATH"] = str(ops)
        self.addCleanup(lambda: os.environ.pop("CONTROL_PLANE_INCIDENTS_PATH", None))
        self.addCleanup(lambda: os.environ.pop("MARKET_OPS_STATE_PATH", None))
        return ops

    def test_auth_and_challenge_never_classified_as_no_comps(self) -> None:
        self.assertEqual(
            classify_provider_failure("eBay redirected the public sold-listing search to authentication"),
            "AUTH_REQUIRED",
        )
        self.assertEqual(
            classify_provider_failure(
                "eBay returned a verification challenge; captcha bypass is not attempted",
                diagnostics={"providerOutcome": "challenge_detected"},
            ),
            "CHALLENGE_REQUIRED",
        )
        self.assertEqual(
            classify_provider_failure("no_clean_exact_comps"),
            "NO_COMPS",
        )
        self.assertNotEqual(
            classify_provider_failure("authentication required before comps"),
            "NO_COMPS",
        )

    def test_chrome_launch_failure_not_classified_as_challenge(self) -> None:
        self.assertEqual(
            classify_provider_failure(
                "Installed Google Chrome could not be launched through Playwright channel='chrome'. "
                "Install Google Chrome, then verify Playwright support with: python -m playwright install chromium",
                diagnostics={"browserConfig": {"challengeStop": True, "headless": False}},
            ),
            "ERROR",
        )
        with tempfile.TemporaryDirectory() as tmp:
            ops = self._isolate(tmp)
            recorded = maybe_record_failure_cooldown(
                market="AU",
                message="Installed Google Chrome could not be launched through Playwright channel='chrome'",
                diagnostics={"browserConfig": {"challengeStop": True}},
                path=ops,
            )
            self.assertIsNone(recorded)

    def test_cooldown_recorded_and_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._isolate(tmp)
            now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
            state = record_marketplace_cooldown(
                "GB",
                reason="AUTH_REQUIRED",
                message="sign-in required",
                hours=6,
                now=now,
                path=path,
            )
            self.assertTrue(state.is_active(now=now + timedelta(hours=1)))
            active = get_active_cooldown("GB", now=now + timedelta(hours=1), path=path)
            self.assertIsNotNone(active)
            self.assertEqual(active.reason, "AUTH_REQUIRED")
            expired = get_active_cooldown("GB", now=now + timedelta(hours=7), path=path)
            self.assertIsNone(expired)

    def test_maybe_record_ignores_no_comps_and_existing_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._isolate(tmp)
            now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
            self.assertIsNone(
                maybe_record_failure_cooldown(
                    market="AU",
                    message="no_clean_exact_comps",
                    now=now,
                    path=path,
                )
            )
            first = maybe_record_failure_cooldown(
                market="CA",
                message="verification challenge",
                diagnostics={"providerOutcome": "challenge_detected"},
                now=now,
                path=path,
            )
            self.assertIsNotNone(first)
            second = maybe_record_failure_cooldown(
                market="CA",
                message="verification challenge again",
                diagnostics={"providerOutcome": "challenge_detected"},
                now=now + timedelta(minutes=5),
                path=path,
            )
            self.assertEqual(first.until, second.until)
            # Must not touch production ledger
            prod = ROOT / "reports" / "runtime" / "control_plane_incidents.json"
            if prod.exists():
                blob = prod.read_text(encoding="utf-8")
                self.assertNotIn(str(first.incident_id or ""), blob)


if __name__ == "__main__":
    unittest.main()
