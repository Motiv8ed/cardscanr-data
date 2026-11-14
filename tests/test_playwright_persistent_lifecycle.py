from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cardscanr_market_engine.playwright_persistent_lifecycle import (
    CHROME_PROFILE_LOCK_NAMES,
    is_target_closed_error,
    launch_persistent_context_with_retry,
    profile_lock_artifacts_present,
    teardown_persistent_context,
    wait_for_profile_lock_release,
)


class TargetClosedClassificationTests(unittest.TestCase):
    def test_target_closed_by_name(self) -> None:
        class TargetClosedError(Exception):
            pass

        self.assertTrue(is_target_closed_error(TargetClosedError("closed")))

    def test_target_closed_by_message(self) -> None:
        self.assertTrue(is_target_closed_error(RuntimeError("playwright._impl._errors.TargetClosedError: boom")))


class ProfileLockTests(unittest.TestCase):
    def test_wait_for_lock_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / CHROME_PROFILE_LOCK_NAMES[0]).touch()
            self.assertTrue(profile_lock_artifacts_present(profile))

            def _clear_lock() -> None:
                (profile / CHROME_PROFILE_LOCK_NAMES[0]).unlink()

            with mock.patch(
                "cardscanr_market_engine.playwright_persistent_lifecycle.time.sleep",
                side_effect=lambda _s: _clear_lock(),
            ):
                self.assertTrue(wait_for_profile_lock_release(profile, timeout_seconds=1.0, poll_seconds=0.01))


class LaunchRetryTests(unittest.TestCase):
    def test_retries_once_on_target_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            calls = {"n": 0}

            def _launch() -> str:
                calls["n"] += 1
                if calls["n"] == 1:

                    class TargetClosedError(Exception):
                        pass

                    raise TargetClosedError("profile lock race")
                return "ok"

            result = launch_persistent_context_with_retry(_launch, profile_dir=profile, max_attempts=2)
            self.assertEqual(result, "ok")
            self.assertEqual(calls["n"], 2)

    def test_teardown_invokes_close_and_stop(self) -> None:
        context = mock.Mock()
        pw = mock.Mock()
        teardown_persistent_context(context=context, pw=pw, profile_dir=None, wait_profile_lock=False)
        context.close.assert_called_once()
        pw.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()