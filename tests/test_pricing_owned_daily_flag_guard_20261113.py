"""Regression tests for the production owned-daily release flag and AU safety budget.

All tests are offline: no Supabase, browser, scheduler writes or eBay requests.
"""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from cardscanr_market_engine import owned_daily_enablement
from workers import market_price_scheduler


class OwnedDailyReleaseHoldTests(TestCase):
    def test_explicit_false_file_blocks_true_environment_and_does_not_enqueue(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            flag = root / "owned_daily_full_enable.flag"
            flag.write_text("false\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"OWNED_DAILY_FULL_ENABLE": "true"}),
                patch.object(owned_daily_enablement, "FLAG_PATH", flag),
                patch.object(market_price_scheduler, "ROOT", root),
            ):
                result = market_price_scheduler._maybe_run_owned_daily(object())
            self.assertIsNone(result)
            self.assertFalse((root / "reports/runtime/owned_daily_last_run_utc.txt").exists())

    def test_invalid_file_fails_closed_even_when_environment_true(self) -> None:
        with TemporaryDirectory() as temp:
            flag = Path(temp) / "owned_daily_full_enable.flag"
            flag.write_text("perhaps\n", encoding="utf-8")
            with patch.dict(os.environ, {"OWNED_DAILY_FULL_ENABLE": "true"}):
                self.assertFalse(owned_daily_enablement.owned_daily_full_enable(flag_path=flag))

    def test_true_file_wins_over_false_environment(self) -> None:
        with TemporaryDirectory() as temp:
            flag = Path(temp) / "owned_daily_full_enable.flag"
            flag.write_text("true\n", encoding="utf-8")
            with patch.dict(os.environ, {"OWNED_DAILY_FULL_ENABLE": "false"}):
                self.assertTrue(owned_daily_enablement.owned_daily_full_enable(flag_path=flag))

    def test_missing_file_uses_environment_fallback(self) -> None:
        with TemporaryDirectory() as temp:
            missing = Path(temp) / "not-present.flag"
            with patch.dict(os.environ, {"OWNED_DAILY_FULL_ENABLE": "true"}):
                self.assertTrue(owned_daily_enablement.owned_daily_full_enable(flag_path=missing))
            with patch.dict(os.environ, {"OWNED_DAILY_FULL_ENABLE": "false"}):
                self.assertFalse(owned_daily_enablement.owned_daily_full_enable(flag_path=missing))

    def test_continuous_au_config_daily_cap_remains_60(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            values = owned_daily_enablement.apply_continuous_au_env()
            self.assertEqual(values["MARKET_WORKER_CONCURRENCY"], "1")
            self.assertEqual(values["MAX_LIVE_SUBMISSIONS_PER_HOUR"], "20")
            self.assertEqual(values["MAX_LIVE_SUBMISSIONS_PER_DAY"], "60")
            self.assertEqual(os.environ["MAX_LIVE_SUBMISSIONS_PER_DAY"], "60")
