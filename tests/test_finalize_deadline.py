"""Regression: post-Sold finalize must not hang indefinitely."""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cardscanr_market_engine.ebay_availability import (  # noqa: E402
    EbayAvailabilitySnapshot,
    begin_probe,
    get_availability,
    release_probe_local_failure,
    save_availability,
)
from cardscanr_market_engine.finalize_deadline import (  # noqa: E402
    FINALIZE_TIMEOUT_SAFE,
    run_with_finalize_deadline,
)
from cardscanr_market_engine.marketplace_ops_state import utc_now  # noqa: E402
from cardscanr_market_engine.owned_daily_outcomes import classify_exception_outcome  # noqa: E402
from cardscanr_market_engine.providers.errors import ProviderTemporaryError  # noqa: E402


class FinalizeDeadlineTests(unittest.TestCase):
    def test_success_path(self) -> None:
        out = run_with_finalize_deadline(lambda: "ok", timeout_seconds=2)
        self.assertEqual(out, "ok")

    def test_hang_raises_finalize_timeout_safe_and_calls_on_timeout(self) -> None:
        cancelled = threading.Event()

        def hang() -> str:
            # Mimic wedged CDP: poll until disconnect (on_timeout) unblocks.
            deadline = time.time() + 10
            while time.time() < deadline:
                if cancelled.is_set():
                    raise RuntimeError("cdp_disconnected_by_watchdog")
                time.sleep(0.05)
            return "never"

        def on_timeout() -> None:
            cancelled.set()

        started = time.monotonic()
        with self.assertRaises(ProviderTemporaryError) as ctx:
            run_with_finalize_deadline(
                hang,
                timeout_seconds=0.35,
                on_timeout=on_timeout,
                stage="unit_hang",
            )
        elapsed = time.monotonic() - started
        err = ctx.exception
        self.assertIn(FINALIZE_TIMEOUT_SAFE, str(err))
        self.assertEqual(err.diagnostics.get("terminal"), FINALIZE_TIMEOUT_SAFE)
        self.assertEqual(err.diagnostics.get("ownedDailyOutcome"), FINALIZE_TIMEOUT_SAFE)
        self.assertTrue(err.diagnostics.get("lastGoodRetained"))
        self.assertFalse(err.diagnostics.get("falseFreshness"))
        self.assertTrue(cancelled.is_set())
        self.assertLess(elapsed, 3.0)

    def test_classify_outcome(self) -> None:
        exc = ProviderTemporaryError(
            f"{FINALIZE_TIMEOUT_SAFE}: post_sold exceeded",
            diagnostics={"ownedDailyOutcome": FINALIZE_TIMEOUT_SAFE, "reason": "post_sold_finalize_timeout"},
        )
        self.assertEqual(classify_exception_outcome(exc, diagnostics=exc.diagnostics), FINALIZE_TIMEOUT_SAFE)

    def test_release_probe_does_not_mark_healthy_or_sorry(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "avail.json"
            now = utc_now()
            snap = EbayAvailabilitySnapshot(
                state="PROBE_REQUIRED",
                consecutive_sorry_events=2,
                next_probe_at=now,
                probe_in_flight=False,
                market="AU",
            )
            save_availability(snap, path=path, force=True)
            begin_probe(now=now, path=path)
            self.assertTrue(get_availability(path=path).probe_in_flight)
            released = release_probe_local_failure(
                now=now,
                path=path,
                reference="FINALIZE_TIMEOUT_SAFE unit",
            )
            self.assertFalse(released.probe_in_flight)
            self.assertEqual(released.state, "PROBE_REQUIRED")
            self.assertEqual(released.consecutive_sorry_events, 2)
            self.assertFalse(released.confirmed_healthy)
            self.assertEqual(released.last_outcome, "probe_finalize_timeout_safe")


if __name__ == "__main__":
    unittest.main()
